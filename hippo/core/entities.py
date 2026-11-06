"""实体归一（M7.1）。

问题不在于"怎么算相似"，而在于**合并是不可逆的**。

`小李` 和 `老李` 的字符重合度 1/3、拼音首字母都是 L、长度相同、
在中文语境里都可能指代某位同事。任何"相似度阈值"只要落在这个区间，
就会把两个人粘成一个人；粘完之后 traits、facts、event_entities 全迁过去，
用户在界面上看到的是"它把我同事记成我老婆了"，而且没有撤销按钮。

所以这一层的设计原则是 **宁可多一个实体，不可错合并一次**：

    完全相等 / 别名命中        → 直接合并（几乎不可能错）
    规范化后相等               → 直接合并（"隔壁老王"→"老王"）
    高度相似但不确定            → **不合并**，建新实体 + 记 doubt
    其余                       → 建新实体

"记 doubt" 是这里唯一聪明的部分：它把判断权交还给用户，
而 doubt 本身很便宜（一行 type='entity' 的疑问，检索到时才渲染）。

第二个坑是 trigram 在中文短名上几乎没有区分度：
`老王` 的 3-gram 是 {"^老王", "老王$"}，`老李` 是 {"^老李", "老李$"}，交集为空。
而 `王建国`/`王建军` 只有 0.2。**所以 0.8 阈值实际上只对
"同一串名字的不同写法"生效**，指望它自动认出"王哥=王建国"是不现实的。
真正的合并来源是模型给的 aliases（prompt 第 9 条），这里只做兜底。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = [
    "normalize_name", "surname_of", "trigram_jaccard", "similarity",
    "Match", "decide", "AUTO_MERGE", "SUSPECT", "NEW",
]

# 前缀修饰：只去掉"隔壁""我们公司"这类限定语，不去"老/小/阿"——
# "老王"的"老"是称呼的一部分，"王"才是姓。
_PREFIXES = (
    "隔壁的", "隔壁", "我们家的", "我们家", "我家的", "我家",
    "我们公司的", "我们公司", "公司的", "我们", "我的",
    # 模型会把"用户的妈妈"当成正名写进来，渲染时就成了"用户的妈妈是什么性格"
    "用户的", "用户",
)
# 后缀尊称："王建国老师" 和 "王建国" 是同一人；"王老师" 去掉后只剩 1 字，不动。
_SUFFIXES = ("老师", "先生", "女士", "同学", "经理", "总监", "主任", "老板")

_PUNCT = "，。、,.!?！？:：;；\"'“”‘’()（）[]【】《》<>·-—_/\\|"

AUTO_MERGE = "merge"
SUSPECT = "doubt"
NEW = "new"


def normalize_name(name: str) -> str:
    """把同一个人的不同写法收敛到一个字符串。不改变语义，只去装饰。"""
    s = unicodedata.normalize("NFKC", str(name or ""))
    s = re.sub(r"[\s\u3000]+", "", s)
    s = s.strip(_PUNCT)
    # 只认**最长**匹配，命中即停。
    # 第一版是"命中就 break"，但排在前面的是更长的"用户的"，
    # "用户的猫" 去掉前缀只剩 1 字、不够 2 字，于是跳过、继续试"用户"，
    # 结果得到"的猫"——一个永远匹配不上的实体。
    for p in sorted(_PREFIXES, key=len, reverse=True):
        if s.startswith(p):
            if len(s) - len(p) >= 2:
                s = s[len(p):]
            break
    for suf in sorted(_SUFFIXES, key=len, reverse=True):
        if s.endswith(suf):
            if len(s) - len(suf) >= 2:
                s = s[:-len(suf)]
            break
    return s


def surname_of(name: str) -> str:
    """粗糙的姓氏提取。只用于"可疑"判定，不用于合并，错了也不致命。"""
    s = normalize_name(name)
    if not s:
        return ""
    if s[0] in "老小阿" and len(s) >= 2:
        return s[1]
    return s[0]


def _grams(s: str) -> set[str]:
    t = f"^{s}$"
    if len(t) <= 3:
        return {t}
    return {t[i:i + 3] for i in range(len(t) - 2)}


def trigram_jaccard(a: str, b: str) -> float:
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return inter / len(ga | gb)


def similarity(a: str, b: str) -> float:
    """归一化后再比。相等直接 1.0，避免短名 trigram 退化成 0。"""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return trigram_jaccard(na, nb)


def _one_char_off(a: str, b: str) -> bool:
    """同姓、同长、只差一个字。典型：王建国 / 王建军。"""
    na, nb = normalize_name(a), normalize_name(b)
    if len(na) != len(nb) or len(na) < 2:
        return False
    if surname_of(na) != surname_of(nb):
        return False
    return sum(1 for x, y in zip(na, nb) if x != y) == 1


@dataclass(frozen=True)
class Match:
    action: str          # merge / doubt / new
    target_id: int | None
    score: float
    reason: str


def decide(
    name: str,
    type_: str,
    existing: list[tuple[int, str, str, list[str]]],
    *,
    aliases: list[str] | None = None,
    merge_threshold: float = 0.8,
    doubt_threshold: float = 0.6,
) -> Match:
    """给一个待写入的实体名，决定"挂到谁身上 / 新建 / 新建但要问一句"。

    existing: [(id, canonical, type, aliases), ...]，只含同 session 的实体。
    aliases:  **这一次**提到的别名。必须参与比较：
              模型说"张敏，也叫小张"，而库里已经有"小张"——那是同一个人，
              但只看 canonical 会判成两个。这个方向漏了，别名就白给了。
    """
    n = normalize_name(name)
    if not n:
        return Match(NEW, None, 0.0, "empty")

    incoming = {n} | {normalize_name(a) for a in (aliases or []) if normalize_name(a)}

    best: tuple[float, int | None, str, str] = (0.0, None, "", "")

    for eid, canonical, etype, ex_aliases in existing:
        if etype != type_:
            # 类型不同不合并，也不记 doubt："朝阳"是地名还是人名，
            # 问用户"这两个是不是同一个人"没有意义——名字撞车而已。
            continue
        cand = [canonical, *ex_aliases]
        for c in cand:
            if c and normalize_name(c) in incoming:
                return Match(AUTO_MERGE, eid, 1.0, "exact")

        # 这里曾经有一条 "前缀包含" 规则：nc.startswith(n) 就合并。
        # 它能把 "李娜" 合到 "李娜娜"（对），也能把 "张伟" 合到 "张伟明"（错）。
        # 中文昵称确实常是加字，但母女、父子、同事同姓也常是加字，
        # 而合并不可逆 —— 删掉。真昵称由模型写进 aliases，走 exact 那条路。

        score = max(
            (similarity(x, c) for x in incoming for c in cand if c),
            default=0.0,
        )
        if score > best[0]:
            best = (score, eid, canonical, "sim")

    score, eid, canonical, why = best
    if eid is not None and score >= merge_threshold:
        return Match(AUTO_MERGE, eid, score, why)

    # 可疑但不动手：同姓同长差一字，或相似度落在灰区
    for cand_id, canonical, etype, ex_aliases in existing:
        if etype != type_:
            continue
        names = [canonical, *ex_aliases]
        if any(
            _one_char_off(x, c)
            for x in incoming
            for c in names
            if c and x
        ):
            return Match(SUSPECT, cand_id, max(score, 0.5), "one_char_off")
    if eid is not None and score >= doubt_threshold:
        return Match(SUSPECT, eid, score, "grey_zone")

    return Match(NEW, None, score, "no_match")
