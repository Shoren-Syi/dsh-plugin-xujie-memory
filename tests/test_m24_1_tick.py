# -*- coding: utf-8 -*-
"""M24.1 心跳 tick 骨架。

验收：60s 周期稳定（抖动<2s）/ 单步异常不崩 / 各步频率正确 / 优雅退出<1s。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from hippo.config import Config
from hippo.core.heartbeat import Heartbeat

T0 = 1_760_000_000
DAY = 86400


def _hb(store, **kw) -> Heartbeat:
    cfg = Config()
    # 关掉"每天 3 点巩固"的偶发干扰：测试里的 T0 正好不是 3 点，
    # 但其他用例会把时间推到任意位置，显式固定更稳。
    cfg.heartbeat.consolidate_hour = 3
    return Heartbeat(store, cfg=cfg, **kw)


def test_six_steps_present(store):
    """六步都要出现在报告里，没到频率的标 ran=False（不是失败）。"""
    hb = _hb(store)
    rep = hb.tick(now=T0)
    assert [s.name for s in rep.steps] == [
        "plans", "doubts", "mood", "tier", "consolidate", "curiosity"]
    assert rep.errors == 0
    assert rep.get("plans").ran and rep.get("doubts").ran


def test_frequencies(store):
    """验收 #3：mood 每 5 分钟、tier 每 60 分钟、plans 每轮。

    连跑 10 分钟（每分钟一 tick）：plans 10 次、mood 2 次（0' 和 5'）、
    tier 1 次（0'）。频率错了不会报错，只会让心跳悄悄变贵。
    """
    hb = _hb(store)
    runs = {"plans": 0, "mood": 0, "tier": 0}
    for i in range(10):
        rep = hb.tick(now=T0 + i * 60)
        for name in runs:
            runs[name] += int(rep.get(name).ran)
    assert runs == {"plans": 10, "mood": 2, "tier": 1}


def test_single_step_failure_isolated(store):
    """验收 #2：mood 抛异常，其余五步照常跑，errors 记 1。"""
    hb = _hb(store)

    def boom(_now):
        raise RuntimeError("注入故障")

    hb._mood = boom          # type: ignore[method-assign]
    rep = hb.tick(now=T0)
    assert rep.errors == 1
    assert rep.get("mood").err.startswith("RuntimeError")
    assert rep.get("plans").ran and not rep.get("plans").err
    assert rep.get("doubts").ran and not rep.get("doubts").err


def test_all_steps_failing_never_raises(store):
    """六步全炸，tick 本身仍然返回报告——循环不许被一步搞死。"""
    hb = _hb(store)
    for name in ("_plans", "_doubts", "_mood", "_tier", "_consolidate", "_curiosity"):
        setattr(hb, name, lambda _now: (_ for _ in ()).throw(ValueError("x")))
    # 设在 03:00，让 consolidate 的每日闸门也放行，六步才会全炸
    t3 = int(time.mktime((2026, 1, 2, 3, 0, 0, 0, 0, -1)))
    rep = hb.tick(now=t3)
    assert rep.errors == 6
    assert len(rep.steps) == 6


def test_plans_fire_through_heartbeat(store):
    """心跳复用 M19.2 的调度器，不另造定时器。"""
    store.add_plan(session="s", due_at=T0 - 10, content="叫用户起床")
    hb = _hb(store)
    rep = hb.tick(now=T0)
    assert rep.get("plans").n == 1
    assert store.recent_fired_plans("s", now_ts=T0) != []


def test_doubts_expire_to_memory(store):
    """过期疑问在 tick 里转成 cold 事件（"我忘了"的体感来源）。"""
    store.add_doubt(session="s", question="用户说的「那边」是公司还是老家？",
                    expire_days=1)
    hb = _hb(store)
    rep = hb.tick(now=int(time.time()) + 2 * DAY)
    assert rep.get("doubts").n == 1
    kinds = [r["kind"] for r in store.conn.execute(
        "SELECT kind FROM events WHERE session = 's'").fetchall()]
    assert "doubt" in kinds


def test_mood_decays_and_stamps_updated(store):
    """情绪落库：值衰减 + updated 跟上（否则时间戳永远停在一年前）。"""
    store.add_turns("s", [("user", "今天真烦", 0)], ts=T0 - DAY)
    store.upsert_affect("s", updated=T0 - DAY, joy=95, anger=80)
    hb = _hb(store)
    rep = hb.tick(now=T0)
    assert rep.get("mood").n == 1
    a = store.get_affect("s")
    assert a.updated == T0
    assert a.anger < 80 and a.joy < 95


def test_daily_consolidate_only_once(store):
    """03:00 那一轮跑一次，同一天再 tick 不重复跑。"""
    t3 = int(time.mktime((2026, 1, 2, 3, 0, 0, 0, 0, -1)))
    hb = _hb(store)
    first = hb.tick(now=t3)
    second = hb.tick(now=t3 + 60)
    assert first.get("consolidate").ran
    assert not second.get("consolidate").ran


def test_curiosity_hook(store):
    """M25 没接时 0 次；接了就跑一次。不接不等于降级。"""
    hb = _hb(store)
    assert hb.tick(now=T0).get("curiosity").n == 0

    calls = []
    hb.on_curiosity = lambda: calls.append(1) or 2
    assert hb.tick(now=T0 + 60).get("curiosity").n == 2
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_period_stable_and_fast_stop(store):
    """验收 #1/#4：真实 asyncio 循环，抖动 <2s、stop 后 <1s 退出。"""
    hb = _hb(store, tick_seconds=1)
    stop = asyncio.Event()
    task = asyncio.create_task(hb.run_forever(stop=stop, max_ticks=3))
    await asyncio.wait_for(task, timeout=6)

    assert hb.counts["ticks"] == 3
    # 三轮各 1 秒，抖动应远小于 2 秒（实测几十毫秒）
    assert hb.last_report is not None and hb.last_report.jitter_ms < 2000

    # stop 立即生效
    stop2 = asyncio.Event()
    t0 = time.perf_counter()
    task2 = asyncio.create_task(hb.run_forever(stop=stop2))
    await asyncio.sleep(0.05)
    stop2.set()
    await asyncio.wait_for(task2, timeout=2)
    assert time.perf_counter() - t0 < 1.0
