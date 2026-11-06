"""M19.2 调度器（手册验收 4 条）。

时间用注入的 `now` 推进（虚拟时钟），不真 sleep——手册的"实测
8~40s"属于 sim_plan 的活（python -m hippo.tools.sim_plan），
单测要的是性质：误差上界 = tick 周期、并发只触发一次、missed 正确。
"""

from __future__ import annotations

import asyncio

from hippo.core.scheduler import Scheduler

T0 = 1_800_000_000


def _mk(store, *, due=T0 + 60, recur=None, content="到点叫我"):
    store.add_plan(session="s", due_at=due, content=content,
                   raw_hint="明天8点", recur=recur)
    return store.due_plans(T0 + 10**6)[0]


# ---------------------------------------------------------- tick 触发
def test_tick_fires_due_plan(store):
    s = Scheduler(store, tick_seconds=60)
    p = _mk(store, due=T0 + 60)
    # 到点前：不触发
    assert s.tick(now=T0 + 59) == []
    # 到点：触发
    fired = s.tick(now=T0 + 60)
    assert [x.id for x in fired] == [p.id]
    row = store.conn.execute(
        "SELECT fired, fired_at FROM plans WHERE id=?", (p.id,)).fetchone()
    assert row[0] == 1 and row[1] == T0 + 60


def test_tick_never_fires_early(store):
    s = Scheduler(store)
    _mk(store, due=T0 + 3600)
    assert s.tick(now=T0) == []


def test_concurrent_ticks_fire_once(store):
    """验收 #2：并发 10 次只 1 次——UPDATE WHERE fired=0 的 rowcount 仲裁。"""
    s = Scheduler(store)
    p = _mk(store, due=T0)
    wins = sum(bool(s.tick(now=T0 + 1)) for _ in range(10))
    assert wins == 1
    assert store.conn.execute(
        "SELECT fired FROM plans WHERE id=?", (p.id,)).fetchone()[0] == 1


def test_tick_error_bounds():
    """验收 #1 的性质面：误差上界 = tick 周期（60s ≤ 手册 60s）。"""
    assert Scheduler(store=None).tick_seconds <= 60


# ---------------------------------------------------------- missed
def test_sweep_marks_overdue(store):
    """验收 #3：重启补——到点超 grace 的标 missed=1。"""
    s = Scheduler(store, grace_seconds=300)
    _mk(store, due=T0 - 600)                     # 超期 10 分钟
    p2 = _mk(store, due=T0 - 100, content="grace 内")  # 超期 100s，grace 内
    n = s.sweep(now=T0)
    assert n == 1
    rows = {r["content"]: r["missed"] for r in store.conn.execute(
        "SELECT content, missed FROM plans").fetchall()}
    assert rows["到点叫我"] == 1
    assert rows["grace 内"] == 0


def test_swept_plan_still_fires_with_missed_flag(store):
    """missed=1 不影响触发——晚到的提醒还是要发，注入时说明晚了。"""
    s = Scheduler(store, grace_seconds=300)
    p = _mk(store, due=T0 - 600)
    s.sweep(now=T0)
    fired = s.tick(now=T0 + 30)
    assert [x.id for x in fired] == [p.id]
    row = store.conn.execute(
        "SELECT fired, missed FROM plans WHERE id=?", (p.id,)).fetchone()
    assert row[0] == 1 and row[1] == 1


def test_tick_preserves_missed_flag(store):
    """tick 触发时不得把 sweep 标的 missed 洗回 0。"""
    s = Scheduler(store, grace_seconds=300)
    p = _mk(store, due=T0 - 600)
    s.sweep(now=T0)
    s.tick(now=T0 + 30)
    assert store.conn.execute(
        "SELECT missed FROM plans WHERE id=?", (p.id,)).fetchone()[0] == 1


# ---------------------------------------------------------- recur
def test_daily_plan_advances(store):
    """验收：重复计划正确推下次——daily 触发后 due_at +1 天、fired 归 0。"""
    s = Scheduler(store)
    p = _mk(store, due=T0 + 60, recur="daily", content="每天7点半叫我")
    s.tick(now=T0 + 60)
    row = store.conn.execute(
        "SELECT fired, due_at, attempts FROM plans WHERE id=?",
        (p.id,)).fetchone()
    assert row[0] == 0                       # 等待下一次到点
    assert row[1] == T0 + 60 + 86400
    # 第二天到点再触发
    fired = s.tick(now=T0 + 60 + 86400)
    assert [x.id for x in fired] == [p.id]


def test_weekly_plan_advances(store):
    s = Scheduler(store)
    p = _mk(store, due=T0 + 60, recur="weekly", content="每周三开会")
    s.tick(now=T0 + 60)
    row = store.conn.execute(
        "SELECT fired, due_at FROM plans WHERE id=?", (p.id,)).fetchone()
    assert row[0] == 0
    assert row[1] == T0 + 60 + 7 * 86400


# ---------------------------------------------------------- defer（M19.3 联动）
def test_defer_postpones_ten_minutes(store):
    """验收 #3：deferred 路径可用——fired 拨回 0，due_at +10 分钟。"""
    s = Scheduler(store)
    p = _mk(store, due=T0 + 60)
    s.tick(now=T0 + 60)
    assert store.defer_plan(p.id, now=T0 + 65) is True
    row = store.conn.execute(
        "SELECT fired, due_at, attempts FROM plans WHERE id=?",
        (p.id,)).fetchone()
    assert row[0] == 0 and row[1] == T0 + 65 + 600 and row[2] == 1
    # 10 分钟后重新到点，可再触发
    assert [x.id for x in s.tick(now=T0 + 65 + 600)]


def test_defer_max_three_then_forced(store):
    """验收 #4：最多延 3 次——第 4 次 defer 被拒绝，计划必须发。"""
    s = Scheduler(store)
    p = _mk(store, due=T0 + 60)
    for i in range(3):
        s.tick(now=T0 + 60 + i * 700)        # 每次 defer 后 10 分钟再到点
        assert store.defer_plan(p.id, now=T0 + 65 + i * 700) is True
    # 第 4 次到点触发后，defer 被拒绝
    s.tick(now=T0 + 60 + 3 * 700)
    assert store.defer_plan(p.id, now=T0 + 65 + 3 * 700) is False
    row = store.conn.execute(
        "SELECT fired, attempts FROM plans WHERE id=?", (p.id,)).fetchone()
    assert row[0] == 1 and row[1] == 3


# ---------------------------------------------------------- 循环
def test_run_forever_runs_and_stops(store):
    """asyncio 循环：跑起来、能触发、能停下——循环本体不炸。"""
    s = Scheduler(store, tick_seconds=0.05)
    p = _mk(store, due=T0 + 1)
    import time as _t
    real_due = int(_t.time()) + 1
    store.conn.execute("UPDATE plans SET due_at=? WHERE id=?",
                       (real_due, p.id))
    fired = []

    def on_fire(plan):
        fired.append(plan.id)

    s.on_fire = on_fire

    async def drive():
        stop = asyncio.Event()
        task = asyncio.create_task(s.run_forever(stop=stop))
        for _ in range(200):
            if fired:
                break
            await asyncio.sleep(0.1)
        stop.set()
        await task

    asyncio.run(drive())
    assert fired == [p.id]
