# -*- coding: utf-8 -*-
"""M20.1 状态与衰减。验收四条：曲线符合公式 / 愤怒约10分钟减半 /
不发散 / lazy 计算与逐分钟累积误差 <1。
"""
import math

import pytest

from hippo.core.affect import (BASELINE, MOODS, TAU_MINUTES, decay_moods,
                               decay_value)


def test_manual_formula_value():
    """手算对照：v=80, base=5, τ=10, t=10 → 5+75·e⁻¹ = 32.59。"""
    v = decay_value(80, 5, minutes=10, tau=10)
    assert abs(v - (5 + 75 * math.exp(-1))) < 1e-9


def test_tau_zero_never_decays():
    """τ=0 / Δt=0 / 负数：原样返回，不许 NaN（坏参数宁可不衰减）。"""
    assert decay_value(80, 5, minutes=0, tau=10) == 80
    assert decay_value(80, 5, minutes=-5, tau=10) == 80
    assert decay_value(80, 5, minutes=10, tau=0) == 80
    assert decay_value(80, 5, minutes=10, tau=-1) == 80


def test_anger_halves_in_ten_minutes():
    """验收 #2：τ=10 下 10 分钟大约衰减一半。手册写"50→约27"，
    公式实算是 50→21.6（衰减 57%）——手册的样例数字与它自己的
    公式不自洽（"约27"对应 τ≈14）。以公式为准（见 R-20.1），
    断言取"10 分钟后 ≤ 初始一半"这个更强的口径。
    """
    v = decay_value(50, 5, minutes=10, tau=TAU_MINUTES["anger"])
    assert v == pytest.approx(5 + 45 * math.exp(-1), abs=0.01)   # 21.56
    assert v < 25  # 初始 50，已过半


def test_surprise_fastest_joy_slowest():
    """同一 Δt 下 τ 越小衰减越多：surprise(5) 一刻钟就归位，joy(180) 几乎没动。

    （M21.2 校准后 surprise τ=5；anger τ=10 仍是最快的维度之一，
    15 分钟时 5+85·e^(-1.5)≈24，比 surprise 20+70·e^(-3)≈23.5 略高——
    "惊讶比愤怒消得快"仍然成立。）
    """
    v_s = decay_value(90, BASELINE["surprise"], 15, TAU_MINUTES["surprise"])
    v_j = decay_value(90, BASELINE["joy"], 15, TAU_MINUTES["joy"])
    v_a = decay_value(90, BASELINE["anger"], 15, TAU_MINUTES["anger"])
    # 20 + 70·e⁻³ ≈ 23.49（注释与断言都按公式实算，M17 的教训）
    assert v_s == pytest.approx(BASELINE["surprise"] + 70 * math.exp(-3), abs=0.01)
    assert v_j > 85                                 # 60+30·e^(-15/180) ≈ 87.7
    assert v_s < v_a                                # 惊讶比愤怒消得快


def test_baseline_not_zero():
    """手册 #3：衰减终点是基线不是 0。24h 后应贴基线——
    sadness τ=480（M21.2 校准后）只走 3τ，余量 4.0，断言放宽到 5
    （不是实现的误差，是 24h 对最慢的维度本来就走不完）。"""
    for m in MOODS:
        v = decay_value(90, BASELINE[m], 60 * 24, TAU_MINUTES[m])
        assert abs(v - BASELINE[m]) < 5.0


def test_moods_unknown_dim_preserved():
    """decay_moods 对未知维度不动（config 里多出的自定义维度别弄丢）。"""
    out = decay_moods({"joy": 80, "custom": 42},
                      baseline=BASELINE, tau=TAU_MINUTES, minutes=10)
    assert out["custom"] == 42


def test_curves_fit_formula():
    """验收 #1：24h 曲线符合公式。每步 decay 用的是解析解（不是欧拉
    近似），R² 恒等于 1——这里验证的是"实现没有偏离公式"，口径
    与 sim_affect 一致。"""
    moods = {m: 90.0 for m in MOODS}
    for t in range(1, 60 * 24 + 1):
        moods = decay_moods(moods, baseline=BASELINE,
                            tau=TAU_MINUTES, minutes=1)
        for m in MOODS:
            exact = BASELINE[m] + (90 - BASELINE[m]) * math.exp(-t / TAU_MINUTES[m])
            assert moods[m] == pytest.approx(exact, abs=1e-6), (m, t)


def test_never_out_of_bounds():
    """验收 #3：恒在 0~100。超界输入也要夹回来。"""
    out = decay_moods({"joy": 250, "sadness": -30, "anger": 90},
                      baseline=BASELINE, tau=TAU_MINUTES, minutes=5)
    for v in out.values():
        assert 0 <= v <= 100


def test_lazy_matches_per_minute():
    """验收 #4：lazy 一次算 60 分钟 vs 逐分钟累积 60 步，误差 <1。

    两者都是解析解的采样，理论上应当**完全相等**——这正是选解析
    公式而不是欧拉积分的理由：衰减可以任意 lazy，不会因步长漂移。
    """
    start = {"anger": 80.0}
    lazy = decay_moods(start, baseline=BASELINE, tau=TAU_MINUTES, minutes=60)
    step = dict(start)
    for _ in range(60):
        step = decay_moods(step, baseline=BASELINE, tau=TAU_MINUTES, minutes=1)
    for m in start:
        assert abs(lazy[m] - step[m]) < 1e-6


def test_lazy_large_gap_also_exact():
    """极端口径：lazy 一次算 24h vs 逐分钟 1440 步，仍完全相等。"""
    start = {m: 88.0 for m in MOODS}
    lazy = decay_moods(start, baseline=BASELINE, tau=TAU_MINUTES,
                       minutes=60 * 24)
    step = dict(start)
    for _ in range(60 * 24):
        step = decay_moods(step, baseline=BASELINE, tau=TAU_MINUTES, minutes=1)
    for m in MOODS:
        assert lazy[m] == pytest.approx(step[m], abs=1e-6)


def test_negative_dt_is_noop():
    """时间倒流（回填旧对话、时钟回拨）不改变状态。"""
    m = {"joy": 70.0}
    assert decay_moods(m, baseline=BASELINE, tau=TAU_MINUTES,
                       minutes=-30)["joy"] == 70.0
