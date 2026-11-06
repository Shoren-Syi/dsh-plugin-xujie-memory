# -*- coding: utf-8 -*-
"""M26.3 基线对照。

三组跑同一份语料，回答一个很朴素的问题：**这东西比不做强在哪**。

读这张表要小心一件事：`full` 的召回是 1.000，比本方案还高。这不是
本方案输了，两者的约束根本不同——

    full   把所有历史都塞进 prompt，信息当然全在，代价是每轮 4412 token
    hippo  在 576 token 的预算里挑，挑到 0.96

"全量重读"的问题从来不是记不住，是贵：200 轮之后每轮 4.4k token，
一年就是几十万。本方案要证明的是"用 1/8 的钱买到 96% 的召回"。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hippo.config import load_config
from hippo.tools.eval_all import run, table

FIX = Path("tests/fixtures/dialog_200.jsonl")


@pytest.fixture(scope="module")
def three():
    cfg = load_config()
    return {m: run(m, fixture=FIX, cfg=cfg) for m in ("none", "full", "hippo")}


def test_none_barely_remembers(three):
    """只带最近 5 轮：长程信息基本全丢。

    20 条事实里能答出 1 条（0.06），且能答出的那条一定是最近说过的那条。
    """
    assert three["none"].recall < 0.20


def test_hippo_beats_none_by_a_wide_margin(three):
    """DoD 第 2 条：C 的召回显著高于 A。"""
    assert three["hippo"].recall - three["none"].recall > 0.60


def test_hippo_costs_a_fraction_of_full(three):
    """DoD 第 3 条：C 的成本 ≤ B 的 1/5。

    实测 576 / 4412 = 0.13，也就是 1/7.7。
    """
    ratio = three["hippo"].tokens_per_turn / three["full"].tokens_per_turn
    assert ratio <= 0.20


def test_full_is_the_ceiling(three):
    """全量重读的召回是上限——它在信息上确实没有损失，只是贵。

    这条断言的意义是**承认对手不弱**：如果 full 只有 0.5，
    说明语料或评测有问题（答案原文都在上下文里，怎么可能答不出）。
    """
    assert three["full"].recall >= 0.95
    assert three["full"].tokens_per_turn > three["hippo"].tokens_per_turn * 4


def test_table_renders_three_rows(three):
    """给外部看的那张表，三行齐全、有表头、不串行。"""
    text = table([three["none"], three["full"], three["hippo"]])
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert "mode" in lines[0]
    body = [ln for ln in lines if ln.startswith(("none", "full", "hippo"))]
    assert len(body) == 3
    assert all("x" in ln for ln in body)      # 每行都有成本倍数
