"""M25.2 额度与静默：别让它变成话痨。

四条规矩各自有测试，最后一条跑七天：
日额度 / 自然日重置 / 静默时段 / 冷遇闭嘴 / 不打断。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from hippo.core.curiosity import Curiosity
from hippo.core.scheduler import Scheduler
from hippo.tools import sim_days

TZ = ZoneInfo("Asia/Shanghai")


def _ts(day: int = 5, hour: int = 10, minute: int = 0) -> int:
    return int(datetime(2026, 1, day, hour, minute, tzinfo=TZ).timestamp())


def _gap(store, now: int, text: str = "那边是公司还是老家？") -> int:
    return store.add_doubt(session="s", question=text, impact=0.9,
                           expire_days=0, now=now - 8 * 86400)


def _speak(store, now: int, text: str = "问一句？") -> int:
    cid = store.add_curiosity(session="s", question=text, gap_kind="doubt",
                              gap_ref=f"doubt:{now}", impact=0.8, urgency=1.0,
                              novelty=1.0, priority=0.8, now=now)
    store.mark_curiosity_spoken(cid, now)
    return cid


def test_daily_cap(store):
    """一天最多 3 次。第 4 次连队列都不该进。"""
    now = _ts()
    _gap(store, now)
    for i in range(3):
        _speak(store, now + i, f"第 {i} 问？")
    eng = Curiosity(store, wall=lambda: now + 10)
    assert eng.quota.used(now + 10) == 3
    assert eng.prepare("s", now + 10) is None
    assert eng.take("s", now + 10) is None


def test_reset_next_day(store):
    """自然日重置：昨天说满了，今天从 3 开始。"""
    now = _ts()
    _gap(store, now)
    for i in range(3):
        _speak(store, now + i)
    eng = Curiosity(store, wall=lambda: now)
    assert eng.quota.left(now) == 0
    assert eng.quota.left(now + 86400) == 3
    # 顺带验证"刚问完的那条 24 小时内仍算未落地"，所以要多等一会儿
    assert eng.prepare("s", now + 86400 + 60) is not None


def test_quiet_hours(store):
    """23 点到早上 8 点之间不主动开口。"""
    night = _ts(hour=23, minute=30)
    _gap(store, night)
    eng = Curiosity(store, wall=lambda: night)
    assert eng.quota.in_quiet(night)
    assert eng.prepare("s", night) is None


def test_quiet_boundary(store):
    """8 点整就该放行——边界写错一格，等于每天少两小时。"""
    morning = _ts(hour=8, minute=0)
    _gap(store, morning)
    eng = Curiosity(store, wall=lambda: morning)
    assert not eng.quota.in_quiet(morning)
    assert eng.prepare("s", morning) is not None


def test_cold_shoulder(store):
    """连着两次没人接 → 今天不说了。"""
    now = _ts()
    for i in range(2):
        _speak(store, now - (30 - i) * 3600, f"第 {i} 问？")
    store.sweep_curiosity_ignored(now=now)

    eng = Curiosity(store, wall=lambda: now)
    _gap(store, now)
    assert eng.store.gave_up_curiosity("s", now=now)
    assert eng.prepare("s", now) is None
    assert eng.quota.snoozed("s", now)


def test_cold_shoulder_recovers(store):
    """冷遇不是永久的。三次都问不出去，等于好奇这个功能死了。

    三天后再试一次：状态过期之后必须重新开口。
    """
    now = _ts()
    for i in range(2):
        _speak(store, now - 5 * 86400 + i * 3600, f"第 {i} 问？")
    store.sweep_curiosity_ignored(now=now)

    eng = Curiosity(store, wall=lambda: now)
    _gap(store, now)
    assert not eng.store.gave_up_curiosity("s", now=now)
    assert eng.prepare("s", now) is not None


def test_busy_postpones(store):
    """30 分钟内有过对话 → 推迟。他正在聊，回复里顺手问就行。"""
    now = _ts()
    _gap(store, now)
    store.add_turns("s", [("user", "在吗", 0)], ts=now - 5 * 60)

    eng = Curiosity(store, wall=lambda: now)
    assert eng.quota.busy("s", now)
    assert eng.prepare("s", now) is None

    eng2 = Curiosity(store, wall=lambda: now + 3600)
    assert not eng2.quota.busy("s", now + 3600)
    assert eng2.prepare("s", now + 3600) is not None


def test_no_stacking(store):
    """问出去还没等到回应，就别问第二句。

    连着三条"你最近怎么样"不是关心，是催债。
    """
    now = _ts()
    store.add_doubt(session="s", question="那边是公司还是老家？", impact=0.9,
                    expire_days=0, now=now - 8 * 86400)
    store.add_doubt(session="s", question="那个人叫什么来着？", impact=0.8,
                    expire_days=0, now=now - 8 * 86400)
    eng = Curiosity(store, wall=lambda: now)
    assert eng.prepare("s", now) is not None
    assert eng.take("s", now) is not None
    # 刚说出去，还没人接
    assert eng.outstanding("s", now + 60)
    assert eng.prepare("s", now + 60) is None


def test_plan_not_quiet(store):
    """静默只管好奇。用户自己定的提醒，半夜也要响。"""
    night = _ts(hour=23, minute=30)
    store.add_plan(session="s", due_at=night - 60, content="叫他起来看球")
    fired = Scheduler(store).tick(now=night)
    assert len(fired) == 1


def test_chatty_day_capped(store):
    """一个话多的日子：用户一小时里聊了六轮，额度必须在第 4 次开口时拦住。

    这是唯一能真正压到 daily_quota 的场景——平时"一次只问一句"和
    "30 分钟内不打断"比额度先起作用，所以七天模拟里根本碰不到上限。
    """
    now = _ts()
    for i in range(8):
        store.add_doubt(session="s", question=f"第 {i} 件事后来怎么样了？",
                        impact=0.8, expire_days=0, now=now - 8 * 86400)

    eng = Curiosity(store, wall=lambda: now)
    spoke = 0
    t = now
    for _ in range(8):
        t += 40 * 60
        # 上一轮 39 分钟前结束 → 不在"正在聊"窗口里，可以准备
        eng.prepare("s", t - 60)
        store.add_turns("s", [("user", "在吗", 0)], ts=t)
        q = eng.take("s", t)
        if q:
            spoke += 1
            eng.note_user_turn("s", q, now=t + 60)   # 用户接了这个话头

    assert spoke == 3, spoke
    assert eng.quota.used(t) == 3


def test_seven_days():
    """七天：日 ≤3，静默时段 0 次。"""
    r = sim_days.run(7)
    assert r["max_per_day"] <= 3, r["speaks"]
    assert r["quiet_violations"] == 0
    assert len(r["speaks"]) == 7
    # 冷遇那几天真的闭嘴：不是"少说"，是 0
    assert 0 in r["speaks"], r["speaks"]
