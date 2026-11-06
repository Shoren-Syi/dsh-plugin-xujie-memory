"""M12.2 验收：强制触发。

三个条件（新实体 / 回忆词 / 话题切换）任一满足就检索，并把 reason 记下来。
reason 是这套闸门唯一可观测的东西——"这个月花了多少钱在检索上"
要靠它拆开：换话题花的、回忆花的是两笔不同的账。
"""

from __future__ import annotations

from hippo.core.topic import (
    LAST_KEY, TURN_KEY, new_entity_hint, save_keywords, should_retrieve,
)


def _ev(store, raw, keywords, *, session="s1"):
    return store.add_event(session=session, raw=raw, keywords=keywords,
                           summary=raw)


def _seed(store):
    _ev(store, "我昨天搬家了，从朝阳搬到海淀", "搬家 朝阳 海淀")
    _ev(store, "我养了只猫叫年糕", "猫 年糕 宠物")


# ------------------------------------------------------------------ 新实体
def test_new_entity_detected(store):
    assert new_entity_hint(store, "s1", "我朋友老张下周结婚") == ["老张"]
    assert new_entity_hint(store, "s1", "我老板叫李总") == ["李总"]


def test_known_entity_not_new(store):
    store.upsert_entity(session="s1", canonical="老王", type="person")
    assert new_entity_hint(store, "s1", "我朋友老王来了") == []


def test_new_entity_ignores_verbs(store):
    """"我朋友说好" 不该被切成 "说好" 这个"名字"。"""
    assert new_entity_hint(store, "s1", "我朋友说好要来") == []
    assert new_entity_hint(store, "s1", "我朋友都是这样") == []


def test_new_entity_triggers(store):
    _seed(store)
    save_keywords(store, "s1", {"搬家"})
    tr = should_retrieve(store, "s1", "我朋友老张下周结婚")
    assert tr.fire
    assert tr.reason == "new_entity"
    assert "老张" in tr.signals


# ------------------------------------------------------------------ 回忆词
def test_recall_marker_forces_deep(store):
    _seed(store)
    save_keywords(store, "s1", {"搬家"})
    tr = should_retrieve(store, "s1", "你还记得我上次说的搬家吗")
    assert tr.fire
    assert tr.reason == "recall"
    assert tr.deep is True, "回忆词必须连 L3：用户要的是想起来，不是知道有这回事"


def test_recall_beats_new_entity(store):
    """两个都命中时报 recall——它决定 deep，代价更高，必须显式。"""
    _seed(store)
    save_keywords(store, "s1", {"搬家"})
    tr = should_retrieve(store, "s1", "你还记得我朋友老张吗")
    assert tr.reason == "recall"


# ------------------------------------------------------------------ 冷启动
def test_cold_start(store):
    _seed(store)
    tr = should_retrieve(store, "s1", "我搬到哪了")
    assert tr.fire
    assert tr.reason == "cold_start"


def test_idle_not_cold_start(store):
    """存过关键词但是空的（上句是"嗯"），不算冷启动。"""
    _seed(store)
    save_keywords(store, "s1", set())
    tr = should_retrieve(store, "s1", "我搬到哪了")
    assert tr.reason != "cold_start"


# ------------------------------------------------------------------ 延续
def test_same_entity_continues(store):
    """上一轮和这一轮提到同一个人 → 铁定接着聊，不看词面。"""
    eid = store.upsert_entity(session="s1", canonical="老王", type="person")
    _ev(store, "老王去深圳出差", "老王 深圳 出差")
    save_keywords(store, "s1", {"深圳"}, [eid])
    tr = should_retrieve(store, "s1", "老王什么时候回来")
    assert tr.fire is False
    assert tr.reason == "continue"
    assert "同实体" in tr.signals


def test_continue_when_same_topic(store):
    _seed(store)
    save_keywords(store, "s1", {"搬家", "朝阳", "海淀"})
    tr = should_retrieve(store, "s1", "海淀那边房租贵吗")
    assert tr.fire is False
    assert tr.reason == "continue"


def test_switch_on_new_topic(store):
    _seed(store)
    _ev(store, "明天要下雨", "天气 下雨")
    save_keywords(store, "s1", {"搬家", "朝阳", "海淀"})
    tr = should_retrieve(store, "s1", "明天天气怎么样")
    assert tr.fire
    assert tr.reason == "switch"


# ------------------------------------------------------------------ 记账
def test_reason_values_cover_all(store):
    """三种值都要能出现——否则 metrics 里只有一个值，等于没记。"""
    _seed(store)
    seen = set()
    save_keywords(store, "s1", {"搬家"})
    seen.add(should_retrieve(store, "s1", "我朋友老张来了").reason)
    seen.add(should_retrieve(store, "s1", "你还记得吗").reason)
    seen.add(should_retrieve(store, "s1", "明天天气怎么样").reason)
    assert {"new_entity", "recall", "switch"} <= seen


def test_turn_auto_increments(store):
    _seed(store)
    should_retrieve(store, "s1", "第一句")
    should_retrieve(store, "s1", "第二句")
    assert store.get_state("s1", TURN_KEY) == "2"


def test_fire_records_turn(store):
    _seed(store)
    tr = should_retrieve(store, "s1", "我搬到哪了")
    assert tr.fire
    assert store.get_state("s1", LAST_KEY) == "1"


def test_throttle_off_by_default(store):
    """默认不节流。实测节流挡掉的多是真换话题（R-12.3）。"""
    _seed(store)
    save_keywords(store, "s1", {"搬家"})
    a = should_retrieve(store, "s1", "明天天气怎么样")
    b = should_retrieve(store, "s1", "今天吃什么好")
    assert a.fire and b.fire
    assert b.reason != "throttled"


def test_throttle_when_enabled(store):
    """开关打开时，间隔内的弱信号会被挡下，但强信号不受影响。"""
    _seed(store)
    save_keywords(store, "s1", {"搬家"})
    first = should_retrieve(store, "s1", "明天天气怎么样", interval=3)
    assert first.fire
    second = should_retrieve(store, "s1", "今天吃什么好", interval=3)
    assert second.fire is False
    assert second.reason == "throttled"
    assert second.suppressed
    # 强信号无视间隔
    third = should_retrieve(store, "s1", "你还记得我说过什么吗", interval=3)
    assert third.fire and third.reason == "recall"
