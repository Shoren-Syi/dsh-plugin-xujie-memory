"""规则预筛（M4.1 / M4.2）。

零 LLM 成本地判断一句话值不值得送进抽取器。

为什么需要它
------------
一次抽取调用约 800~1500 token。20 轮对话里真正有信息的可能只有 6 轮。
不筛 → 每 5 轮抽一次，其中大半是在给"好的""哈哈"付费。
筛过之后再抽 → 同样的钱买到 3 倍的记忆密度。

判定顺序（顺序本身就是策略）
--------------------------
    1. 空 / 纯标点            → 挡
    2. 计划句式                → 放（不受长度限制）
    3. 疑问句                  → 挡（用户在问，没有新信息）
    4. 命中已知实体            → 放（长度门槛降到 3）
    5. 太短（< min_chars）    → 挡
    6. 事实陈述                → 放
    7. 偏好表达                → 放
    8. 上一轮 AI 在提问        → 放（用户在回答）
    9. 其余                    → 挡

第 3 条放在实体之前是有意的：`"老王最近怎么样？"` 命中实体，但它是提问，
不含新信息。先挡疑问再放实体，能让"提到熟人"的闲聊不白白触发抽取。

第 5 条的门槛对实体命中放宽到 3 字：`"豆豆病了"` 只有 4 个字，
但它是重要事件。而 `"嗯"` 这种 1 个字的不该因为实体名恰好是单字就放行。
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

__all__ = ["PrescreenResult", "RuleSet", "EntityIndex", "prescreen",
           "prescreen_heuristic", "load_rules"]

_DEFAULT_LEXICON = Path(__file__).resolve().parent.parent / "data" / "lexicon.yaml"

# 常见寒暄。长度规则已经能挡掉大部分，这几条是"看起来不短但没信息"的
# （"哈哈哈哈哈哈" 6 个字，纯笑声）
_FILLER = re.compile(
    r"^[\s，。！？、~～…\.\!\?哈嗯哦啊呵嘿额呃噢喔好行对是的没错收到]+$"
)

# 纯评价 / 纯天气 / 纯应承。过了 _FILLER 的长度闸（>= min_chars），
# 但仍然一个实词都没有。
#
# 为什么需要这一条：`answer_to_question` 的放行条件是"AI 上一轮在提问"，
# 而 AI 问得多，用户答什么它都放——"今天天气不错"跟在"有什么想聊的吗？"
# 后面就被放行了，白花一次调用。实测 10 个窗口里 chitchat 就是这么漏的。
#
# 刻意不做语义判断（"有没有信息量"），只堵这几类高频形态：
# 规则法做语义判断的上限在 M4 已经撞过了（留出集 R 卡在 0.72），
# 承认它做不到，比假装做得到要好。
_NO_INFO = re.compile(
    r"^(?:"
    r"今天|明天|昨天|现在"          # 时间打头的评价句
    r").{0,6}?"
    r"(?:天气|心情|感觉|还行|不错|挺好|一般|差不多)"
    # 第二支：整句只由"评价词 + 语气词 + 标点"组成。
    # ★ 标点必须用 [\s\W]，不能逐个列全角/半角字符：
    #   _normalize 会做 NFKC，"还行吧，就这样" 进来时逗号已经是半角了。
    #   照输入的字面样子写正则，就是"看着对、跑起来永不命中"。
    r"|^(?:(?:还行|不错|挺好|还好|一般|差不多|不知道|不清楚|随便|无所谓"
    r"|都可以|都行|看情况|就这样|这样吧)"
    r"[吧啊呢哦哎呀嘛哈呗了的]*"
    r"[\s\W]*)+$"
)


@dataclass(frozen=True, slots=True)
class PrescreenResult:
    """预筛结果。

    reason 一定要带上：调参时靠它统计"各分支贡献"，
    否则你只知道总数不对，不知道是哪条规则在乱咬。
    """

    passed: bool
    reason: str  # plan / entity / fact / preference / answer / question / short / empty / none
    matched: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.passed


class RuleSet:
    """一组预编译正则。加载一次，重复用。

    编译成本不可忽略（40 条正则约 3ms），而 prescreen 要求单次 <2ms。
    所以必须在构造时编译好，绝不能在函数里 re.search(pattern_str, ...)
    —— 那会让每次调用都走一遍正则编译缓存查找。
    """

    __slots__ = ("plan", "preference", "time", "fact", "question_enders",
                 "answer_to_question", "min_chars", "version", "source")

    def __init__(self, data: dict[str, Any], *, source: str = "<dict>"):
        def compile_all(key: str) -> tuple[re.Pattern[str], ...]:
            items = data.get(key) or []
            if isinstance(items, str):
                items = [items]
            return tuple(re.compile(p) for p in items)

        self.plan = compile_all("plan")
        self.preference = compile_all("preference")
        self.time = compile_all("time")
        self.fact = compile_all("fact")
        self.question_enders = tuple(data.get("question_enders") or ("?", "？"))
        self.answer_to_question = bool(data.get("answer_to_question", True))
        self.min_chars = int(data.get("min_chars", 6))
        self.version = int(data.get("version", 1))
        self.source = source

    @classmethod
    def load(cls, path: str | Path | None = None) -> RuleSet:
        p = Path(path) if path else _DEFAULT_LEXICON
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls(raw, source=str(p))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"<RuleSet plan={len(self.plan)} pref={len(self.preference)} "
                f"time={len(self.time)} fact={len(self.fact)} src={self.source}>")


_rules_cache: dict[str, RuleSet] = {}


def load_rules(path: str | Path | None = None) -> RuleSet:
    """带缓存的加载。路径变化会重新读，同路径只读一次。"""
    key = str(path or _DEFAULT_LEXICON)
    cached = _rules_cache.get(key)
    if cached is None:
        cached = RuleSet.load(path)
        _rules_cache[key] = cached
    return cached


class EntityIndex:
    """实体词表，按首字分桶。

    为什么分桶
    ----------
    "词表里每个词都在 text 里找一遍"是 O(实体数 × 文本长)。
    1000 个实体、30 字文本 ≈ 1000 次子串搜索 ≈ 0.4ms，
    看起来能忍，但实体是会长的——一年后 8000 个就是 3ms，
    预筛反倒成了每轮最慢的一步。

    按首字分桶后只查"文本里出现过的字"对应的桶，
    中文 30 字文本大约命中 20 个桶，桶内平均 10 条 → 200 次比较。

    为什么不做模糊匹配
    ------------------
    手册 M4.2 提到"trigram 相似度 >0.8 命中别名"。我不做，理由是不可逆：
    "小李" 和 "老李" 的字符相似度是 0.67，调低阈值就能命中——但那是两个不同的人。
    一旦合并，后续所有记忆都会挂错人，而且没有任何办法拆回来。

    正确的信息来源是 aliases：M5 抽取器听到"王哥"时，把它记成"老王"的别名。
    用户在对话里就是这么自我纠正的（"王哥，就是老王"），
    这比字符相似度可靠得多。
    """

    __slots__ = ("_buckets", "session", "size", "_longest")

    def __init__(self, pairs: Iterable[tuple[str, int]] = (), *, session: str = ""):
        buckets: dict[str, list[tuple[str, int]]] = {}
        longest = 0
        size = 0
        for word, eid in pairs:
            w = (word or "").strip()
            if len(w) < 2:
                # 单字名（"李"）当实体太危险，会把"李子"也命中
                continue
            buckets.setdefault(w[0], []).append((w, eid))
            longest = max(longest, len(w))
            size += 1
        # 长词优先：文本里同时有"老王"和"老王家的狗"时，先命中长的
        for v in buckets.values():
            v.sort(key=lambda p: -len(p[0]))
        self._buckets = buckets
        self._longest = longest
        self.session = session
        self.size = size

    @classmethod
    def from_store(cls, store: Any, session: str) -> EntityIndex:
        """从库里加载某个 session 的全部实体与别名。

        调用方应该一个窗口只建一次。每轮都建的话，
        一次 list_entities 就够把预筛的 2ms 预算吃光。
        """
        pairs: list[tuple[str, int]] = []
        try:
            entities = store.list_entities(session)
        except Exception:  # noqa: BLE001 - 库没迁移好时不该让聊天挂掉
            entities = []
        for e in entities:
            pairs.append((e.canonical, e.id))
            for a in e.aliases or ():
                pairs.append((a, e.id))
        return cls(pairs, session=session)

    def __len__(self) -> int:
        return self.size

    def match(self, text: str) -> tuple[int, str] | None:
        """返回 (entity_id, 命中的词)。命中最长优先。"""
        if not self._buckets or not text:
            return None
        best: tuple[int, str] | None = None
        seen: set[str] = set()
        for ch in text:
            if ch in seen:
                continue
            seen.add(ch)
            bucket = self._buckets.get(ch)
            if not bucket:
                continue
            for word, eid in bucket:
                if word in text:
                    if best is None or len(word) > len(best[1]):
                        best = (eid, word)
                    break  # 桶内已按长度降序，第一条命中就是该桶最长的
        return best


def _normalize(text: str) -> str:
    """NFKC：全角数字/字母/标点转半角。

    "８点叫我" 和 "8点叫我" 必须走同一条规则，否则用户换个输入法就漏抽。
    顺带把连续空白压成一个空格——长度判断和正则都更稳定。
    """
    t = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", t).strip()


def _is_question(text: str, rules: RuleSet) -> bool:
    """只认结尾。句中疑问词（"是不是""什么"）不判——
    "我不确定这是不是真的" 是陈述，不该被挡。"""
    t = text.rstrip()
    if not t:
        return False
    return t.endswith(rules.question_enders)


def prescreen(
    text: str,
    *,
    entities: EntityIndex | None = None,
    prev_assistant: str = "",
    rules: RuleSet | None = None,
) -> PrescreenResult:
    """判断一句话是否值得抽取。

    prev_assistant 传上一轮 AI 的回复：如果它在提问，用户这轮多半在回答，
    而回答通常没有标记词（"叫豆豆，三岁了"）。
    """
    r = rules or load_rules()
    norm = _normalize(text)

    if not norm or _FILLER.match(norm):
        return PrescreenResult(False, "empty")

    # 计划优先，且不受长度限制（"8点叫我" 4 个字，但价值最高）
    for pat in r.plan:
        m = pat.search(norm)
        if m:
            return PrescreenResult(True, "plan", (m.group(0),))

    # 疑问句护栏必须在实体之前（见模块 docstring 第 3 条）
    if _is_question(norm, r):
        return PrescreenResult(False, "question")

    if entities is not None and len(norm) >= 3:
        hit = entities.match(norm)
        if hit:
            eid, word = hit
            return PrescreenResult(True, "entity", (word,))

    if len(norm) < r.min_chars:
        return PrescreenResult(False, "short")

    for pat in r.fact:
        m = pat.search(norm)
        if m:
            return PrescreenResult(True, "fact", (m.group(0),))

    for pat in r.preference:
        m = pat.search(norm)
        if m:
            return PrescreenResult(True, "preference", (m.group(0),))

    if r.answer_to_question and prev_assistant and _is_question(prev_assistant, r):
        # 回答也要有内容。"今天天气不错" 回答了"有什么想聊的吗"，
        # 但它是纯评价，抽出来也是 0 条事件——放行的代价是一次 API 调用
        # 和一个被占掉的窗口，收益是 0。
        if not _NO_INFO.search(norm):
            return PrescreenResult(True, "answer")

    return PrescreenResult(False, "none")


# ---------------------------------------------------------------- M29 启发式
# 真实对话（消费 / 技术 / 创作）里，用户很少说"我住在杭州"这种教科书式事实，
# 更多是"我是想2500以内""买二手没问题""价格太离谱"。词表一条都不中，
# 整段对话被挡——R-28.4 实测 120 轮真实对话里 95% 的窗口被挡。
#
# 启发式只做一件事：规则没命中时，判断这句话"像不像有信息"，宁可多抽。
# 抽取本身会返回 0 事件，代价是一次调用（约 ¥0.004/窗口），换覆盖率。


def prescreen_heuristic(
    text: str,
    *,
    entities: EntityIndex | None = None,
    prev_assistant: str = "",
    rules: RuleSet | None = None,
    min_chars: int = 8,
) -> PrescreenResult:
    """M29：规则 + 轻量信息量判断。

    顺序：
      1. 规则命中（plan/entity/fact/preference/answer） → 放
      2. 纯寒暄 / 纯评价                            → 挡
      3. 带数字且不太短（预算/型号/时间）            → 放
      4. 太短（< min_chars）                        → 挡
      5. 疑问句（够长）                             → 放（决策类问题含信息）
      6. 其余                                       → 放
    """
    r = rules or load_rules()
    norm = _normalize(text)
    if not norm or _FILLER.match(norm):
        return PrescreenResult(False, "empty")

    base = prescreen(text, entities=entities, prev_assistant=prev_assistant, rules=r)
    if base.passed:
        return base

    if _NO_INFO.match(norm):
        return PrescreenResult(False, "no_info")

    # 带数字的短句通常含具体信息："5500""r7000p""2026年"
    if any(ch.isdigit() for ch in norm) and len(norm) >= 4:
        return PrescreenResult(True, "numeric")

    if len(norm) < min_chars:
        return PrescreenResult(False, "short")

    if _is_question(norm, r):
        return PrescreenResult(True, "question_rich")

    return PrescreenResult(True, "heuristic")


# ---------------------------------------------------------------- 调试
def explain(text: str, *, entities: EntityIndex | None = None,
            prev_assistant: str = "", rules: RuleSet | None = None) -> str:
    """人看的解释：跑了哪些规则、耗时多少。CLI 用。"""
    r = rules or load_rules()
    t0 = time.perf_counter()
    res = prescreen(text, entities=entities, prev_assistant=prev_assistant, rules=r)
    us = (time.perf_counter() - t0) * 1_000_000
    verdict = "放行" if res.passed else "挡掉"
    matched = f"  ← {res.matched[0]!r}" if res.matched else ""
    return f"{verdict} [{res.reason}] {us:.0f}µs{matched}"
