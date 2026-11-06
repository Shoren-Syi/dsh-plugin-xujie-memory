"""事实的 key 归一与值比较（M8.1 / M8.2）。

这一层只做两件事，但两件都不做就会让"矛盾检测"变成"每换一种说法就报一次"：

**一、key 闭集。** 模型给 `居住城市`、`居住地`、`city` 三种写法，
必须落到同一个 key，否则它们各自 active，同一件事有两个当前值。
表外的 key 一律拒绝——拒绝比放行安全，放行等于承认"key 可以无限长"。

**二、值归一。** 这一条是 M8 真正的坑，手册里没写：

    上海 / 上海市 / 沪          同值
    字节 / 字节跳动             同值
    清华 / 清华大学             同值
    三岁 / 3 岁                 同值
    北京（海淀） / 海淀          不冲突，后者更具体

不做归一，"矛盾检出率"会靠**假矛盾**刷上去：用户每换一种写法，
系统就记一次"你搬家了？"。指标好看，记忆错乱。

反过来，归一不能做得太狠：`张伟` / `张伟明` 看着像前缀包含，
但那是两个人。所以这里按 `type` 分开处理——`place` / `org` 允许
"后缀省略"和"包含关系"，人名（`text`）一个字都不许省。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .timeparse import cn_int

__all__ = [
    "Key", "KeyTable", "load_keys", "DEFAULT_KEYS_PATH",
    "fold_value", "same_value", "more_specific", "explain",
]

DEFAULT_KEYS_PATH = Path(__file__).resolve().parent.parent / "data" / "fact_keys.yaml"

# 地点后缀。去掉之后"上海市"和"上海"才相等。
# 只放行政级别后缀，不放"湾""州"这类词——"杭州"去掉"州"就成了"杭"。
_PLACE_SUFFIX = ("特别行政区", "自治区", "自治州", "街道", "省", "市", "区", "县", "镇", "乡")

# 机构后缀。"清华大学"→"清华"，"字节跳动有限公司"→"字节跳动"→（别名）→"字节"。
# 刻意**不含**"银行""医院""大学"里的"大"等，也不含"银行"——
# 去掉"银行"会让"北京银行"变成"北京"，那是把一家公司当地名。
_ORG_SUFFIX = (
    "股份有限公司", "有限责任公司", "有限公司", "集团", "公司",
    "科技", "网络", "信息技术", "控股",
    # 学校单独列：机构的"大学/学院"和"公司/集团"一样是通名后缀，
    # 去掉之后"清华大学"和"清华"才是一回事。刻意**不含**"银行""医院"——
    # 去掉"银行"会把"北京银行"变成"北京"，那是把一家公司当地名。
    "大学", "学院", "中学", "小学", "研究院", "研究所",
)

# 值的别名。这张表是"手写的常识"，长尾靠上面两条机械规则。
# 只收高频且**确定同义**的，不收"可能同义"的。
_VALUE_ALIASES = {
    # 城市简称
    "沪": "上海", "京": "北京", "穗": "广州", "鹏城": "深圳", "羊城": "广州",
    "帝都": "北京", "魔都": "上海", "山城": "重庆", "蓉城": "成都",
    # 公司简称
    "字节跳动": "字节", "字节跳动有限公司": "字节", "头条": "字节",
    "阿里": "阿里巴巴", "腾讯科技": "腾讯", "美团点评": "美团",
    # 通勤
    "走路": "步行", "步行": "步行", "骑车": "骑行", "开车": "自驾",
    "地铁": "地铁", "公交": "公交", "打车": "打车",
}

# 中文数字 + 单位，用于 number 类型。"三岁" / "3 岁" / "3" 同值
_NUM_RE = re.compile(r"(\d+|[零一二两三四五六七八九十]+)")
_UNIT_TAIL = re.compile(r"(多岁|来岁|左右|出头|多|余|个|岁|年|月|天|小时|分钟)$")

# "北京银行"不是"北京"的更具体版本，是另一家机构。
# 这类通名一旦出现在长词的剩余部分，说明长词是**另一个专名**，
# 不是短词的细化。不设这条，"北京银行"会被判成"北京"的粒度差异，
# 于是用户换工作时系统认为"还是原来那家"。
_DISTINCT_TAIL = (
    "银行", "医院", "学校", "大学", "学院", "公司", "集团", "酒店",
    "超市", "地铁", "机场", "车站", "大厦", "广场", "公园", "法院",
)

# 括号里的补充说明。"北京（海淀）"→"北京"；"养猫（下周出差代喂）"→"养猫"
_PAREN = re.compile(r"[（(][^）)]*[）)]")


@dataclass(frozen=True)
class Key:
    name: str
    cn: str
    card: str          # single / multi
    type: str          # place / org / enum / number / date / text
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def single(self) -> bool:
        return self.card == "single"


class KeyTable:
    """key 的闭集。构造一次，全局复用。"""

    def __init__(self, keys: dict[str, Key], raw: dict[str, Any] | None = None):
        self.keys = keys
        self.raw = raw or {}
        # 别名 → 正名。别名冲突时正名优先（先注册正名，别名不覆盖）。
        self._index: dict[str, str] = {}
        for name in keys:
            self._index[self._fold(name)] = name
        for name, k in keys.items():
            for a in k.aliases:
                self._index.setdefault(self._fold(a), name)

    @staticmethod
    def _fold(s: str) -> str:
        s = unicodedata.normalize("NFKC", str(s or "")).strip().lower()
        return re.sub(r"[\s_\-]+", "", s)

    def canonical(self, raw: str) -> str | None:
        """模型给的 key → 正名。表外返回 None（调用方据此拒绝）。

        两轮匹配，顺序不能反：
          1. 精确（含别名精确）。`工作单位` 必须落到 employer，
             不能被"包含 `工作`"抢去 occupation。
          2. 包含。模型爱加修饰词（`房贷剩余年限`、`用户的居住城市`），
             只认**最长的那个别名**；两个不同 key 的最长别名一样长，
             说明这个词本身有歧义，返回 None 比猜一个安全。
        """
        n = self._fold(raw)
        if not n:
            return None
        hit = self._index.get(n)
        if hit:
            return hit

        best: list[tuple[int, str]] = []
        for alias_fold, name in self._index.items():
            if len(alias_fold) >= 2 and alias_fold in n:
                best.append((len(alias_fold), name))
        if not best:
            return None
        best.sort(reverse=True)
        if len(best) > 1 and best[1][0] == best[0][0] and best[1][1] != best[0][1]:
            return None
        return best[0][1]

    def get(self, name: str) -> Key | None:
        return self.keys.get(name)

    def __len__(self) -> int:
        return len(self.keys)

    def prompt_block(self) -> str:
        """给模型看的词表。一行一个，`key 中文`。

        全表大约 30 行、400 token。它在固定前缀里，命中缓存后几乎不花钱；
        换来的是一条硬约束：模型没有"自己发明 key"这个选项。
        """
        lines = []
        for name, k in self.keys.items():
            lines.append(f"    {name:<12} {k.cn}")
        return "\n".join(lines)


def load_keys(path: str | Path | None = None) -> KeyTable:
    p = Path(path) if path else DEFAULT_KEYS_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    keys: dict[str, Key] = {}
    for name, spec in (data.get("keys") or {}).items():
        keys[name] = Key(
            name=name,
            cn=str(spec.get("cn") or name),
            card=str(spec.get("card") or "single"),
            type=str(spec.get("type") or "text"),
            aliases=tuple(spec.get("aliases") or ()),
        )
    if not keys:
        raise ValueError(f"{p} 里没有 keys")
    return KeyTable(keys, data)


def _base(v: str) -> str:
    s = unicodedata.normalize("NFKC", str(v or ""))
    s = _PAREN.sub("", s)
    s = re.sub(r"[\s\u3000]+", "", s)
    s = s.strip("，。、；：！？,.;:!?\"'“”‘’《》()（）[]【】")
    return s.lower()


def fold_value(type_: str, value: str) -> str:
    """把一个值归一成"比较用的形式"。不同 type 规则不同。

    注意这里返回的字符串**只用于比较**，不写库。库里存原文——
    写归一后的值会丢信息（"上海市浦东新区"被砍成"上海"，用户看到会莫名其妙）。
    """
    s = _base(value)
    if not s:
        return ""

    s = _VALUE_ALIASES.get(s, s)

    if type_ == "place":
        # 反复剥："上海市浦东新区" → 浦东新区 → 浦东
        for _ in range(3):
            for suf in _PLACE_SUFFIX:
                if s.endswith(suf) and len(s) - len(suf) >= 2:
                    s = s[: -len(suf)]
                    break
            else:
                break
        s = _VALUE_ALIASES.get(s, s)
        return s

    if type_ == "org":
        for _ in range(3):
            for suf in _ORG_SUFFIX:
                if s.endswith(suf) and len(s) - len(suf) >= 2:
                    s = s[: -len(suf)]
                    break
            else:
                break
        return _VALUE_ALIASES.get(s, s)

    if type_ in ("number", "date"):
        s = _UNIT_TAIL.sub("", s)
        m = _NUM_RE.search(s)
        if m:
            n = cn_int(m.group(1))
            if n is not None:
                return str(n)
        return s

    if type_ == "enum":
        return _VALUE_ALIASES.get(s, s)

    # text：只做空白与标点归一。人名不许省字——
    # "张伟" 和 "张伟明" 差一个字，是两个人。
    return s


def facets(type_: str, value: str) -> list[str]:
    """一个值拆成若干"面"。地点特有：`北京（海淀）` 同时说明了两级。

    为什么要拆：模型写 `北京（海淀）`，用户后来说 `海淀`。
    字面上两者不相等也不包含（括号被剥掉之后），但它们不矛盾——
    是同一个人的同一件事，一次说全、一次说细。
    """
    out: list[str] = []
    main = _PAREN.sub("", unicodedata.normalize("NFKC", str(value or "")))
    f = fold_value(type_, main)
    if f:
        out.append(f)
    if type_ == "place":
        for inner in _PAREN.findall(str(value or "")):
            for part in re.split(r"[/、,，\s]+", inner.strip("（）()")):
                g = fold_value(type_, part)
                if g and g not in out:
                    out.append(g)
    return out


def same_value(type_: str, a: str, b: str) -> bool:
    fa, fb = facets(type_, a), facets(type_, b)
    return bool(set(fa) & set(fb))


def more_specific(type_: str, new: str, old: str) -> bool:
    """new 和 old 是不是同一件事的两种粒度（兼容，不算矛盾）。

    只对 place / org 开放。`北京（海淀）` 与 `海淀`、`字节` 与 `字节跳动`、
    `上海` 与 `上海市浦东新区` 都不是矛盾，是说得粗细不同。

    规则是**前缀包含**，不是任意包含：中文地名和机构名都是
    "大在前、小在后"（上海市 / 浦东新区），反过来"海淀"出现在
    "北京（海淀）"中间只因为括号，而括号已经被拆成 facets 了。

    人名不适用：`王建国` 包含 `王建`，但那是两个人。
    """
    if type_ not in ("place", "org"):
        return False
    for a in facets(type_, new):
        for b in facets(type_, old):
            if a == b:
                continue
            short, long_ = (a, b) if len(a) < len(b) else (b, a)
            if len(short) < 2:
                continue
            if not long_.startswith(short):
                continue
            tail = long_[len(short):]
            if any(t in tail for t in _DISTINCT_TAIL):
                continue
            if len(tail) <= 8:
                return True
    return False


def explain(type_: str, a: str, b: str) -> str:
    """给报告/日志用的一句话判定理由。不说人话的日志等于没有日志。"""
    if same_value(type_, a, b):
        return f"同值（{fold_value(type_, a)!r}）"
    if more_specific(type_, a, b):
        return "粒度不同，不冲突"
    return f"值不同（{fold_value(type_, a)!r} ≠ {fold_value(type_, b)!r}）"


# ---------------------------------------------------------------- 写入决策
@dataclass
class Decision:
    """一条 fact 该怎么落库。纯数据，不含 SQL —— 由 store 执行。"""

    action: str                       # insert / same / refine / replace / doubt / reject
    keep_id: int | None = None        # 命中的那条（same / refine）
    close_ids: tuple[int, ...] = ()   # 要关掉的旧条（replace）
    value: str = ""                   # 实际写入的值
    question: str = ""
    reason: str = ""

    @property
    def written(self) -> bool:
        return self.action in ("insert", "refine", "replace")


def decide_fact(
    key: Key,
    existing: list[tuple[int, str]],
    value: str,
    confidence: float = 0.6,
    *,
    label: str = "用户",
    threshold: float = 0.6,
) -> Decision:
    """同 key 已有若干 active 行，新来一个值，该怎么办。

    existing: [(id, value), ...]，只含 active（valid_to IS NULL）。

    六种结局，每一种都有明确理由，没有"其他"：

    | action  | 触发 | 后果 |
    |---------|------|------|
    | reject  | 空值 | 不写 |
    | same    | 归一后同值 | 只抬 confidence |
    | refine  | 粒度不同（北京 / 北京（海淀）） | 就地换成更具体的，valid_from 不变 |
    | replace | single 槽值不同且可信 | 关旧开新，旧值留痕 |
    | doubt   | single 槽值不同但不可信 | 不写，问一句 |
    | insert  | 没有 active，或 multi 槽 | 新开一条 |

    `refine` 单独存在是因为它不该被记成"变化"：
    用户先说我住北京、后来说我住海淀，中间**没有搬家**。
    如果走 replace，"你什么时候搬的"这个问题就答错了。
    """
    if not str(value or "").strip():
        return Decision("reject", reason="空值")

    if not existing:
        return Decision("insert", value=value, reason="该槽无 current 值")

    # 1) 同值：只看有没有一条归一等价的
    for fid, old in existing:
        if same_value(key.type, value, old):
            # 只有新说法**更具体**时才覆盖库里的写法。
            # 否则"北京市"会把"北京"改掉、"上海市浦东新区"会把"上海"改掉——
            # 每说一次就改写一次，最后库里留下的是最后一次的说法，
            # 而不是最完整的那次。值不是越新越好，是越准越好。
            keep = value if (more_specific(key.type, value, old)
                             or len(value) > len(old) and not more_specific(key.type, old, value)) else old
            return Decision("same", keep_id=fid, value=keep,
                            reason=explain(key.type, value, old))

    # 2) 更具体的说法：就地升级，不算搬家
    for fid, old in existing:
        if more_specific(key.type, value, old):
            return Decision("refine", keep_id=fid, value=value,
                            reason=f"{value} 比 {old} 更具体，同一件事")
        if more_specific(key.type, old, value):
            # 新值更笼统（先说海淀、后说北京）：保留已有的细粒度值
            return Decision("same", keep_id=fid, value=old,
                            reason=f"{old} 比 {value} 详细，保留细的")

    # 3) multi 槽：并存，永不顶掉
    if not key.single:
        return Decision("insert", value=value, reason=f"{key.name} 可以并存")

    # 4) single 槽真冲突：可信就换，不可信就问
    old_vals = "、".join(dict.fromkeys(o for _, o in existing))
    if confidence >= threshold:
        return Decision("replace", close_ids=tuple(f for f, _ in existing), value=value,
                        reason=f"{key.cn}：{old_vals} → {value}")
    return Decision("doubt", value=value,
                    question=f"{label}的{key.cn}之前是「{old_vals}」，"
                             f"现在说的是「{value}」，哪个对？",
                    reason=f"置信度 {confidence:.2f} < {threshold}")
