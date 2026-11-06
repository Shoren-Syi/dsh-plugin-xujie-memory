# -*- coding: utf-8 -*-
"""M23.3 懊悔记忆。

验收：rpe≤-0.5 触发 / w0=0.85 / 定向检索命中 / 不进常规 pack。
"""
from __future__ import annotations

import pytest
from datetime import datetime

from hippo.core import ledger
from hippo.core.ledger import KIND_EDIT, recall, register, settle
from hippo.core.pack import pack
from hippo.core.rank import l2_search

T0 = 1_760_000_000


def _accident(store, action="擅自修改了配置文件", predicted=0.85, kind=KIND_EDIT,
              feedback="你怎么又擅自改我配置，下次先问我", ts=T0):
    register(store, "s", action, predicted, kind=kind, now=ts)
    return settle(store, "s", feedback, -1.0, now=ts + 3600)


def test_regret_created_when_rpe_low(store):
    """验收 #1：rpe = -1 - 0.85 = -1.85 ≤ -0.5 → 生成懊悔记忆。"""
    st = _accident(store)
    assert st.rpe <= ledger.REGRET_RPE
    assert st.regret_id is not None
    assert st.is_regret


def test_no_regret_when_rpe_mild(store):
    """自己本来就觉得会挨骂（predicted=-0.9），真被骂了 rpe 只有 -0.1。

    不生成懊悔是对的：这次没让你"意外"。懊悔的价值来自预期落差，
    没有落差就没有可学的东西。
    """
    st = _accident(store, predicted=-0.9)
    assert st.rpe == pytest.approx(-0.1)
    assert st.regret_id is None
    assert store.regrets("s") == []


def test_regret_event_shape(store):
    """验收 #2：kind=regret / w0=0.85 / source=ai / 摘要走模板。"""
    st = _accident(store)
    e = store.get_event(st.regret_id)
    assert e.kind == "regret"
    assert e.w0 == 0.85 and e.weight == 0.85
    assert e.tier == 2                 # hot 让权重自己算，不手写
    assert e.source == "ai"
    # 模板：我{动作}，{反应}，要求下次{怎么做}。
    assert e.summary.startswith("我擅自修改了配置文件，")
    assert "用户说「" in e.summary
    assert e.summary.endswith("。")
    assert "下次先问我" in e.summary
    assert e.summary.count("我我") == 0


def test_keywords_carry_kind_words(store):
    """验收 #3 的前提：keywords 必含动作类型词，否则定向检索无处可查。"""
    st = _accident(store)
    e = store.get_event(st.regret_id)
    kws = e.keywords.split()
    for w in ("擅自", "改文件"):
        assert w in kws, (w, kws)
    assert len(kws) <= 6


def test_targeted_recall_hits(store):
    """验收 #3：打算做同类动作前，能翻出这条教训。"""
    _accident(store)
    got = recall(store, "s", "我想改一下配置文件里的超时")
    assert len(got) == 1
    assert got[0].kind == "regret"


def test_recall_does_not_cross_kinds(store):
    """改文件的教训不该在"我要发消息"时冒出来。

    跨类召回是这类机制最容易出的错：一条万能懊悔反复出现，
    最后 AI 变成"什么都不敢做"。
    """
    _accident(store, kind=KIND_EDIT)
    assert recall(store, "s", "我要给他发条消息") == []
    assert recall(store, "s", "随便聊聊") == []


def test_regret_searchable_but_not_packed(store):
    """验收 #4：常规 pack 0 泄漏——但它本身是可检索的。

    两件事必须同时成立，否则"不进 pack"可以靠"根本查不到"作弊，
    而查不到的懊悔等于没记。
    """
    _accident(store)
    hit = l2_search(store, "s", "改配置文件")
    assert any(it.event.kind == "regret" for it in hit.items), "检索层应该看得到"

    out = pack(store, "s", "我想改配置文件", use_cache=False,
               now=datetime(2026, 1, 1, 12, 0))
    assert "[教训]" not in out.text
    assert "擅自修改了配置文件" not in out.text


def test_lesson_injected_when_action_upcoming(store):
    """真要动手时，教训进 E 段，且排在存疑/计划/情绪之前。"""
    _accident(store)
    out = pack(store, "s", "我想改配置文件的超时设置", use_cache=False,
               upcoming_action="改一下配置文件", now=datetime(2026, 1, 1, 12, 0))
    assert "[教训]" in out.text
    line = next(ln for ln in out.text.split("\n") if ln.startswith("[教训]"))
    assert "擅自修改了配置文件" in line
    assert "下次先问我" in line
    # 数字不上桌：RPE 只活在库里
    assert "-1.85" not in out.text and "0.85" not in out.text
