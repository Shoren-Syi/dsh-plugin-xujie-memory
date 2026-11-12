# -*- coding: utf-8 -*-
"""M77 验收：遗忘 ≠ 丢失。

原则：权重只影响**日常排序/过滤**；用户明确回忆（"我以前…"）时必须能穿透权重下限。
背景：`retrieve.min_weight` 以前是个死配置（全仓没人读），
现在真正生效，并且只作用于隐式查询。
"""
from __future__ import annotations

from hippo.core.retrieve import l1_candidates, parse_query
from hippo.core.store import Store


def _seed(store: Store) -> int:
    store.add_turns("s", [("user", "我大学时候养过一只叫年糕的猫", 0)], ts=1_760_000_000)
    return store.add_event(
        session="s", raw="我大学时候养过一只叫年糕的猫",
        summary="用户大学时养过一只叫年糕的猫", keywords=["年糕", "猫"], importance=2,
        event_time=1_760_000_000, w0=0.05, weight=0.05)


def test_low_weight_event_is_filtered_for_implicit_query(store, cfg):
    store.cfg = cfg
    store.retrieve_cfg = cfg.retrieve
    eid = _seed(store)
    q = parse_query("年糕是什么猫", "s", store)
    hit = l1_candidates(store, "s", q)
    assert eid not in hit.event_ids, "日常查询不该被 0.05 权重的旧事占据"


def test_low_weight_event_reachable_by_explicit_recall(store, cfg):
    store.cfg = cfg
    store.retrieve_cfg = cfg.retrieve
    eid = _seed(store)
    q = parse_query("我以前是不是养过猫", "s", store)
    hit = l1_candidates(store, "s", q)
    assert eid in hit.event_ids, "显式回忆必须能穿透权重下限"


def test_floor_zero_disables_filter(store, cfg):
    store.cfg = cfg
    store.retrieve_cfg = cfg.retrieve
    cfg.retrieve.min_weight = 0.0
    eid = _seed(store)
    q = parse_query("年糕是什么猫", "s", store)
    hit = l1_candidates(store, "s", q)
    assert eid in hit.event_ids
