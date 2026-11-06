"""M7.2 trait 与证据链。

四条规矩：
  没有证据不许落库；重复不膨胀；矛盾要失效；证据要能追回原文。
"""

from __future__ import annotations

import pytest

from hippo.core.schema import TraitOut
from hippo.core.store import NoEvidenceError


def _eid(store, name="老王"):
    return store.resolve_entity(session="s", name=name).target_id


def test_no_evidence_is_rejected(store):
    eid = _eid(store)
    with pytest.raises(NoEvidenceError):
        store.add_trait(entity_id=eid, trait="可能对现状不满", evidence=[])


def test_duplicate_trait_does_not_inflate(store):
    eid = _eid(store)
    e1 = store.add_event(session="s", raw="我在创业公司做后端", entities=[eid])
    e2 = store.add_event(session="s", raw="我们公司最近融资了", entities=[eid])

    t1 = store.add_trait(entity_id=eid, trait="在创业公司做后端", evidence=[e1], confidence=0.6)
    t2 = store.add_trait(entity_id=eid, trait="在创业公司做后端", evidence=[e2], confidence=0.6)
    assert t1 == t2
    ts = store.list_traits(eid)
    assert len(ts) == 1
    assert ts[0].evidence == [e1, e2]
    assert ts[0].confidence == pytest.approx(0.7)


def test_confidence_caps_at_095(store):
    eid = _eid(store)
    ev = store.add_event(session="s", raw="x", entities=[eid])
    for _ in range(10):
        store.add_trait(entity_id=eid, trait="做后端", evidence=[ev], confidence=0.9)
    assert store.list_traits(eid)[0].confidence == pytest.approx(0.95)


def test_conflicting_slot_expires_old(store):
    """换工作：新的一条占 employer 槽，旧条自动关窗。

    这两句话字面毫无关系（"在字节" / "在创业公司"），
    字符串比不出矛盾，只能靠槽位。
    """
    eid = _eid(store)
    e1 = store.add_event(session="s", raw="我在字节做后端", entities=[eid])
    e2 = store.add_event(session="s", raw="我跳槽了，现在在创业公司", entities=[eid])

    old = store.add_trait(entity_id=eid, trait="在字节做后端", evidence=[e1],
                          confidence=0.8, slot="employer")
    new = store.add_trait(entity_id=eid, trait="在创业公司做后端", evidence=[e2],
                          confidence=0.8, slot="employer")

    active = store.list_traits(eid)
    assert [t.id for t in active] == [new]
    allt = store.list_traits(eid, active_only=False)
    old_row = next(t for t in allt if t.id == old)
    assert old_row.valid_to is not None
    # 旧条不删：三个月后问"他以前在哪工作"还要查得到
    assert old_row.trait == "在字节做后端"


def test_empty_slot_does_not_clobber(store):
    """给不出槽位的特征不许互相覆盖。

    如果没有这条，模型每轮吐三条"话少但心细"式的特征，
    角色卡最后只会剩最后一条。
    """
    eid = _eid(store)
    ev = store.add_event(session="s", raw="x", entities=[eid])
    store.add_trait(entity_id=eid, trait="话少但心细", evidence=[ev])
    store.add_trait(entity_id=eid, trait="对朋友很讲义气", evidence=[ev])
    assert len(store.list_traits(eid)) == 2


def test_slot_normalized_by_schema():
    t = TraitOut(entity="老王", trait="做后端", slot="Employer ")
    assert t.slot == "employer"
    t2 = TraitOut(entity="老王", trait="做后端", slot="在哪工作")
    assert t2.slot == ""      # 非 ASCII 一律清空，不许污染槽位


def test_evidence_traces_back_to_raw(store):
    eid = _eid(store)
    raw = "我在创业公司做后端，压力挺大的"
    ev = store.add_event(session="s", raw=raw, entities=[eid])
    store.add_trait(entity_id=eid, trait="在创业公司做后端", evidence=[ev])

    t = store.list_traits(eid)[0]
    got = store.get_event(t.evidence[0])
    assert got is not None and got.raw == raw


def test_close_trait_is_idempotent(store):
    eid = _eid(store)
    ev = store.add_event(session="s", raw="x", entities=[eid])
    tid = store.add_trait(entity_id=eid, trait="做后端", evidence=[ev])
    assert store.close_trait(tid) is True
    assert store.close_trait(tid) is False


# ---------------------------------------------------------------- 合并 × 索引
def test_merge_entities_does_not_break_trait_slot_index(store):
    """两个实体合并时，同槽位的 active trait 不能撞 007 的唯一索引。

    现场：narrative 和 multiperson 各建了一个"老王"，都有一条 slot=job 的特征，
    文案还写得不一样（"在字节做后端" / "后端工程师"）。按文案判重判定为
    "不重复"，直接 UPDATE entity_id，撞 `(entity_id, slot)` 索引，整个合并失败。

    合并失败不是最糟的——最糟的是它发生在事务里，用户看到"合并失败"，
    但不知道有多少条已经迁过去了。
    """
    a = store.upsert_entity(session="s1", canonical="老王", type="person")
    b = store.upsert_entity(session="s1", canonical="老王~2", type="person")
    e1 = store.add_event(session="s1", raw="老王在字节做后端", event_time=1700000000)
    e2 = store.add_event(session="s1", raw="老王是后端工程师", event_time=1700000001)

    store.add_trait(entity_id=a, trait="在字节做后端", evidence=[e1], slot="job")
    store.add_trait(entity_id=b, trait="后端工程师", evidence=[e2], slot="job")

    stats = store.merge_entities(b, a)
    assert stats["traits_merged"] >= 1

    actives = [t for t in store.list_traits(a) if t.slot == "job"]
    assert len(actives) == 1, "同槽位合并后必须只剩一条"
    assert set(actives[0].evidence) == {e1, e2}, "两条的证据都要留下"


def test_merge_entities_does_not_break_fact_single_index(store):
    """两个实体合并时，同 key 的 active fact 不能撞 008 的唯一索引。

    同一现场的 facts 版本：两个"老王"各有一条 employer。直接改 entity_id
    会撞 `idx_facts_single_active`。

    正确行为不是"删掉一条"，是**旧的按 valid_to 收尾**——
    删了的话"老王以前在腾讯"就永久消失，而历史查询正是要它。
    """
    a = store.upsert_entity(session="s1", canonical="老王", type="person")
    b = store.upsert_entity(session="s1", canonical="老王~2", type="person")
    store.set_fact(session="s1", key="employer", value="腾讯",
                   entity_id=a, when=1700000000)
    store.set_fact(session="s1", key="employer", value="字节",
                   entity_id=b, when=1700000002)

    store.merge_entities(b, a)

    active = [f.value for f, _who in store.list_facts("s1", include_historical=False)
              if f.entity_id == a and f.key == "employer"]
    assert active == ["字节"], "更新的那条应该留下"

    hist = [f.value for f, _who in store.list_facts("s1", include_historical=True)
            if f.entity_id == a and f.key == "employer"]
    assert set(hist) == {"腾讯", "字节"}, "旧值必须留成 historical，不能删"
