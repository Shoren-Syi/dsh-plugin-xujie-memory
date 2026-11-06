# -*- coding: utf-8 -*-
"""M20.3 旋钮映射。验收：恰好一行 / 不含情绪自述 / 四旋钮可推导 /
长度 ≤60 字。
"""
import pytest

from hippo.core.affect import (BASELINE, Knobs, knobs_of, render_line)


def test_knobs_derivable_from_mapping():
    """验收 #3：四旋钮都可推导。手册映射表逐行核对。"""
    k = knobs_of({"joy": 90})                     # joy 90-60=30 > 15
    assert k.warmth > 0 and k.verbosity > 0
    k = knobs_of({"sadness": 40})                 # 40-10=30
    assert k.verbosity < 0 and k.initiative < 0
    k = knobs_of({"anger": 35})                   # 35-5=30
    assert k.verbosity < 0 and k.recall_breadth > 0
    k = knobs_of({"fear": 35})                    # 35-5=30
    assert k.initiative < 0
    k = knobs_of({"disgust": 35})                 # 35-5=30
    assert k.warmth < 0
    k = knobs_of({"surprise": 50})                # 50-20=30
    assert k.initiative > 0


def test_knobs_baseline_relative():
    """基线相对偏移驱动，不是绝对值：基线 joy 60 时 62 不算愉快。"""
    k = knobs_of({"joy": 62})
    assert k.warmth == 0 and k.verbosity == 0


def test_exactly_one_line():
    """验收 #1：输出恰好一行（无换行符）。"""
    for moods in ({"joy": 90}, {"anger": 80, "fear": 60}, {}, BASELINE.copy(),
                  {m: 99 for m in BASELINE}):
        line = render_line(moods)
        assert "\n" not in line
        assert line.startswith("[状态]")


def test_no_self_declaration():
    """验收 #2：不含情绪自述——不写"我现在很生气"。"""
    import re
    pat = re.compile(r"我(很|好|超级|特别|真的|现在)?"
                     r"(生气|愤怒|难过|伤心|害怕|恐惧|开心|高兴|愉悦|恶心|惊讶|激动)")
    for moods in ({"anger": 90}, {"sadness": 80}, {"joy": 95},
                  {m: 99 for m in BASELINE}):
        assert not pat.search(render_line(moods))


def test_max_60_chars():
    """验收 #4：长度 ≤60 字 100%。扫 200 组随机极端状态。"""
    import random
    random.seed(42)
    for _ in range(200):
        moods = {m: random.choice([0, 5, 20, 40, 55, 70, 85, 100])
                 for m in BASELINE}
        line = render_line(moods)
        assert len(line) <= 60, (moods, line, len(line))


def test_quiet_state_still_one_line():
    """全中性时也恰好一行、有指令位（不许空转）。"""
    line = render_line(BASELINE.copy())
    assert len(line) <= 60 and "一切照常" in line


def test_sadness_warmth_untouched():
    """sadness 高只动 verbosity/initiative，不动 warmth（悲伤不是冷漠）。"""
    k = knobs_of({"sadness": 50})
    assert k.warmth == 0


def test_render_line_smoke():
    """样例输出格式：显著维度 + 旋钮短语。joy+anger 的 verbosity
    抵消（叠加语义：矛盾情绪表达中性，不算 bug——见 R-20.3），
    换同向叠加的 sadness+anger 验证。"""
    line = render_line({"joy": 80, "anger": 30})
    assert "愉悦80" in line and "愤怒30" in line
    assert "语气轻快" in line          # joy warmth+
    assert "检索广度大" in line        # anger recall+
    line = render_line({"sadness": 50, "anger": 35})
    assert "话少" in line              # sadness 与 anger 同压 verbosity，叠加
    assert "少主动发问" in line        # sadness initiative-
    assert "检索广度大" in line        # anger recall+
