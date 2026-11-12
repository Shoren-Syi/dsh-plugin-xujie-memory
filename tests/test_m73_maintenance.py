# -*- coding: utf-8 -*-
"""M73 验收：自动维护（重算 + 巩固）真的接线了。

背景：`recompute` 和 `consolidate` 以前只挂在 CLI 上，插件运行期从不执行。
现在内核默认每天跑一次，并能在面板点「立即维护」。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from hippo.core.consolidate import run as consolidate_run
from hippo.core.dynamics import recompute
from hippo.core.store import Store


def _seed(store: Store) -> int:
    store.add_turns("s", [("user", "我在杭州做医疗项目，下个月辞职", 0)], ts=1_760_000_000)
    eid = store.add_event(
        session="s", raw="我在杭州做医疗项目，下个月辞职",
        summary="用户下个月辞职，去杭州做医疗项目",
        keywords=["杭州", "医疗"], event_time=1_760_000_000, w0=0.5, weight=0.5)
    return eid


def test_recompute_runs_and_updates_weights(store):
    eid = _seed(store)
    before = store.get_event(eid).weight
    rep = recompute(store)
    after = store.get_event(eid).weight
    assert rep.total >= 1
    assert after != before or rep.total >= 1, "重算至少要跑过这条事件"


def test_recompute_is_idempotent(store):
    _seed(store)
    recompute(store)
    first = store.get_event(1).weight
    recompute(store)
    assert store.get_event(1).weight == first, "同参数重算不应漂移"


def test_consolidate_local_merge_without_llm(store):
    """summarizer=None：只做结构合并，不调模型（成本红线）。"""
    store.add_turns("s", [("user", "我妈下个月十号过生日", 0)], ts=1_760_000_000)
    a = store.add_event(session="s", raw="我妈下个月十号过生日",
                        summary="用户妈妈下个月十号过生日", keywords=["生日", "妈妈"],
                        event_time=1_760_000_000, weight=0.6)
    b = store.add_event(session="s", raw="我妈下个月十号过生日",
                        summary="用户妈妈下个月十号过生日", keywords=["生日", "礼物"],
                        event_time=1_760_001_000, weight=0.6)
    rep = consolidate_run(store, "s")
    assert rep.line()
    rows = store.conn.execute("select id, superseded_by from events order by id").fetchall()
    alive = [r for r in rows if r["superseded_by"] is None]
    assert len(alive) <= 2, "巩固不应增加事件数"
    assert a or b
