"""M14.1 权重公式验收。

这组测试守的是**性质**，不是数值。数值会被调参改掉，性质不能：
时间越久越低、召回让它掉得慢、pinned 不掉、边界不越界、纯函数无副作用。

公式为什么没照抄手册，见 `hippo/core/weight.py` 的 docstring 和 R-14.1。
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from hippo.config import Config
from hippo.core.weight import (
    compute_weight,
    half_life_of,
    now_ts,
    tier_of,
)

DAY = 86400
CFG = Config().weight


def ev(w0=0.5, *, pinned=0, recall=0, last=None, ingest=0, arousal=0.0, tier=2):
    """最小事件替身。故意不用真 Event：公式只该依赖这 5 个字段。"""
    return SimpleNamespace(w0=w0, pinned=pinned, recall_count=recall,
                           last_access=last, ingest_time=ingest,
                           arousal=arousal, tier=tier)


# ------------------------------------------------------------------ 单调性
def test_weight_decreases_with_time():
    """★ 时间越久权重越低。"""
    e = ev(0.7, ingest=0)
    prev = 1.0
    for d in range(0, 400, 10):
        w = compute_weight(e, d * DAY, cfg=CFG)
        assert w <= prev + 1e-12, f"第 {d} 天反而涨了"
        assert w > 0
        prev = w


def test_decay_is_exponential_not_linear():
    """半衰期的意思就是"过一期减半"。线性衰减会让老记忆永远不清零。"""
    e = ev(0.5, ingest=0)
    hl = half_life_of(0.5, cfg=CFG)
    w0 = compute_weight(e, 0, cfg=CFG)
    w1 = compute_weight(e, int(hl * DAY), cfg=CFG)
    assert w1 == pytest.approx(w0 / 2, rel=1e-6)
    w2 = compute_weight(e, int(2 * hl * DAY), cfg=CFG)
    assert w2 == pytest.approx(w0 / 4, rel=1e-6)


# ------------------------------------------------------------------ 半衰期分档
def test_important_things_decay_slower():
    """★ 越重要忘得越慢。手册的 hot=3 天是反的，这里是这条的守门人。"""
    heavy = half_life_of(0.75, cfg=CFG)
    mid = half_life_of(0.55, cfg=CFG)
    light = half_life_of(0.20, cfg=CFG)
    assert heavy > mid > light, f"{heavy} {mid} {light}"


def test_heavy_event_survives_90_days():
    """DoD：30 天前的重要事件还能被检索到。90 天后也得在 warm 以上。"""
    e = ev(0.75, ingest=0)
    assert compute_weight(e, 90 * DAY, cfg=CFG) >= CFG.warm
    assert compute_weight(e, 30 * DAY, cfg=CFG) >= 0.60


def test_trivial_event_dies_quickly():
    """轻记忆一周就基本没了，否则库会被寒暄塞满。"""
    e = ev(0.2, ingest=0)
    assert compute_weight(e, 7 * DAY, cfg=CFG) < 0.05


# ------------------------------------------------------------------ 召回加固
def test_recall_extends_half_life():
    """★ 召回 5 次 → 半衰期 ×(1+0.25×5)=2.25。"""
    base = half_life_of(0.55, cfg=CFG)
    got = half_life_of(0.55, recall=5, cfg=CFG)
    assert got == pytest.approx(base * 2.25)


def test_recall_gain_is_capped():
    """召回 100 次和 5 次一样。不封顶的话一次热门话题能把它顶成永生的。"""
    assert half_life_of(0.55, recall=100, cfg=CFG) == half_life_of(0.55, recall=5, cfg=CFG)


def test_recall_never_pushes_weight_above_w0():
    """★ 增益改成"延长半衰期"之后，w 恒 ≤ w0，永远不会被 clamp 削平。

    手册的乘性加成会顶到 1.0，然后几十天里权重曲线是平顶的——
    "时间越久越低"在输出上不成立。这条测试就是防那个回归。
    """
    for rc in (0, 1, 5, 99):
        e = ev(0.75, recall=rc, ingest=0)
        for d in (0, 1, 30, 365):
            w = compute_weight(e, d * DAY, cfg=CFG)
            assert w <= 0.75 + 1e-9, f"rc={rc} d={d} w={w} 超过 w0"


def test_last_access_wins_over_ingest():
    """上次想起比进来得晚，就以想起的时间为基准。"""
    e = ev(0.6, ingest=0, last=100 * DAY)
    assert compute_weight(e, 100 * DAY, cfg=CFG) == pytest.approx(0.6)
    assert compute_weight(e, 130 * DAY, cfg=CFG) < 0.6


def test_ingest_wins_when_never_recalled():
    """`last_access` 是 None 时不能当 0——那会让所有事件一出生就极老。"""
    e = ev(0.6, ingest=50 * DAY, last=None)
    assert compute_weight(e, 50 * DAY, cfg=CFG) == pytest.approx(0.6)


# ------------------------------------------------------------------ pinned
def test_pinned_does_not_decay():
    """★ 钉住 = 不忘记。90 天后仍然接近原值。"""
    e = ev(0.75, pinned=1, ingest=0)
    assert compute_weight(e, 90 * DAY, cfg=CFG) > 0.74
    # 10 年（3650 天）后仍有 0.70：半衰期 36500 天，10 年只衰掉 6.7%
    assert compute_weight(e, 3650 * DAY, cfg=CFG) > 0.69


def test_pinned_half_life_is_configurable():
    cfg = Config().weight
    cfg.pinned_half_life = 100.0
    assert half_life_of(0.5, pinned=True, cfg=cfg) == 100.0


# ------------------------------------------------------------------ 边界
def test_zero_stays_zero():
    e = ev(0.0, ingest=0)
    for d in (0, 10, 1000):
        assert compute_weight(e, d * DAY, cfg=CFG) == 0.0


def test_never_negative_never_above_one():
    for w0 in (0.0, 0.5, 1.0):
        for d in (0, 1, 10 ** 6):
            w = compute_weight(ev(w0, ingest=0), d * DAY, cfg=CFG)
            assert 0.0 <= w <= 1.0


def test_future_last_access_does_not_inflate():
    """时钟漂移/未来事件：age 归零，不产生负衰减（负衰减会让 w > w0）。"""
    e = ev(0.6, ingest=0, last=10 * DAY)
    assert compute_weight(e, 5 * DAY, cfg=CFG) == pytest.approx(0.6)


# ------------------------------------------------------------------ 纯度
def test_same_input_same_output():
    """纯函数。带 datetime.now() 的话"90 天后"只能靠改系统时钟来测。"""
    e = ev(0.55, recall=2, ingest=1000)
    a = compute_weight(e, 50000, cfg=CFG)
    b = compute_weight(e, 50000, cfg=CFG)
    assert a == b


def test_does_not_touch_the_event():
    """不许原地改事件。改了就变成"算一次漂一次"，重算不幂等。"""
    e = ev(0.55, recall=2, ingest=1000)
    before = dict(vars(e))
    compute_weight(e, 999999, cfg=CFG)
    assert vars(e) == before


def test_accepts_datetime_and_timestamp():
    from datetime import datetime
    e = ev(0.5, ingest=0)
    ts = 100 * DAY
    assert compute_weight(e, ts, cfg=CFG) == compute_weight(
        e, datetime.fromtimestamp(ts), cfg=CFG)


def test_now_ts_none_means_now():
    import time
    assert abs(now_ts(None) - time.time()) < 2


# ------------------------------------------------------------------ tier
def test_tier_thresholds():
    assert tier_of(0.85, cfg=CFG) == 1
    assert tier_of(0.50, cfg=CFG) == 2
    assert tier_of(0.10, cfg=CFG) == 3


def test_pinned_is_always_hot():
    assert tier_of(0.01, pinned=True, cfg=CFG) == 1


def test_tier_hysteresis_prevents_flapping():
    """★ 权重正好卡在 0.70 上的事件不许天天翻层。

    没有滞回的话：0.699 → warm，明天被召回 → 0.70 → hot，后天 → 0.699 → warm。
    探针里那条 w0=0.70 的事件 90 天翻了 30 次。
    """
    assert tier_of(0.68, cfg=CFG) == 2            # 从下面看是 warm
    assert tier_of(0.68, prev=1, cfg=CFG) == 1    # 从 hot 掉下来要多跌 0.05
    assert tier_of(0.64, prev=1, cfg=CFG) == 2    # 跌破 0.65 才算掉


def test_tier_hysteresis_on_cold_edge():
    assert tier_of(0.28, cfg=CFG) == 3
    assert tier_of(0.28, prev=2, cfg=CFG) == 3
    assert tier_of(0.28, prev=3, cfg=CFG) == 3
    assert tier_of(0.32, prev=3, cfg=CFG) == 3    # 要涨回 0.35 才出 cold
    assert tier_of(0.36, prev=3, cfg=CFG) == 2


def test_tier_is_output_not_input():
    """半衰期只看 w0，不看 tier。这条断了自指，是整套公式成立的前提。

    同一条事件，把 tier 从 1 改成 3，权重必须一模一样。
    """
    a = ev(0.55, ingest=0, tier=1)
    b = ev(0.55, ingest=0, tier=3)
    for d in (0, 5, 60):
        assert compute_weight(a, d * DAY, cfg=CFG) == compute_weight(b, d * DAY, cfg=CFG)
    assert half_life_of(0.55, cfg=CFG) == half_life_of(0.55, cfg=CFG)


def test_half_life_bands_are_configurable():
    cfg = Config().weight
    cfg.half_life_by_w0 = [[0.5, 10.0], [0.0, 2.0]]
    assert half_life_of(0.6, cfg=cfg) == 10.0
    assert half_life_of(0.4, cfg=cfg) == 2.0


def test_nan_w0_is_zero():
    e = ev(float("nan"), ingest=0)
    assert compute_weight(e, 0, cfg=CFG) == 0.0


def test_math_is_actually_half_life():
    """别再有人把 log(2) 写成常数 0.69 然后差一点点。"""
    e = ev(0.5, ingest=0)          # w0=0.5 落在 hl=30 那档
    hl = 30.0
    assert compute_weight(e, int(hl * DAY), cfg=CFG) == pytest.approx(0.25, rel=1e-6)
    assert math.log(2) * hl / hl == pytest.approx(math.log(2))
