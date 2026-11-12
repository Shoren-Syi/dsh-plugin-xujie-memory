# -*- coding: utf-8 -*-
"""M75 验收：心跳真的在插件里跑起来了。

背景：`core/heartbeat.py` 自己写着"宿主在 lifespan 里起一个 task"，
但插件从来没起过——plans/doubts/mood/tier/consolidate/curiosity 六个定时步骤
全部是死功能。现在内核按 `heartbeat.interval_seconds` 跑 tick。
"""
from __future__ import annotations

from hippo.config import Config
from hippo.core.heartbeat import Heartbeat


def test_tick_runs_and_writes_last_tick(store, cfg):
    store.add_turns("s", [("user", "下周三记得提醒我去体检", 0)], ts=1_760_000_000)
    hb = Heartbeat(store, cfg=cfg)
    rep = hb.tick(now=1_760_000_100)
    assert rep.steps, "应至少记录六步"
    names = {s.name for s in rep.steps}
    assert {"plans", "doubts", "mood", "tier", "consolidate", "curiosity"} <= names
    last = store.get_state("_sys", "hb:last_tick", "")
    assert last, "心跳必须写 last_tick，否则排查不了'心跳死了'"
    assert hb.counts.get("llm_calls", 0) == 0, "本地 tick 不应产生 LLM 调用"


def test_tick_is_cheap_and_idempotent(store, cfg):
    hb = Heartbeat(store, cfg=cfg)
    hb.tick(now=1_760_000_100)
    hb.tick(now=1_760_000_160)
    assert store.get_state("_sys", "hb:last_tick", "") == "1760000160"


def test_heartbeat_interval_config_default():
    cfg = Config()
    assert cfg.heartbeat.interval_seconds >= 15, "太短的 tick 会白耗 CPU"
