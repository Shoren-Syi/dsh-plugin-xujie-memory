"""三层记忆 + 重点标注 + 遗忘曲线 + 模糊搜索。

这一层不重复造事实表，只把已有的东西**按使用方式**组织起来：

    语义记忆  states / person card / 锚点        “我知道什么”
    情节记忆  traces / summaries                “发生过什么”
    工作记忆  最近几句 + 未确认项                “现在在聊什么”

`FuzzySearch` 解决的是文字检索：FTS trigram 对 1–2 个字的查询无能，
LIKE 又只认完整子串。个人记忆量不大，直接对最近 N 条原文做字符二元组
相似度，短词、错字、语序不同都能捞回来。**它只圈候选，排序仍看权重。**
"""

from __future__ import annotations

import math
import re
from typing import Any

from .decay import Lifecycle

LAYERS = ("semantic", "episodic", "working")
_PUNCT = re.compile(r"[\s，。！？、；：“”‘’（）()\[\]【】,.!?;:\"'`~…—-]+")


def _normalize(text: str) -> str:
    return _PUNCT.sub("", (text or "").lower())


def _bigrams(text: str) -> set[str]:
    text = _normalize(text)
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def similarity(query: str, content: str) -> float:
    """0–1。字符二元组 Dice + 完整子串奖励；对短查询和错字都友好。"""
    q, c = _normalize(query), _normalize(content)
    if not q or not c:
        return 0.0
    if q in c:
        # 短查询命中整段：2 个字也给高分，但不至于压过更长的精确匹配。
        return min(1.0, 0.85 + 0.15 * (len(q) / max(len(c), 1)))
    qb, cb = _bigrams(q), _bigrams(c)
    if not qb or not cb:
        return 0.0
    dice = 2 * len(qb & cb) / (len(qb) + len(cb))
    # 中文里错一个字很常见（「奶茶」→「奶荣」）：二元组会全灭，
    # 所以单字重合也要给分，否则错一个字就搜不到。
    char_overlap = len(set(q) & set(c)) / max(len(set(q)), 1)
    return min(1.0, 0.65 * dice + 0.35 * char_overlap)


from functools import lru_cache


@lru_cache(maxsize=4096)
def _prepared(content: str) -> tuple[str, frozenset, frozenset]:
    c = _normalize(content)
    return c, frozenset(_bigrams(c)), frozenset(c)


def _similarity_prepared(q: str, qb: set, qchars: set, content: str) -> float:
    """与 `similarity()` 等价，只是查询侧已经预处理过。"""
    c, cb, cchars = _prepared(content or "")
    if not c:
        return 0.0
    if q in c:
        return min(1.0, 0.85 + 0.15 * (len(q) / max(len(c), 1)))
    if not qb or not cb:
        return 0.0
    dice = 2 * len(qb & cb) / (len(qb) + len(cb))
    char_overlap = len(qchars & cchars) / max(len(qchars), 1)
    return min(1.0, 0.65 * dice + 0.35 * char_overlap)


class FuzzySearch:
    def __init__(self, engine):
        self.engine = engine

    def search(self, scope_id: str, query: str, *, limit: int = 5,
               candidates: int = 800) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []
        rows = self.engine._conn().execute(
            "SELECT trace_id, content, recorded_at FROM traces"
            " WHERE scope_id=? AND erased_at IS NULL"
            " ORDER BY recorded_at DESC LIMIT ?",
            (scope_id, max(1, int(candidates))),
        ).fetchall()
        # v0.52：查询只预处理一次；内容的二元组按文本缓存（进程内 LRU）。
        # 每轮 800 条候选，旧版每条都重新规范化查询，单轮约 20ms。
        q = _normalize(query)
        if not q:
            return []
        qb = _bigrams(q)
        qchars = set(q)
        scored = []
        for row in rows:
            score = _similarity_prepared(q, qb, qchars, row["content"])
            if score >= 0.15:
                scored.append((score, row))
        if not scored:
            return []
        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[: max(limit * 3, limit)]
        weights = Lifecycle(self.engine).batch_priority(
            [row["trace_id"] for _, row in top]
        )

        def rank(item):
            score, row = item
            w = weights.get(row["trace_id"], {})
            # 相关度为主，权重/锚点只做同分时的次序。
            return (-(score + 0.1 * min(float(w.get("priority") or 0.0), 2.0)),
                    row["recorded_at"] or "")

        out = []
        for item in sorted(top, key=rank)[:limit]:
            score, row = item
            w = weights.get(row["trace_id"], {})
            out.append({
                "trace_id": row["trace_id"],
                "content": row["content"],
                "score": round(float(score), 4),
                "priority": w.get("priority"),
                "anchor": bool(w.get("anchor")),
                "importance": w.get("base"),
                "recorded_at": row["recorded_at"],
                "layer": "episodic",
            })
        return out


class Memory:
    """三层记忆的统一入口：看结构、标重点、看曲线、搜文字。"""

    def __init__(self, engine):
        self.engine = engine
        self.lifecycle = Lifecycle(engine)
        self.fuzzy = FuzzySearch(engine)

    # ------------------------------------------------------------ 三层
    def layers(self, scope_id: str, *, recent: int = 5) -> dict[str, Any]:
        conn = self.engine._conn()
        states = conn.execute(
            "SELECT COUNT(*) AS n FROM states WHERE scope_id=?", (scope_id,)
        ).fetchone()["n"]
        traces = conn.execute(
            "SELECT COUNT(*) AS n FROM traces WHERE scope_id=? AND erased_at IS NULL",
            (scope_id,),
        ).fetchone()["n"]
        try:
            summaries = conn.execute(
                "SELECT COUNT(*) AS n FROM summaries WHERE scope_id=?", (scope_id,)
            ).fetchone()["n"]
        except Exception:  # noqa: BLE001
            summaries = 0
        open_items = conn.execute(
            "SELECT COUNT(*) AS n FROM clarifications WHERE scope_id=? AND status='open'",
            (scope_id,),
        ).fetchone()["n"]
        recent_rows = conn.execute(
            "SELECT trace_id, content, recorded_at FROM traces"
            " WHERE scope_id=? AND erased_at IS NULL"
            " ORDER BY recorded_at DESC LIMIT ?",
            (scope_id, max(1, int(recent))),
        ).fetchall()
        anchors = self.lifecycle.anchored(scope_id)
        return {
            "scope_id": scope_id,
            "semantic": {"states": states, "anchors": len(anchors)},
            "episodic": {"traces": traces, "summaries": summaries},
            "working": {
                "recent": [dict(r) for r in recent_rows],
                "open_clarifications": open_items,
            },
            "anchored": [a["ref_id"] for a in anchors],
        }

    # ------------------------------------------------------------ 重点
    def mark(self, scope_id: str, trace_id: str, importance: float = 4.0,
             note: str = "") -> dict[str, Any]:
        trace = self.engine.get_trace(trace_id)
        if trace is None or trace.get("scope_id") != scope_id:
            raise ValueError("trace_not_found")
        if trace.get("erased_at"):
            raise ValueError("trace_erased")
        # 5.0 视为"锚定"：时间拿不走；其余是"淡得慢"。
        # 先写重要性（会建 lifecycle 行），再设锚点，顺序反了锚点会丢。
        info = self.lifecycle.set_importance(trace_id, importance, scope_id)
        if float(importance) >= 5.0:
            self.lifecycle.set_anchor(trace_id, True)
            info = self.lifecycle.get(trace_id)
        return {
            "trace_id": trace_id,
            "importance": info.get("base"),
            "priority": info.get("priority"),
            "anchor": bool(info.get("anchor")),
            "note": note,
        }

    def unmark(self, scope_id: str, trace_id: str) -> dict[str, Any]:
        trace = self.engine.get_trace(trace_id)
        if trace is None or trace.get("scope_id") != scope_id:
            raise ValueError("trace_not_found")
        self.lifecycle.set_anchor(trace_id, False)
        info = self.lifecycle.set_importance(trace_id, 1.0, scope_id)
        return {"trace_id": trace_id, "importance": info.get("base"),
                "priority": info.get("priority"), "anchor": False}

    def curve(self, scope_id: str, trace_id: str, points: int = 12) -> list[dict]:
        trace = self.engine.get_trace(trace_id)
        if trace is None or trace.get("scope_id") != scope_id:
            raise ValueError("trace_not_found")
        return self.lifecycle.curve(trace_id, points=max(2, min(60, int(points))))

    # ------------------------------------------------------------ 搜索
    def search(self, scope_id: str, query: str, *, limit: int = 5) -> list[dict]:
        return self.fuzzy.search(scope_id, query, limit=limit)
