# -*- coding: utf-8 -*-
"""M78 验收：跨会话身份边界。

原则（对应审阅意见 §4）：
- **用户长期记忆**（事实 / 特征）属于"这个人"，跨会话继承；
- **会话记忆**（事件 / 计划 / 存疑）严格按会话隔离，不串味。
"""
from __future__ import annotations

from hippo.core.pack import pack
from hippo.core.persist import persist_result
from hippo.core.schema import EventOut, ExtractResult, FactOut, TraitOut
from hippo.core.store import Store
from hippo.core.turns import Window


def _window(store: Store, session: str) -> Window:
    turns = store.get_turns(session, limit=6)
    return Window(session=session, idxs=[t.idx for t in turns], token_est=0,
                  has_pending=False, turns=turns)


def _seed_session_a(store: Store) -> None:
    store.add_turns("a", [("user", "我住在杭州，做医疗系统开发", 0)], ts=1_760_000_000)
    persist_result(store, "a", _window(store, "a"), ExtractResult(
        events=[EventOut(raw="我住在杭州，做医疗系统开发",
                         summary="用户住在杭州，做医疗系统开发",
                         keywords=["杭州", "医疗"], importance=4)],
        facts=[FactOut(entity="用户", key="city", value="杭州", confidence=0.9, evidence=[0])],
        traits=[TraitOut(entity="用户", trait="做医疗系统开发", status="observed", evidence=[0])],
    ), now=1_760_000_100)


def test_cross_session_facts_and_traits_inherited(store, cfg):
    store.cfg = cfg
    _seed_session_a(store)
    store.add_turns("b", [("user", "新会话开始", 0)], ts=1_760_000_500)
    out = pack(store, "b", "我住哪？", cfg=cfg, use_cache=False)
    assert "杭州" in out.text, "长期事实应跨会话继承"
    assert "医疗系统开发" in out.text, "角色特征应跨会话继承"


def test_cross_session_can_be_disabled(store, cfg):
    store.cfg = cfg
    cfg.retrieve.cross_session = False
    _seed_session_a(store)
    store.add_turns("b", [("user", "新会话开始", 0)], ts=1_760_000_500)
    out = pack(store, "b", "我住哪？", cfg=cfg, use_cache=False)
    assert "杭州" not in out.text


def test_events_stay_session_scoped(store, cfg):
    store.cfg = cfg
    _seed_session_a(store)
    store.add_turns("b", [("user", "新会话开始", 0)], ts=1_760_000_500)
    out = pack(store, "b", "医疗系统开发的事情进展如何", cfg=cfg, use_cache=False)
    # 事实/特征可以跨；但事件本身（原话/事件记忆）不应从会话 a 串到 b
    assert "用户住在杭州，做医疗系统开发" not in out.text.replace("用户：", "")
