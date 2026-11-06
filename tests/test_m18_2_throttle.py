"""M18.2 限流器：六个拦截条件 + 每轮一条 + 记账 + 注入格式。

手册验收表 4 条：一轮最多 1 条 / 8 轮内不重复 / 最多问 2 次 /
50 轮追问 ≤5 次（用 sim_throttle 模拟，asks=3）。
"""

from __future__ import annotations

from hippo.core.doubt import (ASK_TEMPLATE, DEFAULT_COOLDOWN, DEFAULT_MAX_ASK,
                              format_doubt, pick_doubt, should_ask)
from hippo.tools.sim_throttle import simulate


def _d(**kw) -> object:
    """should_ask 只需要这几个字段，用一个轻量替身，不建库。"""
    from hippo.tools.sim_throttle import SimDoubt
    base = dict(id=1, question="用户的老家在哪个城市", confidence=0.5,
                impact=0.8, cost=0.3, expires=10**12, asked_count=0,
                last_asked_turn=None)
    base.update(kw)
    return SimDoubt(**base)


# ---------------------------------------------------------- 六条件逐条
def test_condition_asked_this_turn():
    assert should_ask(_d(), 5, True) is False


def test_condition_confident_enough():
    """confidence > 0.75：基本懂了，问了像抬杠。"""
    assert should_ask(_d(confidence=0.76), 5, False) is False
    assert should_ask(_d(confidence=0.75), 5, False) is True   # 边界可问


def test_condition_low_impact():
    assert should_ask(_d(impact=0.39), 5, False) is False
    assert should_ask(_d(impact=0.4), 5, False) is True


def test_condition_too_costly():
    assert should_ask(_d(cost=0.71), 5, False) is False
    assert should_ask(_d(cost=0.7), 5, False) is True


def test_condition_expired():
    assert should_ask(_d(expires=100), 5, False, now_ts=101) is False
    assert should_ask(_d(expires=100), 5, False, now_ts=100) is True  # 到期当刻仍可问


def test_condition_cooldown():
    assert should_ask(_d(last_asked_turn=3), 10, False) is False   # 差 7 轮
    assert should_ask(_d(last_asked_turn=2), 10, False) is True    # 差 8 轮，放行


def test_condition_max_ask_count():
    assert should_ask(_d(asked_count=2, last_asked_turn=1), 20, False) is False
    assert should_ask(_d(asked_count=1, last_asked_turn=1), 20, False) is True


def test_all_clear_passes():
    assert should_ask(_d(), 5, False) is True


# ---------------------------------------------------------- pick
def test_pick_highest_impact_per_cost(store):
    """三个都放行 → 取 impact/cost 最高的那条。"""
    store.add_doubt(session="s", question="用户的老家在哪个城市",
                    impact=0.8, cost=0.4)                    # 2.0
    store.add_doubt(session="s", question="用户养的猫是什么品种",
                    impact=0.9, cost=0.3)                    # 3.0 ← 选它
    store.add_doubt(session="s", question="用户的新工作干啥的呀",
                    impact=0.6, cost=0.2)                    # 3.0（后入库，并列不选）
    d = pick_doubt(store, "s", turn=1)
    assert d is not None and d.question == "用户养的猫是什么品种"


def test_pick_respects_cooldown_then_counts(store):
    """问过 → 8 轮内不再问；且 asked_count+1 记账。"""
    did = store.add_doubt(session="s", question="用户的老家在哪个城市",
                          impact=0.8, cost=0.3)
    assert pick_doubt(store, "s", turn=1) is not None
    d = [x for x in store.active_doubts("s") if x.id == did][0]
    assert d.asked_count == 1 and d.last_asked_turn == 1

    # 冷却期内：8 轮内不再挑出任何东西
    assert pick_doubt(store, "s", turn=5) is None
    # 冷却刚过：还能第二次
    d2 = pick_doubt(store, "s", turn=9)
    assert d2 is not None and d2.id == did


def test_pick_never_third_time(store):
    """最多问 2 次：第 3 次不再问（手册验收 #3）。"""
    did = store.add_doubt(session="s", question="用户的老家在哪个城市",
                          impact=0.8, cost=0.3)
    assert pick_doubt(store, "s", turn=1) is not None
    assert pick_doubt(store, "s", turn=10) is not None
    assert pick_doubt(store, "s", turn=20) is None
    d = [x for x in store.active_doubts("s") if x.id == did][0]
    assert d.asked_count == 2


# ---------------------------------------------------------- 注入格式
def test_format_fixed_template():
    q = "用户的老家在哪个城市"
    assert format_doubt(q) == ASK_TEMPLATE.format(q=q)
    assert format_doubt(q).startswith("[存疑] ")
    assert "问一次就好" in format_doubt(q)


def test_sim_throttle_50_turns():
    """手册验收 #4：50 轮 ≤5 次（剧情设计为 3 次）。"""
    res = simulate(50)
    assert res.asks <= 5
    assert res.asks == 3
    assert res.repeats == 0
    # 每轮最多 1 条在 simulate 结构上保证；history 无重复冷却违规
    turns_asked = [t for t, _ in res.history]
    assert len(turns_asked) == len(set(turns_asked))


def test_sim_throttle_repeat_violation_zero():
    """8 轮内不重复（手册验收 #2）：同一条两次注入间隔 ≥8。"""
    res = simulate(50)
    by_id: dict[int, list[int]] = {}
    for t, did in res.history:
        by_id.setdefault(did, []).append(t)
    for did, ts in by_id.items():
        for a, b in zip(ts, ts[1:]):
            assert b - a >= DEFAULT_COOLDOWN, (did, a, b)
    # 并验证一条最多问 2 次
    assert all(len(ts) <= DEFAULT_MAX_ASK for ts in by_id.values())


def test_doubt_defaults_match_manual():
    assert DEFAULT_COOLDOWN == 8
    assert DEFAULT_MAX_ASK == 2
