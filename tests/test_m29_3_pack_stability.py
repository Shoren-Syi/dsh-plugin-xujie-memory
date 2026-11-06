# -*- coding: utf-8 -*-
"""M29.3 验收：稳定段版本号与预算收缩的可验证性。

这一节守的是缓存正确性。版本号 `[ctx-v{n}]` 不是装饰——它是给上游缓存
用的键：键一样才会复用前缀。所以它必须等于**实际注入的稳定段字节**的
哈希。原来的实现是在分项/总预算裁 A/B/C **之前**算 hash，裁完照发：
C 段一旦被裁，版本号描述的是裁剪前的字节，实际发出去的是裁剪后的字节，
于是内容已经变了，版本号还在说"没变"。实测 pinned 压到 12 token 时，
注入版本 5623、实际字节哈希 0362。
"""
from __future__ import annotations

import hashlib
import re

import pytest

from hippo.config import Config
from hippo.core import pack as pack_mod
from hippo.core.pack import _targeted_self_fact, pack

_VER = re.compile(r"^\[ctx-v([0-9a-f]{4})\]\n")


def _injected(result) -> dict[str, str]:
    return {b.name: b.text for b in result.blocks}


def _actual_hash(result) -> str:
    """按本轮真正注入的字节重算稳定段哈希：A（去版本行）+ B + C。"""
    blk = _injected(result)
    persona = blk["persona"]
    body = persona.split("\n", 1)[1] if _VER.match(persona) else persona
    stable = body + "\x00" + blk["card"] + "\x00" + blk["pinned"]
    return hashlib.sha1(stable.encode("utf-8")).hexdigest()[:4]


def _version(result) -> str:
    m = _VER.match(_injected(result)["persona"])
    return m.group(1) if m else ""


def test_version_matches_injected_bytes_when_pinned_is_trimmed(store):
    """C 段被分项预算裁掉时，版本号必须描述裁剪后的字节。"""
    store.set_fact(session="s", key="age", value="30岁", when=100)
    for i, key in enumerate(("city", "hometown", "occupation", "employer",
                             "school", "major")):
        store.set_fact(session="s", key=key, value=f"值{i}" * 12, when=100 + i)

    cfg = Config()
    cfg.pack.pinned_tokens = 12          # 强制裁 C 段
    r = pack(store, "s", "我今年多大？", cfg=cfg, use_cache=False)

    pinned_tokens = next(b.tokens for b in r.blocks if b.name == "pinned")
    assert pinned_tokens <= 12, "前提没成立：C 段应该被裁到预算内"
    assert _version(r) == _actual_hash(r)


def test_version_matches_injected_bytes_when_card_is_compacted(store):
    """总预算受压、角色卡被收缩后，版本号同样必须跟上。"""
    store.set_fact(session="s", key="age", value="30岁", when=100)
    ev = store.add_event(session="s", raw="素材", keywords="素材",
                         summary="素材", event_time=900)
    eid = store.upsert_entity(session="s", canonical="用户", type="person")
    for i in range(8):
        store.add_trait(entity_id=eid, trait=f"背景特征{i}" + "内容" * 20,
                        evidence=[ev], confidence=0.9)
    mem = store.add_event(session="s", raw="我最近在学游泳，每周三去一次",
                          keywords="游泳 每周三", summary="在学游泳，每周三去一次",
                          event_time=1000)

    old = pack_mod.TOTAL_BUDGET
    pack_mod.TOTAL_BUDGET = 700          # 逼出总预算受压，但不至于全砍光
    try:
        r = pack(store, "s", "我游泳是什么时候学的？", use_cache=False)
    finally:
        pack_mod.TOTAL_BUDGET = old

    assert "card:budget_compacted" in r.dropped, "前提没成立：角色卡应被压缩"
    assert _version(r) == _actual_hash(r)
    # 压缩不能把本轮的相关记忆挤掉——那是压缩本来的目的。
    assert "[记忆" in _injected(r)["memory"], mem


def test_version_is_stable_across_identical_packs(store):
    """同一状态连打两次，字节必须完全一致（缓存复用的前提）。"""
    store.set_fact(session="s", key="age", value="30岁", when=100)
    ev = store.add_event(session="s", raw="我是做后端的", keywords="后端",
                         summary="做后端", event_time=900)
    eid = store.upsert_entity(session="s", canonical="老王", type="person")
    store.add_trait(entity_id=eid, trait="做后端", evidence=[ev], confidence=0.9)
    store.add_turns("s", [("user", "我今年三十了", 0), ("assistant", "记住了", 0)],
                    ts=1000)

    a = pack(store, "s", "我今年多大？", use_cache=False).text
    b = pack(store, "s", "我今年多大？", use_cache=False).text
    assert a == b


def test_multivalue_self_fact_keeps_up_to_three_values(store):
    """多值槽（忌口）不是矛盾：三条以内全给，超过就截断并标"等"。"""
    for value in ("不吃辣", "不吃香菜", "海鲜过敏"):
        store.set_fact(session="s", key="diet", value=value, when=100)
    out = _targeted_self_fact(store, "s", "我的忌口是什么？")
    assert "不吃辣" in out and "不吃香菜" in out and "海鲜过敏" in out

    for value in ("不喝酒", "不吃内脏"):
        store.set_fact(session="s", key="diet", value=value, when=200)
    out = _targeted_self_fact(store, "s", "我的忌口是什么？")
    assert out.count("、") == 3 and out.endswith("等")


def test_historical_fact_never_injected_even_if_newer_wins(store):
    """写了两条，只有当前那条进来；旧值不能借定向查询漏出去。"""
    store.set_fact(session="s", key="city", value="上海", when=100)
    store.set_fact(session="s", key="city", value="北京", when=200)
    out = _targeted_self_fact(store, "s", "我的住址是什么？")
    assert "北京" in out
    assert "上海" not in out


@pytest.mark.parametrize("question", [
    "我的住址是什么？",
    "我现在的住址是什么？",
    "我目前的住址是多少",
    "我今年多大？",
    "我现在多大？",
])
def test_time_adverb_does_not_block_self_fact(store, question):
    """时间副词和"的"换位置，不该让定向查询失效（曾经失效过）。"""
    store.set_fact(session="s", key="city", value="北京", when=100)
    store.set_fact(session="s", key="age", value="30岁", when=100)
    out = _targeted_self_fact(store, "s", question)
    assert out, f"应命中而没命中：{question}"


@pytest.mark.parametrize("question", [
    "我妈妈今年多大？",
    "我妹妹的年龄是多少？",
    "我以前多大？",
    "我之前的年龄是多少？",
    "你今年多大？",
])
def test_ownership_and_history_guards(store, question):
    """别人的事实和历史事实，定向查询一律不许碰。"""
    store.set_fact(session="s", key="age", value="30岁", when=100)
    assert _targeted_self_fact(store, "s", question) == ""
