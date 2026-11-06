# -*- coding: utf-8 -*-
"""M21 调参：预设区分度 / τ 拟合正确性 / 盲评表完整性。"""
import json
import math
from pathlib import Path

from hippo.core.affect import (BASELINE, MOODS, PRESETS, TAU_MINUTES,
                               decay_moods, render_line)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- M21.1
def test_three_presets_distinct():
    """三套预设基线互不相同（人设首先要不一样）。"""
    vals = {tuple(sorted(p.items())) for p in PRESETS.values()}
    assert len(PRESETS) == 3 and len(vals) == 3
    assert PRESETS["warm"]["joy"] > PRESETS["neutral"]["joy"] > PRESETS["sharp"]["joy"]


def test_preset_trace_divergence():
    """区分度的真实口径：同一事件后的轨迹，多数时刻三套不同。

    评测单位是"事件→轨迹"而非"delta→一行"——同 delta 下旋钮
    输出必然相同（偏移一样），区分度来自到达阈值的难易和衰减节奏。
    """
    from hippo.tools.eval_persona import SCENARIOS, trace
    same = diff = 0
    for sc in SCENARIOS:
        lines = [trace(PRESETS[n], sc) for n in ("warm", "neutral", "sharp")]
        for i in range(3):
            if lines[0][i] == lines[1][i] == lines[2][i]:
                same += 1
            else:
                diff += 1
    assert diff > same, f"区分度不足: 不同 {diff} / 相同 {same}"
    # 平静场景三套永远相同（人设只在有事时显形）
    assert trace(PRESETS["warm"], {}) == trace(PRESETS["sharp"], {})


def test_sharp_joy_tail_longer_after_big_joy():
    """区分度的数学来源要诚实：τ 共用 + 偏移驱动下，同样的单次 delta
    三套的偏移轨迹**完全相同**——差异只能来自 clamp（以及多轮叠加时
    warm 先顶到 100）。模拟"被连夸 4 轮"（每轮 +20，cap 内）：warm
    第 3 轮就顶格（偏移 30），sharp 第 4 轮才顶格（偏移 55）；3 小时
    后 sharp 还在显著愉悦区而 warm 已回中性——"平时低沉的人大喜后
    的反差拖得更久"，符合直觉。
    """
    from hippo.core.affect import apply_delta
    after = {}
    for n in ("warm", "neutral", "sharp"):
        m = dict(PRESETS[n])
        for _ in range(4):                     # 连夸 4 轮
            m = apply_delta(m, {"joy": 20}, inhibition={})
        after[n] = m["joy"]
    assert after["warm"] == 100.0 and after["sharp"] == 100.0
    t180 = {n: decay_moods({"joy": after[n]}, baseline=PRESETS[n],
                           tau=TAU_MINUTES, minutes=180)["joy"]
            for n in after}
    assert t180["warm"] - PRESETS["warm"]["joy"] < 15     # warm 回中性
    assert t180["sharp"] - PRESETS["sharp"]["joy"] >= 15  # sharp 拖尾
    # 小 delta 单次时三套偏移轨迹相同（诚实口径：这不是区分度来源）
    a2 = {n: apply_delta(PRESETS[n], {"joy": 20}, inhibition={})["joy"]
          - PRESETS[n]["joy"] for n in after}
    assert len({round(v, 6) for v in a2.values()}) == 1


# ---------------------------------------------------------------- M21.2
def test_fit_tau_recovers_true_value():
    """合成数据验证拟合器：按 τ=14（网格点）生成采样，拟合应命中。"""
    from hippo.tools.fit_tau import fit_tau
    v0, base = 80.0, 5.0
    samples = [(t, base + (v0 - base) * math.exp(-t / 14))
               for t in (1, 5, 15, 60, 180)]
    assert fit_tau(v0, base, samples) == 14.0


def test_fit_tau_noisy_data_close():
    """带噪声的合成数据：τ=90 的曲线加 ±5% 噪声，拟合落在 60~120。"""
    from hippo.tools.fit_tau import fit_tau
    v0, base = 60.0, 10.0
    samples = [(t, base + (v0 - base) * math.exp(-t / 90) * (1 + 0.02 * ((t * 7) % 5 - 2)))
               for t in (1, 5, 15, 60, 180)]
    assert 60 <= fit_tau(v0, base, samples) <= 120


def test_scenario_fixture_covers_all_moods():
    """手册验收 #1：六维 τ 都拟合过 → 夹具必须覆盖全部六维。"""
    rows = [json.loads(line) for line in
            (FIXTURES / "affect_scenarios.jsonl").read_text(
                encoding="utf-8").splitlines() if line.strip()]
    assert {r["mood"] for r in rows} == set(MOODS)
    assert len(rows) == 6
    for r in rows:
        assert len(r["samples"]) == 5      # 手册：按 1/5/15/60/180 分钟采样
        assert set(r["samples"]) == {"1", "5", "15", "60", "180"}


def test_calibrated_tau_matches_fixture():
    """M21.2 写回 config 后：夹具直觉数据与校准 τ 的平均偏差 <20%
    （验收 #2 的口径）。"""
    from hippo.tools.fit_tau import fit_tau
    rows = [json.loads(line) for line in
            (FIXTURES / "affect_scenarios.jsonl").read_text(
                encoding="utf-8").splitlines() if line.strip()]
    total = 0.0
    for r in rows:
        m = r["mood"]
        base = BASELINE[m]
        samples = [(float(t), float(v)) for t, v in r["samples"].items()]
        tau = fit_tau(base + r["delta"], base, samples)
        total += abs((tau - TAU_MINUTES[m]) / TAU_MINUTES[m])
    assert total / len(rows) < 0.20, f"平均偏差 {total / len(rows):.0%}"


# ---------------------------------------------------------------- M21.3
def test_blind_sheet_consistent(tmp_path):
    """盲评表与答案键条数一致、状态分布均衡（手册口径：每状态 5 条）。"""
    from hippo.tools.blind_test import main as bt_main
    import sys
    sheet = tmp_path / "s.html"
    key = tmp_path / "k.json"
    argv = sys.argv
    sys.argv = ["blind_test", "--emit", str(sheet), "--key", str(key),
                "--seed", "7"]
    try:
        bt_main()
    finally:
        sys.argv = argv
    entries = json.loads(key.read_text(encoding="utf-8"))
    assert len(entries) == 15
    from collections import Counter
    assert Counter(e["state"] for e in entries) == {
        "anger": 5, "joy": 5, "sadness": 5}
    html = sheet.read_text(encoding="utf-8")
    assert html.count("<tr>") == 16          # 15 条 + 表头


def test_blind_replies_no_self_declaration():
    """验收 #4 红线：回复模板不含情绪自述——区分靠语气不靠'我很难过'。"""
    import re
    from hippo.tools.blind_test import _tpl_for
    pat = re.compile(r"我(很|好|超级|特别|真的|现在)?"
                     r"(生气|愤怒|难过|伤心|害怕|恐惧|开心|高兴|愉悦|恶心|惊讶|激动)")
    for st in ("anger", "joy", "sadness"):
        for t in _tpl_for(st):
            assert not pat.search(t), (st, t)


def test_blind_states_render_one_line():
    """三组状态的 [状态] 行都是一行 ≤60 字（模板由旋钮驱动的前提）。"""
    from hippo.tools.blind_test import STATES
    for moods in STATES.values():
        line = render_line(moods)
        assert len(line) <= 60 and "\n" not in line
