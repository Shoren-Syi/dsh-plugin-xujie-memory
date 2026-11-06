"""关键词清洗（M6.1）。

关键词不是给人看的标签，是**检索的唯一入口**。2 字中文词（"搬家""房租"）
进不了 trigram（只索引 ≥3 字符），FTS5 捞不到，只能靠 kw_index。
所以关键词质量直接决定"三个月后能不能想起这件事"。

三个失败模式，各自对应一段代码：

1. **空词**：模型给"事情""问题""一些"。它们能匹配上所有事件，等于没检索。
2. **太长**：模型给"用户昨天下午从朝阳搬到海淀"。它只匹配这一条，
   检索价值等于 0，还占掉 8 个名额里的一个。
3. **落单字**："猫""狗"被长度规则砍掉。但那是用户养的猫。

清洗只做确定的事：查表、量长度、去重、截断。**不做语义判断**——
"这个词对这条记忆重要吗"是模型该回答的，规则法答不了（M4 已经撞过这堵墙）。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Sequence

# 单条事件最多留几个。超过 8 个，kw_index 的倒排会开始互相打架：
# 一条事件命中全部 token，检索排序就退化成按 weight 排，关键词失去意义。
MAX_KEYWORDS = 8

# 虚词与代词。它们不是"停用词表"意义上的无意义，而是**在所有事件里都出现**，
# 所以没有区分度。80 条的规模是故意的：再长就会开始误杀真词。
STOPWORDS = frozenset("""
的 了 着 是 在 有 和 与 或 也 都 就 还 又 很 太 更 最 不 没 别 被 把 让 给 对 从 向 到
我 你 他 她 它 我们 你们 他们 她们 咱们 自己 别人 大家 人家
这 那 这个 那个 这些 那些 这样 那样 这么 那么 这里 那里 这边 那边
什么 怎么 怎样 为什么 哪个 哪些 多少 几 如何 是否
啊 吧 呢 吗 呀 哦 嗯 唉 哈 嘛 啦 呗 哇
一 一个 一下 一点 一些 一样 一直 一起 一般 一边
上 下 前 后 里 外 中 内 间 时 时候 之前 之后 以后 以前 现在 当时
而且 但是 因为 所以 如果 虽然 然后 不过 只是 就是 还是 或者 以及
可以 应该 需要 必须 可能 也许 大概 差不多 比较 非常 特别 真的 确实
说 说了 说道 做 做了 搞 弄 用 用了 想 想了 觉得 感觉 认为 知道 看到 听到
来 来了 去 去了 走 走了 回 回来 过来 起来 出来 进去
好 好的 行 可以 对 是的 没错 收到 明白 了解
""".split())

# 有字面长度但**没有区分度**的词。它们能匹配上几乎所有事件，
# 是 kw_index 里的噪声源。和 STOPWORDS 分开，因为语义不同：
# STOPWORDS 是语法上的虚词，这里是"语义上正确但检索上无用"。
#
# 刻意**不收**"工作""学习""生活""心情"这类词：它们是泛词，但确实是真话题
# （"我换工作了"），砍掉会伤召回。判断标准是"它指向对话本身，还是指向世界"。
GENERIC = frozenset("""
事情 事儿 问题 东西 情况 内容 方面 部分 相关 有关 其他 其它
一些 有点 有些 稍微 很多 不少 大量 少量
信息 消息 描述 说明 记录 对话 聊天 内容
""".split())

# 纯标点 / 纯数字 / 纯空白：既不是词，也没法当检索键。
# 分隔符必须包含空白：关键词既可能是数组，也可能是 "老王 跳槽 字节" 这种串。
# 少写一个 \s，整串就会被当成一个 12 字的长词——它照样入库、照样不报错，
# 只是再也检索不到。
_SEP = re.compile(r"[,，、;；\s]+")
_ONLY_PUNCT = re.compile(r"^[\W_]+$", re.UNICODE)
_ONLY_NUM = re.compile(r"^[\d.]+$")
# 首尾的标点与引号。模型常给「"搬家"」这种带引号的词。
_TRIM = " \t\r\n\"'“”‘’「」『』《》〈〉()（）[]【】{}<>,，.。!！?？:：;；、…-—_*#`~|/\\"


def _norm(text: str) -> str:
    """归一化：NFKC（全角→半角）+ 去首尾标点 + 中文不住空格。"""
    s = unicodedata.normalize("NFKC", (text or "").strip())
    s = s.strip(_TRIM)
    return re.sub(r"\s+", " ", s).strip()


def _reject(token: str, entities: set[str]) -> str:
    """返回拒绝原因；空串表示保留。抽出来是为了 explain() 能复用同一套判断。"""
    if not token:
        return "空"
    if token in entities:
        return ""                      # 实体名优先，长度和停用词都不管
    if token.lower() in STOPWORDS or token in STOPWORDS:
        return "停用词"
    if token in GENERIC:
        return "空词"
    if len(token) < 2:
        return "单字"
    if _ONLY_PUNCT.match(token):
        return "纯标点"
    if _ONLY_NUM.match(token):
        return "纯数字"
    if len(token) > 12:
        # 一句话被当成关键词。它不是检索键，是一条摘要的劣质副本。
        return "过长"
    return ""


def normalize_keywords(
    items: str | Sequence[str] | None,
    *,
    limit: int = 16,
) -> list[str]:
    """机械归一化：去标点、去空、去重、限长。**不做策略判断**。

    为什么单独拆出来：schema 层（解析模型输出）拿不到实体名，
    如果在那里就把"猫"按单字砍掉，等到 persist 知道"猫是用户养的猫"时
    已经没得救了。所以解析层只做机械活，策略留给有上下文的调用方。
    """
    if not items:
        return []
    raw = _SEP.split(items) if isinstance(items, str) else list(items)
    out, seen = [], set()
    # 数组元素内部也要拆："老王 跳槽" 这种粘在一起的元素，
    # 原样留着就是一个检索不到任何东西的 5 字长词。
    flat = [piece for item in raw for piece in _SEP.split(str(item))]
    for item in flat:
        t = _norm(str(item))
        if not t or t.lower() in seen or _ONLY_PUNCT.match(t):
            continue
        seen.add(t.lower())
        out.append(t)
        if len(out) >= limit:
            break
    return out


def clean_keywords(
    items: str | Sequence[str] | None,
    *,
    entities: Iterable[str] = (),
    limit: int = MAX_KEYWORDS,
) -> list[str]:
    """清洗并截断关键词。保持输入顺序（同类得分下先出现的优先）。

    截断策略：实体名 > 长词 > 短词 > 靠前的。理由是实体名和长词
    几乎不可能同时出现在几十条事件里，而"搬家""房子"这种短词会。
    """
    if not items:
        return []
    raw = normalize_keywords(items, limit=64)

    ents = {_norm(e) for e in entities if e}
    kept: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw):
        t = _norm(str(item))
        # 小写去重：AI / ai 在 kw_index 里是两条互不命中的倒排项，
        # 留着只会让同一条事件重复占名额。
        key = t.lower()
        if not t or key in seen:
            continue
        if _reject(t, ents):
            continue
        seen.add(key)
        score = 2.0 * (t in ents) + min(len(t), 4) / 4.0 - idx * 0.01
        kept.append((score, idx, t))

    if len(kept) > limit:
        kept = sorted(kept, key=lambda x: (-x[0], x[1]))[:limit]
    kept.sort(key=lambda x: x[1])
    return [t for _, _, t in kept]


def explain(items: str | Sequence[str], *, entities: Iterable[str] = ()) -> list[tuple[str, str]]:
    """每个词的命运：(词, 保留/拒绝原因)。给 `hippo why` 和调 prompt 用。"""
    if not items:
        return []
    raw = _SEP.split(items) if isinstance(items, str) else list(items)
    ents = {_norm(e) for e in entities if e}
    out, seen = [], set()
    for item in raw:
        t = _norm(str(item))
        if t.lower() in seen:
            out.append((t, "重复"))
            continue
        seen.add(t.lower())
        out.append((t, _reject(t, ents) or "保留"))
    return out


def is_empty_word(token: str, entities: Iterable[str] = ()) -> bool:
    """这个词是不是"空词"（停用词或泛词）。

    评测用：算"模型输出了多少空词"，必须在**清洗前**统计，
    否则清洗完永远是 0，指标就没有意义了。
    """
    t = _norm(token)
    if t in {_norm(e) for e in entities if e}:
        return False
    return t in GENERIC or t.lower() in STOPWORDS or t in STOPWORDS
