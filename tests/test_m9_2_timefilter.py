"""M9.2 验收：时间与状态过滤。

这一节最容易写错的地方，是把它当成"加个 WHERE 条件"。
实际上它有三件事要同时成立：

  1. **默认只给还有效的**。历史值不是垃圾，但也不能默认出现——
     否则"我现在住哪"会答成"你以前住北京"。
  2. **历史查询要能真的返回历史值**。M8 的真机语料里 historical 是 0 条，
     这意味着这条路径不会被日常对话覆盖到。没有测试，它就是死代码。
  3. **"最近"不是时间条件**。"老王最近怎么样"里的"最近"是问候语，
     按 30 天硬过滤会把三个月前的辞职事件砍掉——那正是要回答的事。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from hippo.core.retrieve import AT, HISTORICAL, PLAIN, RECENT, l1_candidates, l1_facts, parse_query
from hippo.core.timeparse import parse_span

TZ = "Asia/Shanghai"
# 固定"现在"，否则测试会在 2026 年底和 2027 年初给出不同结果
NOW = datetime(2026, 11, 5, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo(TZ))


def _ts(y, m, d):
    return int(datetime(y, m, d, tzinfo=__import__("zoneinfo").ZoneInfo(TZ)).timestamp())


def _ev(store, raw, when, *, end=None, weight=0.5, **kw):
    return store.add_event(
        session="s1", raw=raw, summary=raw, keywords=[], event_time=when,
        event_time_end=end, w0=weight, weight=weight, **kw)


# ---------------------------------------------------------------- 状态过滤
def test_default_excludes_superseded(store):
    """默认不含 historical。0 条，不是"少几条"。"""
    old = _ev(store, "我住在北京", _ts(2025, 1, 1))
    new = _ev(store, "我搬到上海了", _ts(2026, 1, 1))
    store.supersede_event(old, new)

    res = l1_candidates(store, "s1", text="我住哪")
    assert old not in res.event_ids
    assert new in res.event_ids


def test_historical_intent_returns_old_value(store):
    """"以前住哪"能返回旧值。"""
    old = _ev(store, "我住在北京", _ts(2025, 1, 1))
    new = _ev(store, "我搬到上海了", _ts(2026, 1, 1))
    store.supersede_event(old, new)

    q = parse_query("我以前住在哪", "s1", store, now=NOW)
    assert q.mode == HISTORICAL
    res = l1_candidates(store, "s1", q, now=NOW)
    assert old in res.event_ids
    assert new in res.event_ids


def test_supersede_self_is_rejected(store):
    """事件不能取代自己。允许的话，它会从默认检索里静默消失。"""
    from hippo.core.store import StoreError

    e = _ev(store, "我住在北京", _ts(2025, 1, 1))
    with pytest.raises(StoreError):
        store.supersede_event(e, e)
    assert e in l1_candidates(store, "s1", text="住哪").event_ids


# ---------------------------------------------------------------- 区间重叠
def test_span_overlap_is_left_closed_right_open(store):
    """区间事件用重叠判断，边界左闭右开。

    事件"去年冬天"= [2025-12-01, 2026-03-01)。
      - 查 2025-12 → 命中（起点落在区间内）
      - 查 2026-03 → **不命中**（事件 end 是开的，3 月已经不属于"去年冬天"）
      - 查 2025-11 → 不命中
    写错成 `event_time >= t_start` 时，第一条会漏；写错成闭区间时，第二条会多。
    """
    e = _ev(store, "去年冬天搬走了", _ts(2025, 12, 1), end=_ts(2026, 3, 1))

    hit = l1_candidates(store, "s1", text="2025年12月", now=NOW)
    assert e in hit.event_ids, "起点落在区间内却没命中"

    miss = l1_candidates(store, "s1", text="2026年3月", now=NOW)
    assert e not in miss.event_ids, "右端开区间失效，3 月不该命中"

    miss2 = l1_candidates(store, "s1", text="2025年11月", now=NOW)
    assert e not in miss2.event_ids


def test_zero_length_span_still_matches(store):
    """`event_time_end == event_time` 的零长区间任何查询都该能命中。

    模型给个点却不给区间时会写成这样。SQL 里直接比 `end > t_start`
    会得到空区间，事件从此不可检索——一条在库里、但永远捞不到的记忆。
    """
    t = _ts(2026, 6, 1)
    e = _ev(store, "六月初交了房租", t, end=t)
    assert e in l1_candidates(store, "s1", text="2026年6月", now=NOW).event_ids


def test_point_event_not_matched_by_later_month(store):
    """点事件 2026-06-01 不该被"2026年7月"命中。"""
    e = _ev(store, "六月初交了房租", _ts(2026, 6, 1))
    assert e not in l1_candidates(store, "s1", text="2026年7月", now=NOW).event_ids


# ---------------------------------------------------------------- 言说动词
def test_speech_verb_mutes_time_filter(store):
    """"我上周说要搬家"——搬家是三个月前的事，上周只是"说"的时间。

    这是 L1 唯一一类"错杀且下游无法补救"的误判：
    按 [上周, 现在) 过滤后，搬家事件不在候选集里，
    而关键词"搬家"再准也只能在候选集内部排序。
    """
    e = _ev(store, "决定搬家", _ts(2026, 8, 1))
    q = parse_query("我上周说要搬家那事", "s1", store, now=NOW)
    assert q.time_muted, "时间词没有被言说动词压住"
    assert q.span is None
    assert e in l1_candidates(store, "s1", q, now=NOW).event_ids


def test_time_word_without_speech_verb_still_filters(store):
    """同样的时间词，没有言说动词就该过滤。对照组，防止压制规则吃掉一切。"""
    e = _ev(store, "决定搬家", _ts(2026, 8, 1))
    q = parse_query("上周搬家的车", "s1", store, now=NOW)
    assert q.time_muted == ""
    assert q.span is not None
    assert e not in l1_candidates(store, "s1", q, now=NOW).event_ids


# ---------------------------------------------------------------- RECENT
def test_recent_does_not_hard_filter(store):
    """D61："最近"只打标不过滤。

    "老王最近怎么样"→ 老王三个月前辞职，30 天窗口会把它砍掉，
    AI 于是答"不记得他近况"——而库里明明有。这条测试如果把
    soft_since 改成硬过滤，会立刻红。
    """
    old = _ev(store, "老王辞职了", _ts(2026, 8, 1))
    q = parse_query("老王最近怎么样", "s1", store, now=NOW)
    assert q.mode == RECENT
    assert q.soft_since is not None and q.soft_since > 0
    res = l1_candidates(store, "s1", q, now=NOW)
    assert old in res.event_ids


def test_explicit_month_still_hard_filters(store):
    """"上个月"有边界，走 AT，照旧硬过滤。和"最近"必须区别对待。"""
    old = _ev(store, "老王辞职了", _ts(2026, 8, 1))
    q = parse_query("上个月发生了什么", "s1", store, now=NOW)
    assert q.mode == AT, f"上个月应该走 AT，实际 {q.mode}"
    assert old not in l1_candidates(store, "s1", q, now=NOW).event_ids


# ---------------------------------------------------------------- facts
def test_facts_default_hides_historical(store):
    """facts 默认只给 valid_to IS NULL 的。"""
    store.set_fact(session="s1", key="city", value="北京", when=_ts(2025, 1, 1))
    store.set_fact(session="s1", key="city", value="上海", when=_ts(2026, 1, 1))

    active = l1_facts(store, "s1")
    assert [f.value for f in active] == ["上海"]

    hist = l1_facts(store, "s1", parse_query("我以前住哪", "s1", store, now=NOW))
    assert sorted(f.value for f in hist) == ["上海", "北京"]


def test_facts_global_kept_for_entity_query(store):
    """entity_id 为空的全局事实，在实体查询里也必须带上。

    不带的话"老王最近怎么样"连"我住哪"都答不出来——
    而用户问老王时，往往下一句就是"他离我远不远"。
    """
    wang = store.upsert_entity(session="s1", canonical="老王", type="person")
    store.set_fact(session="s1", key="city", value="上海", when=_ts(2026, 1, 1))
    store.set_fact(session="s1", key="employer", value="字节",
                   entity_id=wang, when=_ts(2026, 1, 1))

    q = parse_query("老王最近怎么样", "s1", store, now=NOW)
    vals = {f.value for f in l1_facts(store, "s1", q)}
    assert vals == {"上海", "字节"}


# ---------------------------------------------------------------- 注入
def test_query_text_is_parameterized(store):
    """查询里塞 SQL 不该有任何效果。参数化不是"顺手写了 ?"，是必须守住的一条。"""
    e = _ev(store, "正常事件", _ts(2026, 1, 1))
    q = parse_query("'; DROP TABLE events; --", "s1", store, now=NOW)
    res = l1_candidates(store, "s1", q, now=NOW)
    assert e in res.event_ids
    n = store.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    assert n == 1


# ---------------------------------------------------------------- 模式判定
@pytest.mark.parametrize("text,expect", [
    ("我以前住在哪", HISTORICAL),
    ("原来那个公司叫什么", HISTORICAL),
    ("小时候住的房子", HISTORICAL),
    ("我住哪", PLAIN),
    ("上周干了什么", AT),
    ("最近在忙什么", RECENT),
    ("", PLAIN),
])
def test_mode_detection(store, text, expect):
    assert parse_query(text, "s1", store, now=NOW).mode == expect


def test_parse_span_agrees_with_l1_on_last_week(store):
    """L1 的区间必须来自 parse_span，不能自己再写一套。

    M8 修过"上周三落到下周"（事故 53）。如果 L1 另写一套时间解析，
    那个 bug 会以另一种形式复活，而且没人会想到去 M8 的测试里找。
    """
    q = parse_query("上周", "s1", store, now=NOW)
    span = parse_span("上周", base=NOW, tz=TZ)
    assert q.span.start == span.start and q.span.end == span.end
    assert q.span.start == _ts(2026, 10, 26)


def test_self_facts_not_mixed_into_entity_query(store):
    """问老王时，不带用户本人的 facts。

    这一条我一开始写反了：理由是"用户问完老王，下一句经常是他离我远不远"。
    真机上一跑就露馅——用户有 17 条事实，老王有 1 条，问老王时返回 18 条，
    17 条跟老王无关，M13 的 600 token 预算一半浪费在这里。

    "问谁给谁"是自洽的：用户问自己的事时（"我几点睡"）认不出任何实体，
    走无实体分支，那时全部 facts 本来就都会给。
    """
    me = store.upsert_entity(session="s1", canonical="用户", type="person")
    wang = store.upsert_entity(session="s1", canonical="老王", type="person")
    store.set_fact(session="s1", key="city", value="北京",
                   entity_id=me, when=_ts(2026, 1, 1))
    store.set_fact(session="s1", key="commute", value="飞机",
                   entity_id=wang, when=_ts(2026, 1, 1))

    q = parse_query("老王最近怎么样", "s1", store, now=NOW)
    assert q.entity_ids == [wang]
    assert [f.value for f in l1_facts(store, "s1", q)] == ["飞机"]

    # 问自己的事：认不出实体 → 全部给
    q2 = parse_query("我几点睡", "s1", store, now=NOW)
    assert q2.entity_ids == []
    assert {f.value for f in l1_facts(store, "s1", q2)} == {"北京", "飞机"}
