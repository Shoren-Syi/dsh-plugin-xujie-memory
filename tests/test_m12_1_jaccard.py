"""M12.1 验收：关键词集与相似度。

手册写的是 Jaccard。实测它在真实对话上**恒为 0**（相邻两句几乎不共享关键词，
用户全用代词），五档阈值给出完全一样的触发率——那就不叫判据了。

所以这一节测的是 `relate` 的三条依据（覆盖率/指代/省略），
以及 `jaccard` 作为对照指标的边界行为。为什么换，见 core/topic.py 的注释。
"""

from __future__ import annotations

import time

from hippo.core.topic import (
    jaccard, last_keywords, relate, save_keywords, user_keywords,
)


def _ev(store, raw, keywords, *, session="s1"):
    return store.add_event(session=session, raw=raw, keywords=keywords,
                           summary=raw)


# ------------------------------------------------------------------ jaccard
def test_jaccard_basic():
    assert jaccard({"a", "b"}, {"b", "c"}) == 1 / 3
    assert jaccard({"a"}, {"a"}) == 1.0


def test_jaccard_empty_is_zero():
    """空集不除零，返回 0。手册验收 #4。"""
    assert jaccard(set(), {"a"}) == 0.0
    assert jaccard({"a"}, set()) == 0.0
    assert jaccard(set(), set()) == 0.0


# ------------------------------------------------------------------ 关键词集
def test_user_keywords_only_known_words(store):
    """只保留库里真的出现过的词。跨词边界的碎片（"租涨"）被词表过滤掉。"""
    _ev(store, "我搬家了，房租涨了两千", "搬家 房租 涨")
    words = user_keywords(store, "s1", "房租涨了多少")
    assert "房租" in words
    assert "租涨" not in words, "跨词边界的 n-gram 不该进关键词集"
    assert words == {"房租"}


def test_user_keywords_includes_entity(store):
    eid = store.upsert_entity(session="s1", canonical="老王", type="person")
    store.conn.execute("UPDATE entities SET aliases = ? WHERE id = ?",
                       ('["王哥"]', eid))
    store.conn.commit()
    _ev(store, "老王搬家", "搬家")
    words = user_keywords(store, "s1", "王哥最近怎么样")
    assert "老王" in words, "用别名问，要归到正名——pack 里写的是正名"


def test_user_keywords_empty_text(store):
    assert user_keywords(store, "s1", "") == set()


# ------------------------------------------------------------------ relate
def test_same_topic_high(store):
    _ev(store, "老王跳槽了", "老王 跳槽")
    prev = user_keywords(store, "s1", "老王跳槽了")
    cur = user_keywords(store, "s1", "老王新公司怎么样")
    score, basis = relate(cur, prev, "老王新公司怎么样")
    assert score == 1.0
    assert basis == "coverage"


def test_different_topic_zero(store):
    _ev(store, "老王跳槽了", "老王 跳槽")
    _ev(store, "明天要下雨", "天气 下雨")
    prev = user_keywords(store, "s1", "老王跳槽了")
    cur = user_keywords(store, "s1", "明天天气怎么样")
    score, _ = relate(cur, prev, "明天天气怎么样")
    assert score == 0.0


def test_anaphora_is_continuation(store):
    """没有内容词但有代词 → 接着说。这是真实对话的常态，也是 Jaccard 失效的原因。"""
    prev = {"老王", "出差"}
    score, basis = relate(set(), prev, "对，他下周去深圳")
    assert score == 1.0
    assert basis == "anaphora"


def test_other_is_not_anaphora(store):
    """"其他"里有"他"，但不是代词。"""
    score, basis = relate(set(), {"老王"}, "其他人都不知道")
    assert basis != "anaphora"


def test_elliptical_question_is_continuation(store):
    """省略主语的短问句 = 接着上面问。"""
    for q in ("什么品种", "去几天", "什么时候交"):
        score, basis = relate(set(), {"猫"}, q)
        assert basis == "elliptical", q
        assert score > 0.25


def test_full_question_with_subject_is_new(store):
    """有主语的完整问句 + 一个已知词都没命中 = 问新东西。"""
    score, basis = relate(set(), {"搬家"}, "你养猫了吗")
    assert basis == "ask"
    assert score == 0.0


def test_idle_when_no_previous_topic(store):
    """本轮有词、上轮窗口空 → 没东西可复用，判切换。"""
    score, basis = relate({"房租"}, set(), "房租多少")
    assert basis == "idle"
    assert score == 0.0


def test_both_empty(store):
    score, basis = relate(set(), set(), "嗯")
    assert basis == "empty"
    assert score == 0.5


def test_relate_fast():
    """手册验收 #3：<1ms。纯集合运算，不该慢。"""
    a = {f"词{i}" for i in range(20)}
    b = {f"词{i}" for i in range(10, 30)}
    t0 = time.perf_counter()
    for _ in range(2000):
        relate(a, b, "随便一句话")
    ms = (time.perf_counter() - t0) * 1000 / 2000
    assert ms < 1.0, f"{ms:.3f}ms"


# ------------------------------------------------------------------ 存取
def test_keywords_roundtrip(store):
    save_keywords(store, "s1", {"搬家", "房租"}, [3, 7])
    assert last_keywords(store, "s1") == {"搬家", "房租"}
    assert store.get_state("s1", "last_pack_entities") == "3 7"


def test_keywords_isolated_by_session(store):
    save_keywords(store, "s1", {"搬家"})
    assert last_keywords(store, "s2") == set()


# ------------------------------------------------------------------ 共现
def test_cooccur_expands_same_event(store):
    """两句没有共同词，但同属一条记忆——"我搬家了" → "房租涨了多少"。

    词面判断在这里必然失败（一个共同的词都没有），而它们确实是同一件事：
    那条记忆的关键词是"搬家 朝阳 海淀 房租"。
    """
    _ev(store, "我搬到海淀，房租涨了两千", "搬家 海淀 房租")
    prev = user_keywords(store, "s1", "我搬家了")
    cur = user_keywords(store, "s1", "房租涨了多少")
    plain, _ = relate(cur, prev, "房租涨了多少")
    assert plain == 0.0, "词面上确实没有交集"
    kept, basis = relate(cur, prev, "房租涨了多少", store=store)
    assert basis == "cooccur"
    assert kept > 0.25


def test_cooccur_requires_same_event(store):
    """两条互不相关的事件里的词，不算同一话题。"""
    _ev(store, "我搬家了", "搬家 海淀")
    _ev(store, "猫不吃东西", "猫 年糕")
    assert store.co_occur({"搬家"}, {"猫"}) is False


def test_cooccur_empty_sides(store):
    assert store.co_occur(set(), {"a"}) is False
    assert store.co_occur({"a"}, set()) is False
