# -*- coding: utf-8 -*-
"""M20.2 delta 应用与交叉抑制。验收：喜怒不同高 / cap 生效 /
抑制方向正确 / clamp 正确。
"""
import math

import pytest

from hippo.core.affect import BASELINE, DELTA_CAP, apply_delta


def test_cap_limits_single_delta():
    """验收 #2：单次 delta 上限 ±20。一句话不许把情绪打满。"""
    out = apply_delta(BASELINE.copy(), {"joy": 80, "sadness": -80})
    assert out["joy"] == BASELINE["joy"] + DELTA_CAP          # 60+20=80
    assert out["sadness"] == 0.0                              # 10-20→clamp 0


def test_clamp_bounds():
    """验收 #4：终值不越界。低基线 + 负 delta 顶到 0，高值顶到 100。"""
    out = apply_delta({"anger": 3}, {"anger": -20})
    assert out["anger"] == 0.0
    out = apply_delta({"joy": 98}, {"joy": 20})
    assert out["joy"] == 100.0


def test_joy_suppresses_sadness_and_anger():
    """验收 #3：joy↑ → sadness↓ 且 anger↓（矩阵 joy: {sadness:-.3, anger:-.2}）。"""
    out = apply_delta({"sadness": 50, "anger": 50}, {"joy": 20})
    assert out["sadness"] == 50 - 6.0    # -0.3×20
    assert out["anger"] == 50 - 4.0      # -0.2×20


def test_negative_delta_no_crosstalk():
    """抑制只对正向 delta 生效：joy 下降不该把 sadness 拉回来。"""
    out = apply_delta({"sadness": 50}, {"joy": -20})
    assert out["sadness"] == 50.0


def test_no_joy95_anger90():
    """验收 #1：不会出现"喜 95 怒 90"。扫最极端的合法输入组合。"""
    for joy_d in (0, 5, 10, 15, 20):
        for anger_v in (60, 70, 80, 90, 100):
            out = apply_delta({"anger": anger_v}, {"joy": joy_d, "anger": 20})
            assert not (out["joy"] > 90 and out["anger"] > 90)


def test_inhibition_stacks():
    """多来源同时压制同一维应叠加：joy↑ 与 fear↑ 各给 anger 一份压制。"""
    out = apply_delta({"anger": 50}, {"joy": 10, "fear": 10})
    assert out["anger"] == 50 - 2.0 - 3.0   # joy -0.2×10 + fear -0.3×10


def test_inhibition_order_cap_first():
    """顺序验证：cap 在抑制之前——80 的 delta 先限到 20，抑制按 20 算。"""
    out = apply_delta({"sadness": 50}, {"joy": 80})
    assert out["sadness"] == 50 - 6.0    # -0.3×20，不是 -0.3×80=-24


def test_custom_inhibition_matrix():
    """抑制矩阵可注入（config 覆盖）。空矩阵 = 纯叠加。"""
    out = apply_delta({"sadness": 50}, {"joy": 20}, inhibition={})
    assert out["sadness"] == 50.0
    out = apply_delta({"sadness": 50}, {"joy": 20},
                      inhibition={"joy": {"sadness": -0.5}})
    assert out["sadness"] == 50 - 10.0


def test_input_not_mutated():
    """不改入参。persist 里 before/after 都要留着算 affect_changed。"""
    cur = {"joy": 60.0, "sadness": 10.0}
    apply_delta(cur, {"joy": 20})
    assert cur["joy"] == 60.0


# ---------------------------------------------------------------- persist
def _persist(store, text, ts, **delta):
    """走真实的 turn 入库 → 切窗 → persist 链路（不用手搓 Turn）。"""
    from hippo.core.persist import persist_result
    from hippo.core.schema import ExtractResult
    from tests.test_m5_3_persist import make_window

    w = make_window(store, [text], ts=ts)
    return persist_result(store, "s", w, ExtractResult(
        events=[{"raw": text}], affect_delta=dict(delta)))


def test_persist_end_to_end_with_inhibition(store):
    """persist 端到端：delta 走 cap→抑制→clamp。joy +15 → sadness
    被压 -4.5（与 test_m5_3 同款手算）。"""
    from hippo.core.persist import persist_result

    st = _persist(store, "今天太开心了", 1_700_000_000, joy=15, sadness=-5)
    a = store.get_affect("s")
    assert st.affect_changed >= 1
    assert a.joy == 75.0
    assert a.sadness == pytest.approx(0.5)


def test_persist_decay_before_delta(store):
    """两轮之间隔了 30 分钟：第二轮叠加前必须先衰减（否则"气一会儿
    就消"只在没人说话时成立）。用 anger τ=10 的强衰减验证。"""
    from hippo.core.affect import TAU_MINUTES

    t0 = 1_700_000_000
    _persist(store, "气死了", t0, anger=20)
    assert store.get_affect("s").anger == BASELINE["anger"] + 20  # 25

    _persist(store, "没事了", t0 + 1800, joy=0)   # 零 delta 不触发，改给 joy=0 无效
    # 零 delta 走不到衰减分支，补一轮 0.5 的 epsilon
    _persist(store, "真没事了", t0 + 1800, joy=0, anger=1)
    a = store.get_affect("s")
    expected = BASELINE["anger"] + 20 * math.exp(-30 / TAU_MINUTES["anger"])
    assert a.anger == pytest.approx(expected + 1, abs=0.01)  # 5.995+1


def test_persist_zero_delta_skips(store):
    """全零 delta（is_zero）连衰减都不跑——没有新信息时状态停在上次
    写下的值，lazy 读时照样会算，不亏。"""
    from hippo.core.schema import ExtractResult
    from hippo.core.persist import persist_result
    from tests.test_m5_3_persist import make_window

    t0 = 1_700_000_000
    _persist(store, "嗯", t0, joy=10)
    before = store.get_affect("s").updated
    w = make_window(store, ["嗯嗯"], ts=t0)
    persist_result(store, "s", w, ExtractResult(events=[{"raw": "嗯嗯"}]))
    assert store.get_affect("s").updated == before   # 没碰 affect


def test_persist_updated_refreshed(store):
    """写回后 updated 刷新——下轮 decay 的 Δt 从这里起算。"""
    _persist(store, "嗯", 1_700_000_000, joy=10)
    assert store.get_affect("s").updated == 1_700_000_000
