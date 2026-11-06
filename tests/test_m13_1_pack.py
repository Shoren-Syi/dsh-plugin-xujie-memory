"""M13.1 验收：预算表与裁剪。

这一节只有一个真问题：**超预算时丢谁**。

丢错了不会崩，只会让 AI 慢慢变笨或者忘掉用户明说过的事，而这两种
都不会报错。所以每条裁剪规则都要有测试钉住"先丢哪个、后丢哪个"。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from hippo.core.pack import TOTAL_BUDGET, pack
from hippo.core.rank import MemoryItem
from hippo.core.models import Event

NOW = datetime(2026, 11, 5, 14, 0)


def _ev(store, raw, keywords="", summary="", *, tier=2, pinned=0,
        entities=None, session="s1", when=None):
    return store.add_event(
        session=session, raw=raw, keywords=keywords, summary=summary or raw,
        tier=tier, pinned=pinned, entities=entities,
        event_time=int((when or datetime(2026, 11, 4, 12, 0)).timestamp()))


def _pack(store, text="我搬家了", **kw):
    return pack(store, "s1", text, now=kw.pop("now", NOW), **kw)


# ------------------------------------------------------------------ 预算
def test_total_budget_never_exceeded(store):
    for i in range(40):
        _ev(store, f"第{i}条记忆，" + "很长的一段描述" * 6,
            keywords=" ".join(f"词{i}_{j}" for j in range(6)),
            summary="摘要" * 40, tier=1 + (i % 3))
    r = _pack(store, "我搬家了")
    assert r.total_tokens <= TOTAL_BUDGET
    assert r.total_tokens == sum(b.tokens for b in r.blocks) or \
        abs(r.total_tokens - sum(b.tokens for b in r.blocks)) < 40


def test_all_six_blocks_present(store):
    r = _pack(store)
    assert [b.name for b in r.blocks] == [
        "persona", "card", "pinned", "memory", "dynamic", "anchor"]


def test_empty_db_still_packs(store):
    """空库也要能装出人设和锚——服务刚起来时就是空的。"""
    r = _pack(store, "你好")
    assert r.total_tokens > 0
    assert r.by_name["persona"].used
    assert r.by_name["anchor"].used


def test_dropped_is_logged(store):
    """裁了就要记。不记的话"AI 怎么不记得了"永远查不出来。

    最容易超的是 B 段（角色卡）：人一多就顶。D 段反而不会超——
    L2 已经按 `memory_budget_ratio` 装填过了，到 pack 这里通常只有 360。
    """
    ev = _ev(store, "素材", keywords="素材")
    for i in range(30):
        eid = store.upsert_entity(session="s1", canonical=f"人物{i:02d}", type="person")
        for j in range(4):
            store.add_trait(entity_id=eid, trait=f"特征{j}" + "描述" * 6,
                            evidence=[ev], status="observed",
                            confidence=0.8, slot=f"s{j}")
    r = _pack(store, "搬家")
    assert r.dropped, "超预算时必须留下裁剪记录"
    assert any(d.startswith("card:") for d in r.dropped)


def test_memory_trimmed_before_persona(store):
    """D 先被砍，A 最后。人设没了就不是那个人了。"""
    for i in range(60):
        _ev(store, f"第{i}条，" + "细节" * 60, keywords=f"词{i} 搬家",
            summary="摘要" * 40, tier=1)
    r = _pack(store, "搬家")
    assert r.by_name["persona"].tokens > 150, "人设不该被砍到这个程度"
    assert r.by_name["memory"].tokens < 600


# ------------------------------------------------------------------ 确定性
def test_deterministic_same_input(store):
    _ev(store, "我搬家了", keywords="搬家")
    a = _pack(store, "我搬家了")
    b = _pack(store, "我搬家了")
    assert a.text == b.text
    assert a.digest == b.digest


def test_anchor_only_to_minute(store):
    """带秒的话每分钟都变，缓存全碎。"""
    a = pack(store, "s1", "你好", now=datetime(2026, 11, 5, 14, 0, 3))
    b = pack(store, "s1", "你好", now=datetime(2026, 11, 5, 14, 0, 59))
    assert a.by_name["anchor"].text == b.by_name["anchor"].text
    assert ":" in a.by_name["anchor"].text
    assert a.by_name["anchor"].text.count(":") == 1, "只该有一个冒号（时分）"


def test_no_float_in_text(store):
    """浮点数进 prompt 会因为 0.7000000001 这种问题让字节变来变去。"""
    _ev(store, "我搬家了", keywords="搬家")
    text = _pack(store, "我搬家了").text
    for bad in ("0.5", "0.75", "score"):
        assert bad not in text


# ------------------------------------------------------------------ pinned
def test_pinned_never_dropped(store):
    for i in range(40):
        _ev(store, f"第{i}条，" + "细节" * 60, keywords=f"词{i} 搬家", tier=1)
    eid = _ev(store, "我妈住院了，下周一手术", keywords="妈 住院 手术",
              summary="用户母亲住院，下周一手术", pinned=1)
    r = _pack(store, "搬家")
    assert "住院" in r.text, "钉住的事件被裁掉了"
    assert eid in [it.id for it in r.items] or "住院" in r.by_name["pinned"].text


# ------------------------------------------------------------------ 角色卡
def test_card_sorted_by_trait_count(store):
    a = store.upsert_entity(session="s1", canonical="老王", type="person")
    b = store.upsert_entity(session="s1", canonical="小李", type="person")
    e1 = _ev(store, "老王的事", keywords="老王")
    for i in range(4):
        store.add_trait(entity_id=a, trait=f"特征{i}", evidence=[e1],
                        status="observed", confidence=0.8, slot=f"s{i}")
    store.add_trait(entity_id=b, trait="怕坐飞机", evidence=[e1],
                    status="observed", confidence=0.8, slot="fear")
    text = _pack(store).by_name["card"].text
    assert text.index("老王") < text.index("小李"), "特征多的排前面"


def test_card_inferred_marked(store):
    eid = store.upsert_entity(session="s1", canonical="老王", type="person")
    ev = _ev(store, "老王的事", keywords="老王")
    store.add_trait(entity_id=eid, trait="可能有点内向", evidence=[ev],
                    status="inferred", confidence=0.8, slot="personality")
    text = _pack(store).by_name["card"].text
    assert "（我的推测）" in text


# ------------------------------------------------------------------ 事实
def test_facts_dedup_by_key(store):
    """同一个 key 有多条 active 时取最新那条。

    真机库里有三条同时 active 的 `commute`（飞机/步行/高铁），是 M8 的遗留。
    原样倒进 prompt 会让模型以为用户既坐飞机又步行。
    """
    store.set_fact(session="s1", key="city", value="朝阳", when=100)
    store.set_fact(session="s1", key="city", value="海淀", when=200)
    text = _pack(store).by_name["pinned"].text
    assert "海淀" in text
    assert "朝阳" not in text, "旧值不该出现"


# ------------------------------------------------------------------ 记忆块
def test_cold_tier_only_keywords(store):
    _ev(store, "很久以前的搬家", keywords="搬家 杭州", summary="用户搬到杭州",
        tier=3)
    r = _pack(store, "搬家")
    mem = r.by_name["memory"].text
    assert "关键词：搬家 杭州" in mem
    assert "摘要：" not in mem, "cold 档只给关键词"


def test_hot_tier_has_summary(store):
    _ev(store, "昨天搬家了", keywords="搬家", summary="用户昨天搬家", tier=1)
    mem = _pack(store, "搬家").by_name["memory"].text
    assert "摘要：用户昨天搬家" in mem


# ------------------------------------------------------------------ 复用
def test_reuse_when_results_overlap(store):
    """同样的查询连跑两次，第二次应当复用 D 段（字节不变 → 缓存命中）。"""
    _ev(store, "我搬家了", keywords="搬家 海淀", summary="用户搬家")
    a = _pack(store, "我搬家了")
    b = _pack(store, "我搬家了")
    assert b.reused is True
    assert a.by_name["memory"].text == b.by_name["memory"].text


def test_no_reuse_when_topic_changes(store):
    _ev(store, "我搬家了", keywords="搬家 海淀", summary="用户搬家")
    _ev(store, "猫不吃东西", keywords="猫 年糕", summary="猫不吃东西")
    _pack(store, "我搬家了")
    r = _pack(store, "猫最近怎么样")
    assert r.reused is False


def test_reuse_keeps_memory_bytes(store):
    """复用必须逐字复用——重新渲染一遍哪怕内容一样，顺序也可能变。"""
    for i in range(6):
        _ev(store, f"搬家第{i}条", keywords="搬家 细节", summary=f"搬家第{i}条")
    a = _pack(store, "搬家")
    b = _pack(store, "搬家")
    assert a.by_name["memory"].text == b.by_name["memory"].text
    assert b.reused


# ------------------------------------------------------------------ 状态回填
def test_state_saved_for_next_turn(store):
    """漏了这一步，下一轮永远 cold_start（每轮全量检索 + 缓存全废）。"""
    _ev(store, "我搬家了", keywords="搬家")
    _pack(store, "我搬家了")
    assert store.get_state("s1", "last_pack_keywords")
    assert store.get_state("s1", "last_pack_d")
    assert store.get_state("s1", "last_pack_ids")
