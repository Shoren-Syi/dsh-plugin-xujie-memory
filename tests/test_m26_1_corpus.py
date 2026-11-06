# -*- coding: utf-8 -*-
"""M26.1 语料体检。

这组测试守的不是"代码能不能跑"，而是**语料本身有没有坏**。
语料坏掉的表现特别隐蔽：跑完 200 轮，某条事实召回率 0，
你会去查检索、查权重、查 pack 预算——而真正的原因可能是
那一针的 key 不在词表里，或者那句台词根本没出现在对话里。
"""
from __future__ import annotations

import statistics
from pathlib import Path

import pytest

from hippo.core.store import fact_keys
from hippo.tools.check_fixture import check, load

FIX = Path("tests/fixtures/dialog_200.jsonl")
PINS = Path("tests/fixtures/dialog_200.pins.json")


@pytest.fixture(scope="module")
def corpus():
    return load(FIX)


def test_two_hundred_turns_contiguous(corpus):
    """200 轮，turn 必须是 1..200 连着的。断一个号，窗口切片就错一轮。"""
    rows, _ = corpus
    assert len(rows) == 200
    assert [r["turn"] for r in rows] == list(range(1, 201))
    assert {r["role"] for r in rows} == {"user", "assistant"}


def test_fifty_pins_with_expected_mix(corpus):
    """50 针，五类配比和手册一致。"""
    _, pins = corpus
    assert len(pins) == 50
    kinds: dict[str, int] = {}
    for m in pins.values():
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
    assert kinds == {"fact": 15, "rel": 12, "pref": 10, "plan": 8, "seed": 5}


def test_fact_keys_are_in_closed_vocabulary(corpus):
    """15 条 fact 针的 key 必须落在 fact_keys.yaml 里。

    这条是整组里最值钱的：`set_fact` 对表外 key 是**静默拒收**——
    只写一条 info 日志，返回值里 action=reject。语料写错的话，
    评测会显示"这条事实召回率 0"，而它压根没进过库。
    """
    _, pins = corpus
    table = fact_keys()
    bad = [(pid, m["key"]) for pid, m in pins.items()
           if m["kind"] == "fact" and table.canonical(m["key"]) is None]
    assert bad == []


def test_cross_segment_references(corpus):
    """跨段引用 ≥20。段内重复不算——那只是啰嗦，不是长程记忆。"""
    rows, pins = corpus
    seg_of = {r["turn"]: r["seg"] for r in rows}
    cross = 0
    for m in pins.values():
        s0 = seg_of[m["first_turn"]]
        cross += sum(1 for t in m["mentions"]
                     if t != m["first_turn"] and seg_of[t] != s0)
    assert cross >= 20


def test_every_pin_was_actually_said(corpus):
    """每一针都得真的在对话里说过，而且带问句和答案。

    没说过却写进索引，评测会拿它去问，然后记一次"没记住"——
    冤有头债有主，那是语料的错。
    """
    _, pins = corpus
    for pid, m in pins.items():
        assert m["mentions"], f"{pid} 从未出现"
        assert m["q"] and m["a"], f"{pid} 缺问句或答案"
        assert m["first_turn"] >= 1


def test_check_fixture_reports_clean(corpus):
    """体检函数本身要判干净，且统计口径和生成器一致。"""
    rows, pins = corpus
    st, errs = check(rows, pins)
    assert errs == []
    assert st["turns"] == 200 and st["facts"] == 50
    assert st["cross_refs"] >= 20
    # 用户轮均长要像人说话（30 字上下）；低于 15 字说明语料是电报体
    assert st["user_avg_len"] >= 20
    lens = [len(r["text"]) for r in rows]
    assert statistics.mean(lens) < 60      # 也不能是论文
