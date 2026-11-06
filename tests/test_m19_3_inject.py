"""M19.3 到点注入格式（手册验收 4 条）。

注入端到端走真 pack()：scheduler tick 触发 → pack 的 E 段出现
[计划]。时序细节都在测试里钉死：fired 之后 due_plans 查不到，
靠 fired_at 的 10 分钟窗口注入。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from hippo.core.pack import pack
from hippo.core.scheduler import Scheduler

TZ = ZoneInfo("Asia/Shanghai")
# 2026-11-05 周四 08:00
NOW = datetime(2026, 11, 5, 8, 0, tzinfo=TZ)
NOW_TS = int(NOW.timestamp())
YESTERDAY = NOW_TS - 86400


def _mk_plan(store, *, due=None, content="叫用户起来干活",
             raw="明天8点叫我起来干活", recur=None):
    store.add_plan(session="s1", due_at=due or NOW_TS,
                   content=content, raw_hint=raw, recur=recur)
    return store.conn.execute(
        "SELECT id FROM plans ORDER BY id DESC LIMIT 1").fetchone()[0]


# ---------------------------------------------------------- 格式
def test_format_snapshot(store):
    """验收 #1：格式快照。时间 + 内容 + 原话三行。"""
    from hippo.core.pack import _fmt_plan
    pid = _mk_plan(store)
    store.mark_plan_fired(pid, now=NOW_TS)
    p = store.recent_fired_plans("s1", now_ts=NOW_TS)[0]
    text = _fmt_plan(p, NOW_TS)
    assert text == ("[计划] 现在是 08:00，到点了：叫用户起来干活\n"
                    "原话：「明天8点叫我起来干活」")


def test_format_late_note(store):
    """missed=1 时带上晚了多少分钟（手册 M19.2.5）。"""
    from hippo.core.pack import _fmt_plan
    pid = _mk_plan(store, due=NOW_TS - 25 * 60)
    store.sweep_missed_plans(grace_seconds=300, now=NOW_TS)
    store.mark_plan_fired(pid, missed=True, now=NOW_TS)
    p = store.recent_fired_plans("s1", now_ts=NOW_TS)[0]
    text = _fmt_plan(p, NOW_TS)
    assert "（这条晚了 25 分钟）" in text


def test_format_no_hint_no_quote_line(store):
    """没有原话就不放引用行——空行比空话好。"""
    from hippo.core.pack import _fmt_plan
    pid = _mk_plan(store, raw=None, content="开会")
    store.mark_plan_fired(pid, now=NOW_TS)
    p = store.recent_fired_plans("s1", now_ts=NOW_TS)[0]
    text = _fmt_plan(p, NOW_TS)
    assert "原话" not in text and "开会" in text


# ---------------------------------------------------------- pack 端到端
def test_fired_plan_injected_into_pack(store):
    """验收 #2：带上下文——触发后 pack 的动态段出现 [计划]。"""
    pid = _mk_plan(store)
    s = Scheduler(store)
    s.tick(now=NOW_TS)
    r = pack(store, "s1", "早", now=NOW, tz="Asia/Shanghai", use_cache=False)
    dyn = next(b for b in r.blocks if b.name == "dynamic")
    assert "[计划] 现在是 08:00" in dyn.text
    assert "叫用户起来干活" in dyn.text


def test_unfired_plan_not_injected(store):
    """没到点（或 scheduler 没触发）不注入——不是到点前就剧透。"""
    _mk_plan(store, due=NOW_TS + 3600)
    r = pack(store, "s1", "早", now=NOW, tz="Asia/Shanghai", use_cache=False)
    dyn = next(b for b in r.blocks if b.name == "dynamic")
    assert "[计划]" not in dyn.text


def test_deferred_plan_not_injected(store):
    """验收 #3：defer 路径——延后 10 分钟，这 10 分钟内不再注入。"""
    pid = _mk_plan(store, due=NOW_TS)
    s = Scheduler(store)
    s.tick(now=NOW_TS)
    assert store.defer_plan(pid, now=NOW_TS) is True
    r = pack(store, "s1", "早", now=NOW, tz="Asia/Shanghai", use_cache=False)
    dyn = next(b for b in r.blocks if b.name == "dynamic")
    assert "[计划]" not in dyn.text


def test_defer_max_three_then_injected(store):
    """验收 #4：最多延 3 次——第 4 次触发后必须出现在注入里。"""
    pid = _mk_plan(store, due=NOW_TS)
    s = Scheduler(store)
    for i in range(3):
        due = NOW_TS + i * 700
        store.conn.execute("UPDATE plans SET due_at=? WHERE id=?",
                           (due, pid))
        s.tick(now=due)
        assert store.defer_plan(pid, now=due) is True
    # 第 4 次：defer 拒绝，下一次 pack 必须带 [计划]
    store.conn.execute("UPDATE plans SET due_at=? WHERE id=?",
                       (NOW_TS + 3 * 700, pid))
    s.tick(now=NOW_TS + 3 * 700)
    assert store.defer_plan(pid, now=NOW_TS + 3 * 700) is False
    r = pack(store, "s1", "早", now=datetime.fromtimestamp(NOW_TS + 3 * 700 + 60, TZ),
             tz="Asia/Shanghai", use_cache=False)
    dyn = next(b for b in r.blocks if b.name == "dynamic")
    assert "[计划]" in dyn.text


def test_injection_window_expires_after_ten_minutes(store):
    """触发 11 分钟后不再注入——[计划] 段不是永久贴条。"""
    pid = _mk_plan(store, due=NOW_TS - 60)
    s = Scheduler(store)
    s.tick(now=NOW_TS)
    later = datetime.fromtimestamp(NOW_TS + 11 * 60, TZ)
    r = pack(store, "s1", "早", now=later, tz="Asia/Shanghai",
             use_cache=False)
    dyn = next(b for b in r.blocks if b.name == "dynamic")
    assert "[计划]" not in dyn.text
