"""M8.2 验收：矛盾检测 —— 检出、留痕、按时间点可查、低置信度不覆盖。

验收表里的"检出率 ≥95%"单独看没有约束力：把每条新事实都判成矛盾，
检出率就是 100%。所以这里同时钉三件事：

1. 该报的必须报（`test_corpus_recall`）
2. 不该报的一条都不许报（同一条用例里的误报断言）
3. **旧值必须还能查**（`test_history_queryable_at_time`）——
   只把 valid_to 写上、查询却不支持时间点，等于"留了痕但看不见"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hippo.core.migrate import migrate
from hippo.core.store import Store
from tools.eval_contradiction import MUST_DETECT, MUST_NOT, load_cases, run_one

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "contradictions.jsonl"


def test_corpus_exists_and_balanced():
    """两头都要有量。全是正例的语料只能测出"永远返回 true"。"""
    cases = load_cases()
    pos = [c for c in cases if c["kind"] in MUST_DETECT]
    neg = [c for c in cases if c["kind"] in MUST_NOT]
    assert len(pos) >= 15, f"矛盾对只有 {len(pos)} 组，手册要求 15"
    assert len(neg) >= 10, f"不该报矛盾的用例只有 {len(neg)} 组"


def test_corpus_recall():
    """15 组矛盾对，检出率 ≥95%（手册 DoD）。"""
    rows = [run_one(c) for c in load_cases() if c["kind"] in MUST_DETECT]
    hits = [r for r in rows if r["action"] == "replace" and r["n_closed"] >= 1]
    recall = len(hits) / len(rows)
    assert recall >= 0.95, f"检出率 {recall:.1%}，漏报：" + ", ".join(
        f"{r['id']}({r['old']}→{r['new']} 变 {r['action']})" for r in rows
        if r["action"] != "replace")


def test_no_false_positive_on_variants():
    """同值异形、粒度差异、多值槽——一条都不许判成矛盾。"""
    rows = [run_one(c) for c in load_cases() if c["kind"] in MUST_NOT]
    bad = [r for r in rows if r["action"] == "replace"]
    assert not bad, "误报（这些都不是搬家）：" + "; ".join(
        f"{r['id']} {r['old']}→{r['new']}" for r in bad)


def test_low_confidence_asks_instead_of_overwriting():
    rows = [run_one(c) for c in load_cases() if c["kind"] == "low_conf"]
    assert rows, "语料里没有低置信度用例"
    for r in rows:
        assert r["action"] == "doubt", f"{r['id']} 应写 doubt，实际 {r['action']}"
        assert r["n_closed"] == 0, f"{r['id']} 不许关闭旧值"


def test_history_queryable_at_time(store):
    """"搬家之前住哪"必须能答。留痕不算数，能查才算。"""
    store.set_fact(session="s", key="city", value="北京", when=1000, confidence=0.9)
    store.set_fact(session="s", key="city", value="上海", when=2000, confidence=0.9)

    assert store.fact_as_of("s", "city", 1500).value == "北京"
    assert store.fact_as_of("s", "city", 2000).value == "上海"   # 左闭右开
    assert store.fact_as_of("s", "city", 500) is None
    assert [f.value for f in store.get_fact("s", "city")] == ["上海"]


def test_multiple_moves_keep_every_segment(store):
    """搬三次家，三段历史都要在，且区间首尾相接。"""
    for i, city in enumerate(("北京", "上海", "杭州")):
        store.set_fact(session="s", key="city", value=city,
                       when=(i + 1) * 1000, confidence=0.9)
    hist = sorted(store.get_fact("s", "city", include_historical=True),
                  key=lambda f: f.valid_from)
    assert [(f.value, f.valid_from, f.valid_to) for f in hist] == [
        ("北京", 1000, 2000), ("上海", 2000, 3000), ("杭州", 3000, None),
    ]
    for t, want in ((1500, "北京"), (2500, "上海"), (3500, "杭州")):
        assert store.fact_as_of("s", "city", t).value == want


def test_no_duplicate_active_after_many_writes(store):
    """同槽写 20 次，active 永远只有一条。"""
    for i in range(20):
        store.set_fact(session="s", key="employer", value=f"公司{i}",
                       when=1000 + i, confidence=0.9)
    active = store.get_fact("s", "employer")
    assert len(active) == 1
    assert active[0].value == "公司19"


def test_contradiction_records_source_event(store):
    """矛盾要有出处：哪一轮说了新值，日后才追得回去。"""
    e1 = store.add_event(session="s", raw="我住北京", keywords=["北京"], summary="住北京")
    e2 = store.add_event(session="s", raw="我搬上海了", keywords=["上海"], summary="搬家")
    store.set_fact(session="s", key="city", value="北京", source_event=e1, confidence=0.9)
    store.set_fact(session="s", key="city", value="上海", source_event=e2, confidence=0.9)
    assert store.get_fact("s", "city")[0].source_event == e2


def test_eval_tool_writes_report(tmp_path, monkeypatch):
    """评测要落盘：数字只在终端里滚过去，事后无法复核。"""
    import tools.eval_contradiction as mod

    out = tmp_path / "c.json"
    monkeypatch.setattr(mod, "OUT", out)
    monkeypatch.setattr("sys.argv", ["eval_contradiction"])
    assert mod.main() == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["recall"] >= 0.95 and data["false_positive"] == 0


def test_backdated_new_value_keeps_old_queryable(store):
    """新值带"上个月"、旧值没有时间线索时，旧值的区间不能变成负数。

    真机上出现过：北京 [11-05, 10-01)，结束早于开始。
    这种行任何按时间点的查询都查不到，等于"留了痕但谁也看不见"。
    正确语义是：结束时间知道（新值生效那一刻），开始时间不知道 → 用 0 表示。
    """
    store.set_fact(session="s", key="city", value="北京", when=5000, confidence=0.9)
    store.set_fact(session="s", key="city", value="上海", when=1000, confidence=0.9)

    old = [f for f in store.get_fact("s", "city", include_historical=True)
           if f.value == "北京"][0]
    assert old.valid_from == 0 and old.valid_to == 1000
    assert store.fact_as_of("s", "city", 500).value == "北京"
    assert store.fact_as_of("s", "city", 1500).value == "上海"


def test_no_interval_has_end_before_start(store):
    """全域不变量：任何一条历史事实都不许出现 valid_to < valid_from。"""
    seq = [("北京", 5000), ("上海", 1000), ("杭州", 9000), ("苏州", 3000)]
    for city, t in seq:
        store.set_fact(session="s", key="city", value=city, when=t, confidence=0.9)
    for f in store.get_fact("s", "city", include_historical=True):
        if f.valid_to is not None:
            assert f.valid_from <= f.valid_to, f"{f.value}: {f.valid_from} > {f.valid_to}"
