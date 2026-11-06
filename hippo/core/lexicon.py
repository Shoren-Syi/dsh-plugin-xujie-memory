"""查询侧分词（M10.1）。

## 为什么需要它

`kw_index` 里存的是**词**（"搬家""房租"），用户给的是**句子**
（"我上次搬家那个事"）。倒排索引只会精确匹配 token，句子直接拿去查，
一个也命中不了。

中文没有空格，不装分词器的话，唯一的机械解法是 n-gram：把句子切成
长度 2~4 的所有连续片段，拿去倒排里查。垃圾片段（"次搬""家那"）不会
命中任何 token，代价只是多几个占位符，**不会产生错误召回**——
在检索层，"不会静默出错"比"省一点"重要得多。

## 为什么不用 jieba

1. 依赖。用户要"即插即用"，一个纯 Python 分词器加词典是 30MB
2. 分词错一次就漏一条真记忆，而且漏得无声无息；n-gram 不会漏，它宁可多查
3. n-gram 的噪声可以用倒排的"命中数"自然消化，分词错误不能

## 代价与对策

一句 12 字的话出 ~30 个片段。SQLite 的占位符上限是 999，扣掉 L1
候选集的 800，只剩 199——所以 `limit` 卡在 48。

**截断顺序是刻意的**：2 字、3 字片段先保，4 字片段后给。
短片段是召回主力（中文词以 2 字为主），长片段是精度（"字节跳动"）。
反过来截，会把"搬家"砍掉留下"我上次搬"——那一个都命中不了。
"""

from __future__ import annotations

import re
import unicodedata

from .keywords import GENERIC, STOPWORDS

__all__ = ["query_tokens", "MAX_QUERY_TOKENS"]

# 占位符预算：SQLite 默认 999。L1 候选集最多 800，留 150 给查询片段，
# 剩 ~49 的余量给别处（历史标志、limit 等）。超了这个数不是"更准"，是抛错。
MAX_QUERY_TOKENS = 48

# 片段长度上限。5 字以上的片段几乎不可能是一个 kw_index token
# （clean_keywords 虽然放到 12 字，但模型给的关键词极少超过 4 字），
# 生成它们只是白占预算。
MAX_GRAM = 4

# 整段直通的上限：≤6 字的整段也当一个 token 试一次。
# 覆盖"西红柿炒蛋""量子力学"这种模型可能原样写进关键词的长词。
WHOLE_KEEP = 6

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
# 英文/数字串。`.+_-` 是型号（"iphone15"、"gpt-4"）里常见的连接符。
_ASCII = re.compile(r"[a-z0-9][a-z0-9._+-]*")
_ONLY_NUM = re.compile(r"^\d+(\.\d+)?$")


def _is_noise(g: str) -> bool:
    """这个片段是否肯定没有检索价值：它自己就是停用词/泛词。

    **只用这一条判据**。曾经加过一条"两个字都是单字停用词就砍"，
    想法是"我几""在那"这类片段数量最多、最不可能命中，是预算的主要浪费源。
    实测被它误杀的是"最后"（最/后都在单字停用词表里）——
    "我拖到最后一刻"因此一个 token 也命中不了。

    停用词表是按**单字**建的（"上""下""最""后"都是），两字拼起来
    是不是词，单字表答不了。所以现在不砍，改成"排序时放到最后"
    （见 query_tokens）：预算不够时先丢它们，预算够就让它们照常参与。
    """
    return g in STOPWORDS or g in GENERIC


def _is_weak(g: str) -> bool:
    """弱片段：两个字都是单字停用词。不砍，只降优先级。"""
    return len(g) == 2 and g[0] in STOPWORDS and g[1] in STOPWORDS


def _grams(chunk: str) -> tuple[list[str], list[str], list[str]]:
    """一段连续中文 → (2-gram, 3-gram, 4-gram)。分层返回，方便按预算截断。"""
    n = len(chunk)
    out: list[list[str]] = [[], [], []]
    if n < 2:
        return out[0], out[1], out[2]
    for size in (2, 3, 4):
        if size > n:
            break
        bucket = out[size - 2]
        for i in range(n - size + 1):
            g = chunk[i:i + size]
            if not _is_noise(g):
                bucket.append(g)
    return out[0], out[1], out[2]


def query_tokens(text: str, *, limit: int = MAX_QUERY_TOKENS) -> list[str]:
    """一句话 → 查询 token 集。长片段后置，短片段先保。

    返回的列表按"重要性"排序（2 字 → 3 字 → 4 字 → 整段），
    调用方截断时直接取前 limit 个即可。
    """
    src = unicodedata.normalize("NFKC", text or "").lower().strip()
    if not src:
        return []

    g2: list[str] = []
    g3: list[str] = []
    g4: list[str] = []
    weak: list[str] = []
    whole: list[str] = []
    ascii_words: list[str] = []

    for m in _CJK.finditer(src):
        chunk = m.group()
        a, b, c = _grams(chunk)
        # 弱片段从 g2 里摘出来单独排队尾。不摘的话 dict.fromkeys 去重时
        # 保留的是第一次出现的位置，降级就白做了。
        g2.extend(g for g in a if not _is_weak(g))
        g3.extend(b)
        g4.extend(c)
        weak.extend(g for g in a if _is_weak(g))
        if 2 <= len(chunk) <= WHOLE_KEEP:
            whole.append(chunk)

    for m in _ASCII.finditer(src):
        t = m.group().strip("._+-")
        if len(t) >= 2 and not _ONLY_NUM.match(t):
            ascii_words.append(t)

    # 顺序 = 召回优先级。整段排在 4-gram 之前：它更长、更 specific，
    # 而且一个整段命中胜过四个碎片命中。
    merged = [*g2, *g3, *ascii_words, *whole, *g4, *weak]

    seen: set[str] = set()
    out: list[str] = []
    for t in merged:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out
