# -*- coding: utf-8 -*-
"""事件去重：同一件事被抽成两条，是记忆"越用越脏"的最大来源。

真实成因（实测）：窗口里同时有用户原话和助手复述，模型会为两句话各产出一条
事件，摘要几乎一样（"用户下个月辞职，去杭州…" / "用户下个月将辞职，去杭州…"）。
"""
from __future__ import annotations

import re
import unicodedata

__all__ = ["normalize", "similarity", "is_duplicate"]

_PUNCT = re.compile(r"[\s，。、；：！？,.;:!?（）()「」『』\"'“”‘’—…·]+")
_DROP = {"用户", "说", "表示", "提到", "称", "让", "把", "了", "的", "在", "要", "会"}


def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    s = _PUNCT.sub("", s)
    return s


def _bigrams(text: str) -> set[str]:
    norm = normalize(text)
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard。中文上比词级稳，短句也能算。"""
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def _contained(a: str, b: str, *, ratio: float = 0.7) -> bool:
    """短的几乎是长的一部分（"用户辞职" vs "用户辞职去杭州"）。"""
    x, y = normalize(a), normalize(b)
    if not x or not y:
        return False
    short, long = (x, y) if len(x) <= len(y) else (y, x)
    if len(short) / len(long) < ratio:
        return False
    return short in long


def is_duplicate(summary: str, others: list[str], *, threshold: float = 0.55) -> bool:
    """和其它若干条摘要比较，任意一条足够像就算重复。

    阈值 0.55 是实测定的，不是拍的：
      - 真实重复对（"辞职…去杭州…涨四千" / "辞职…将去杭州…涨四千"）= 0.607
      - 真正不同的事（"住在杭州" / "去北京出差三天"）≈ 0.1~0.35
    0.62 会把 0.607 那对漏掉（第一版就漏了），所以放到 0.55；
    再加一条"包含关系"——短句几乎整句出现在长句里也算重复。
    宁可漏删不可错删：错删等于丢记忆。
    """
    if not normalize(summary):
        return False
    for other in others:
        if similarity(summary, other) >= threshold or _contained(summary, other):
            return True
    return False
