# -*- coding: utf-8 -*-
"""M44 验收：让模型给事件打重要性，插件只负责摘要/关键词/落库。

设计意图：什么重要，模型在抽取时看一眼就知道；插件不该用启发式硬猜。
重要性只影响**衰减起点**和**重点标注层排序**，不改变衰减公式——重要的事
忘得慢，但不会"永不遗忘"。
"""
from __future__ import annotations

from hippo.core.persist import persist_result
from hippo.core.schema import EventOut, ExtractResult
from hippo.core.store import Store
from hippo.core.turns import Window


def _window(store: Store, session: str = "s") -> Window:
    turns = store.get_turns(session, limit=4)
    return Window(session=session, idxs=[t.idx for t in turns], token_est=0,
                  has_pending=False, turns=turns)


def _add_turns(store: Store) -> None:
    store.add_turns("s", [("user", "我下个月要辞职，去杭州", 0)], ts=1_760_000_000)


def test_schema_accepts_model_importance():
    ev = EventOut(raw="我下个月要辞职", summary="用户下个月辞职", importance=5)
    assert ev.importance == 5
    assert EventOut(raw="x").importance == 3, "没给就是默认 3"


def test_persist_stores_importance_and_raises_weight(store):
    _add_turns(store)
    persist_result(store, "s", _window(store), ExtractResult(events=[
        EventOut(raw="我下个月要辞职，去杭州", summary="用户下个月辞职去杭州",
                 keywords=["辞职", "杭州"], importance=5)]), now=1_760_000_100)
    ev = store.conn.execute("select importance, weight, w0 from events").fetchone()
    assert ev["importance"] == 5
    assert ev["w0"] > 0.5, "重要的事衰减起点应该更高"


def test_low_importance_lowers_weight(store):
    _add_turns(store)
    persist_result(store, "s", _window(store), ExtractResult(events=[
        EventOut(raw="我下个月要辞职，去杭州", summary="用户下个月辞职去杭州",
                 keywords=["辞职"], importance=1)]),
        now=1_760_000_100)
    ev = store.conn.execute("select importance, w0 from events").fetchone()
    assert ev["importance"] == 1
    assert ev["w0"] < 0.5


def test_importance_is_clamped(store):
    _add_turns(store)
    persist_result(store, "s", _window(store), ExtractResult(events=[
        EventOut(raw="我下个月要辞职，去杭州", summary="辞职", keywords=["辞职"], importance=9)]),
        now=1_760_000_100)
    assert store.conn.execute("select importance from events").fetchone()[0] == 5


def test_highlight_prefers_importance_over_weight(store):
    """重点标注层先看"重不重要"，再看权重/时间。"""
    old_important = store.add_event(session="s", raw="我妈住院了", summary="用户妈妈住院",
                                    keywords=["住院"], event_time=1_760_000_000,
                                    importance=5, weight=0.30, w0=0.30)
    recent_trivial = store.add_event(session="s", raw="改了个变量名", summary="用户改了个变量名",
                                     keywords=["变量"], event_time=1_760_090_000,
                                     importance=2, weight=0.95, w0=0.95)
    picked = store.highlight_events("s", limit=2)
    assert [e.id for e in picked][0] == old_important, "重要的事要排在琐事前面"
    assert recent_trivial in [e.id for e in picked]
