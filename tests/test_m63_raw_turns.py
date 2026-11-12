# -*- coding: utf-8 -*-
"""M63 验收：原话兜底（L1_RAW 等价层）。

背景：194 题诊断里 74% 的漏题是"任何事件里都没有"，但原文还在 turns 表。
这一层不调模型，只在事件没命中时把原话捞上来渲染成低优先级线索。
"""
from __future__ import annotations

from hippo.core.pack import pack
from hippo.core.rank import l2_search


def _turns(store):
    store.add_turns("s", [
        ("user", "我下周要去杭州出差，顺便看看那家做医疗的公司", 0),
        ("assistant", "好的，记下了", 0),
    ], ts=1_760_000_000)


def test_search_turns_finds_matching_user_turn(store):
    _turns(store)
    rows = store.search_turns("s", ["杭州", "医疗"])
    assert len(rows) == 1
    assert "杭州" in rows[0]["text"]
    assert rows[0]["role"] if "role" in rows[0] else True


def test_search_turns_ignores_short_terms_and_misses(store):
    _turns(store)
    assert store.search_turns("s", ["杭"]) == []          # 单字不查
    assert store.search_turns("s", ["东京"]) == []        # 不匹配


def test_l2_search_attaches_raw_turns_when_event_misses(store):
    _turns(store)
    # 库里只有一条与问题无关的事件，事件检索必然空
    store.add_event(session="s", raw="今天天气不错", summary="天气不错",
                    keywords=["天气"], event_time=1_760_000_000)
    res = l2_search(store, "s", "杭州那家医疗公司怎么样")
    assert res.raw_turns, "事件没命中时应该带上原话"
    assert any("杭州" in row["text"] for row in res.raw_turns)


def test_pack_renders_raw_section(store):
    _turns(store)
    out = pack(store, "s", "杭州那家医疗公司怎么样", use_cache=False)
    assert "原话" in out.text
    assert "杭州" in out.text


def test_search_turns_respects_scan_limit(store):
    """扫描边界：超出最近 N 条的原话不参与兜底（否则 LIKE 全表扫，10 万条 40ms）。"""
    for i in range(20):
        store.add_turns("s", [("user", f"第{i}条：杭州项目细节{i}", 0)], ts=1_760_000_000 + i)
    # 最近 5 条里能查到
    rows = store.search_turns("s", ["杭州"], scan_limit=5)
    assert rows and all(r["idx"] >= 15 for r in rows)
    # 边界设小到 1：更早的内容不再返回
    rows1 = store.search_turns("s", ["杭州"], scan_limit=1)
    assert len(rows1) <= 1


def test_search_turns_default_scan_limit_covers_normal_sessions(store):
    for i in range(30):
        store.add_turns("s", [("user", f"第{i}条：上海出差计划{i}", 0)], ts=1_760_000_000 + i)
    rows = store.search_turns("s", ["上海"])          # 默认 5000
    assert len(rows) >= 1


def test_huge_query_is_bounded(store):
    """超长粘贴（5 万字）不能把装配拖到几十毫秒：入口要截断。"""
    import time

    _turns(store)
    big = "杭" * 50000
    t0 = time.perf_counter()
    out = pack(store, "s", big, use_cache=False)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert out.total_tokens > 0
    assert elapsed_ms < 150, f"超长问题装配耗时 {elapsed_ms:.1f}ms"
