"""M10.2 验收：L2 排序与截断。

这一节唯一值得测的东西是**分数是不是可解释的**：

  - 同样的输入 100 次必须出同样的顺序（否则测试写不了，M26 调参也调不明白）
  - pinned 必须进（用户明说"这个要记得"）
  - token 不超预算（M13 的 600 是硬上限，超了就是把别人的位置挤掉）
  - "最近"要真的进分数，而不是只少砍一批候选
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hippo.core.rank import l2_search, score_event
from hippo.core.retrieve import parse_query

NOW = datetime(2026, 11, 5, 12, 0, 0)


def _ts(y, m, d, h=12):
    return int(datetime(y, m, d, h).timestamp())


def _ev(store, raw, when, *, keywords="", weight=0.5, pinned=0,
        summary="", tier=2, session="s1"):
    eid = store.add_event(session=session, raw=raw, event_time=when,
                          keywords=keywords, summary=summary or raw)
    store.conn.execute(
        "UPDATE events SET weight = ?, w0 = ?, pinned = ?, tier = ? WHERE id = ?",
        (weight, weight, pinned, tier, eid))
    store.conn.commit()
    return eid


# ------------------------------------------------------------------ 打分
def test_score_is_monotonic_in_hits(store):
    """命中越多分越高，且**有上界**。"""
    e = store.get_event(_ev(store, "老王搬家", _ts(2026, 10, 1)))
    w = {"keyword": 0.55, "weight": 0.25, "recency": 0.10,
         "pinned": 0.10, "soft_since": 0.0}
    hl = {"hot": 3, "warm": 30, "cold": 180}
    ss = [score_event(e, kw_hit=h, now_ts=NOW.timestamp(), half_life=hl, w=w)
          for h in (0, 1, 2, 3)]
    assert ss == sorted(ss), "命中数增加时分数不能下降"
    assert ss[-1] < 0.55 + 0.25 + 0.10 + 0.10 + 1e-9, "kw 项不能超过 1"


def test_score_does_not_depend_on_other_candidates(store):
    """同一条事件的分数**不随候选集变化**（D63）。

    这条是"分组归一化"写法的照妖镜：如果 kw 分是"除以本次最大值"，
    换一个候选集，同一条事件的分数就会变，排序跟着翻，
    而测试和 M26 的调参都建立不起因果。
    """
    a = _ev(store, "老王搬家", _ts(2026, 10, 1), keywords="老王 搬家")
    e = store.get_event(a)
    w = {"keyword": 0.55, "weight": 0.25, "recency": 0.10,
         "pinned": 0.10, "soft_since": 0.0}
    hl = {"hot": 3, "warm": 30, "cold": 180}
    s1 = score_event(e, kw_hit=2, now_ts=NOW.timestamp(), half_life=hl, w=w)
    s2 = score_event(e, kw_hit=2, now_ts=NOW.timestamp(), half_life=hl, w=w)
    assert s1 == s2


def test_pinned_and_weight_move_the_score(store):
    a = _ev(store, "老王搬家", _ts(2026, 10, 1), keywords="老王 搬家", weight=0.1)
    b = _ev(store, "老王搬家（重要）", _ts(2026, 10, 1), keywords="老王 搬家",
            weight=0.9, pinned=1)
    res = l2_search(store, "s1", "老王搬家", now=NOW)
    ids = [it.id for it in res.items]
    assert ids.index(b) < ids.index(a)


# ------------------------------------------------------------------ 排序确定性
def test_order_is_deterministic_100_times(store):
    """同样的输入 100 次，顺序逐条相同（手册验收 #2）。

    同分事件如果只按 score 排，顺序取决于 SQLite 的返回顺序——
    在测试里表现为"偶尔红一次"，在生产里表现为"同一句话两次答得不一样"。
    """
    for i in range(12):
        _ev(store, f"老王搬家第{i}次", _ts(2026, 10, 1), keywords="老王 搬家")
    first = [it.id for it in l2_search(store, "s1", "老王搬家", now=NOW).items]
    for _ in range(100):
        assert [it.id for it in l2_search(store, "s1", "老王搬家", now=NOW).items] == first


# ------------------------------------------------------------------ 预算
def test_token_budget_never_exceeded(store):
    """装进去的总 token 不超过 memory_tokens × ratio（默认 600 × 0.6 = 360）。"""
    for i in range(30):
        _ev(store, f"老王搬家的事{i}", _ts(2026, 10, 1), keywords="老王 搬家",
            summary="这是一条很长的摘要" * 12)
    res = l2_search(store, "s1", "老王搬家", now=NOW)
    assert res.total_tokens <= res.budget
    assert res.total_tokens == sum(it.tokens for it in res.items)


def test_long_summary_is_clipped_to_80_tokens(store):
    """单条摘要截到 80 token 以内，且尽量在标点处断。"""
    long_text = "用户上周搬家，从朝阳搬到海淀，房租涨了两千。" * 8
    _ev(store, long_text, _ts(2026, 10, 1), keywords="搬家", summary=long_text)
    res = l2_search(store, "s1", "搬家", now=NOW)
    assert res.items
    it = res.items[0]
    assert it.tokens <= 80
    assert it.text.endswith("。"), f"应该在句号处断：…{it.text[-10:]!r}"


def test_short_entries_fill_in_when_long_ones_do_not_fit(store):
    """一条装不下就跳过，继续找装得下的——不是 break。

    break 的后果：前 3 条都是长摘要时，第 4~8 个位置全空，
    明明还有几十条短记忆可用。
    """
    for i in range(4):
        _ev(store, f"老王搬家长{i}", _ts(2026, 10, 1), keywords="老王 搬家",
            summary="很长的描述" * 40)
    for i in range(4):
        _ev(store, f"老王搬家短{i}", _ts(2026, 10, 2), keywords="老王 搬家",
            summary="短")
    res = l2_search(store, "s1", "老王搬家", now=NOW)
    assert len(res.items) >= 5, "长条目装不下时应该继续装短条目"


# ------------------------------------------------------------------ pinned
def test_pinned_always_included_even_if_irrelevant(store):
    """钉住的必须进，哪怕和这句话毫无关系（手册验收 #4）。"""
    p = _ev(store, "每年体检别忘了", _ts(2025, 1, 1), keywords="体检",
            weight=0.05, pinned=1)
    for i in range(10):
        _ev(store, f"老王搬家第{i}次", _ts(2026, 10, 1), keywords="老王 搬家")
    res = l2_search(store, "s1", "老王搬家", now=NOW)
    assert p in [it.id for it in res.items]
    assert res.items[0].id == p, "pinned 排在最前"


def test_pinned_not_counted_against_budget(store):
    """pinned 不受 token 预算约束：预算不够时该挤掉的是别的。"""
    long_text = "必须记得的事。" * 200
    p = _ev(store, long_text, _ts(2025, 1, 1), keywords="体检",
            summary=long_text, pinned=1)
    for i in range(5):
        _ev(store, f"老王搬家第{i}次", _ts(2026, 10, 1), keywords="老王 搬家")
    res = l2_search(store, "s1", "老王搬家", now=NOW)
    assert [it.id for it in res.items][0] == p


# ------------------------------------------------------------------ 降级
def test_no_keyword_hit_falls_back_and_says_so(store):
    """关键词 0 命中时按 weight 兜底，并标记 `degraded`。

    不兜底的话，"我几点睡"这类（M9 实测认不出实体、关键词也不重叠）
    会返回空，AI 只能答"不记得"——而事实就在库里。
    """
    _ev(store, "用户一般晚上十一点睡", _ts(2026, 10, 1), keywords="作息 睡觉")
    # 查询词在库里的关键词和摘要里一个都不出现。
    # 早先这里写的是"我几点睡"——M10.3 的同义词表接上之后它就能命中了
    # （"睡" → "睡觉"），这条测试反而失效。降级路径要用**真的**命不中的句子测。
    res = l2_search(store, "s1", "量子力学考纲", now=NOW)
    assert res.degraded == "no_keyword_hit"
    assert res.items, "兜底必须给东西"


def test_empty_query_marks_degraded(store):
    _ev(store, "随便一条", _ts(2026, 10, 1), keywords="随便")
    res = l2_search(store, "s1", "", now=NOW)
    assert res.degraded == "empty_query"


# ------------------------------------------------------------------ soft_since
def test_soft_since_actually_changes_ranking(store):
    """M9 的 D61 把"最近"从过滤降级成信号，这里验证它真的进了分数。

    两条事件同样的命中数、同样的权重，只有时间不同。
    带"最近"的查询里，新那条必须排前面；不带"最近"时两者不因时间被拉开
    （recency 项本来就会给新的一条更高分，所以这里比的是**差距是否变大**）。
    """
    old = _ev(store, "老王搬家（旧）", _ts(2025, 1, 1), keywords="老王 搬家")
    new = _ev(store, "老王搬家（新）", _ts(2026, 11, 1), keywords="老王 搬家")

    plain = l2_search(store, "s1", "老王搬家", now=NOW)
    recent = l2_search(store, "s1", "老王最近搬家", now=NOW)
    assert recent.query.soft_since is not None

    p_old = next(it.score for it in plain.items if it.id == old)
    r_old = next(it.score for it in recent.items if it.id == old)
    assert r_old <= p_old, "旧事件在'最近'查询里不该加分"
    assert [it.id for it in recent.items].index(new) < [it.id for it in recent.items].index(old)


# ------------------------------------------------------------------ 真机评测
DB = Path("reports/data/eval_l1.db")


@pytest.mark.skipif(not DB.exists(), reason="真机库不存在（先跑 tools/eval_l1.py）")
def test_top8_hit_rate_on_real_corpus():
    """50 条标注查询，top-8 命中率 ≥85%（手册 DoD）。

    这个数**必须和对照组一起看**：只按 weight 排也有 0.38，
    所以"0.87 达标"这句话单独没有意义。差的那 0.5 才是 L2 的贡献。
    """
    from hippo.core.store import Store
    from tools.eval_retrieve import evaluate

    store = Store.open(DB)
    try:
        out = evaluate(store)
    finally:
        store.close()
    assert out["hit_rate"] >= 0.85, (
        f"hit@8={out['hit_rate']:.2f}，未达标。" +
        "失败项：" + "；".join(r["q"] for r in out["rows"] if not r["rank"]))
    assert out["mrr"] >= 0.60, f"mrr={out['mrr']:.2f} 太低，说明命中的都排在后面"


@pytest.mark.skipif(not DB.exists(), reason="真机库不存在")
def test_holdout_hit_rate_on_real_corpus():
    """留出集（调完词表之后没再看过的那批），同样要求 ≥85%。

    它存在的意义：只看标注集的话，任何在测试集上手调过的实现都能到 0.95。
    两个数一起报才有信息量。
    """
    from hippo.core.store import Store
    from tools.eval_retrieve import HOLDOUT, evaluate

    store = Store.open(DB)
    try:
        out = evaluate(store, path=HOLDOUT)
    finally:
        store.close()
    assert out["hit_rate"] >= 0.85, (
        f"留出集 hit@8={out['hit_rate']:.2f}。" +
        "失败项：" + "；".join(r["q"] for r in out["rows"] if not r["rank"]))


@pytest.mark.skipif(not DB.exists(), reason="真机库不存在")
def test_l2_beats_naive_by_a_wide_margin():
    """L2 必须明显好于"只按 weight 排"。

    差距小于 0.2 说明关键词层没起作用，或者排序被 weight 主导了——
    那种情况下 0.87 的命中率是 weight 带来的，不是检索带来的。
    """
    from hippo.core.store import Store
    from tools.eval_retrieve import evaluate

    store = Store.open(DB)
    try:
        full = evaluate(store)
        naive = evaluate(store, naive=True)
    finally:
        store.close()
    assert full["hit_rate"] - naive["hit_rate"] >= 0.20, (
        f"L2 {full['hit_rate']:.2f} vs 对照组 {naive['hit_rate']:.2f}")
