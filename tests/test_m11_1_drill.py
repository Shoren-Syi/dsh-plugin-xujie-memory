"""M11.1 验收：原文下钻的三道闸门。

这一节值得测的不是"能不能拉到原文"，是**不该拉的时候一条都没拉**：

    触发率 ≤25%      否则原话层就是每轮多 300 token 的固定开销
    预算不足不下钻    拉了装不下 = 白花钱，且把别的记忆挤掉
    top-2 之后不下钻  用户问的已经不是第 3 条了

预算类的断言一律用"≤"，不是"=="：装填是贪心的，装不满才是常态，
装满了说明预算算错了。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from hippo.core.drill import (
    DEPTH_RAW, DEPTH_SUMMARY, RAW_MAX_TOKENS, TOP_N, drill, wants_detail,
)
from hippo.core.models import Event
from hippo.core.rank import L2Result, MemoryItem, l2_search

FIX = Path(__file__).parent / "fixtures" / "drill_queries.jsonl"
NOW = datetime(2026, 11, 5, 12, 0, 0)


def _ts(y, m, d, h=12):
    return int(datetime(y, m, d, h).timestamp())


def _ev(store, raw, when, *, keywords="", summary="", pinned=0, session="s1"):
    return store.add_event(
        session=session, raw=raw, event_time=when, keywords=keywords,
        summary=summary or raw, pinned=pinned)


def _seed(store):
    """三条：一条搬家（含"怎么说的"能命中），一条猫，一条报告。"""
    _ev(store, "我昨天下午搬家了，从朝阳搬到海淀，房租涨了两千，累死了",
        _ts(2026, 11, 4), keywords="搬家 朝阳 海淀 房租 涨",
        summary="用户从朝阳搬到海淀，房租涨两千")
    _ev(store, "老王说他下周要去深圳出差，让我帮他喂猫",
        _ts(2026, 11, 3), keywords="老王 深圳 出差 喂猫",
        summary="老王下周去深圳出差，托用户喂猫")
    _ev(store, "我下周三要交季度报告，还没开始写",
        _ts(2026, 11, 2), keywords="季度报告 下周三 没写",
        summary="用户下周三交季度报告，还没开始")


# ------------------------------------------------------------------ 判据
def test_wants_detail_splits_question_from_statement():
    """问细节 vs 陈述。这是本节的判据核心。"""
    assert wants_detail("我当时怎么说的来着？")
    assert wants_detail("把我说过的原话给我")
    assert wants_detail("具体是哪天搬的")
    # 陈述句里带同样的词，不该触发
    assert not wants_detail("当时我不在家")
    assert not wants_detail("具体地址我忘了")
    assert not wants_detail("")


def test_fixture_rate_precision_recall():
    """100 条真实口吻的句子：触发率、误触发率、漏检率。

    这三个数是**成本的唯一约束**。只看"能不能触发"会做出一个每轮都拉的实现。
    """
    rows = [json.loads(line) for line in FIX.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert len(rows) == 100

    tp = fp = fn = 0
    for r in rows:
        got, want = wants_detail(r["q"]), bool(r["detail"])
        if got and want:
            tp += 1
        elif got:
            fp += 1
        elif want:
            fn += 1

    rate = (tp + fp) / len(rows)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0

    assert rate <= 0.25, f"触发率 {rate:.0%} 超过 25%"
    assert precision >= 0.85, f"误触发太多，精确率只有 {precision:.2f}"
    assert recall >= 0.90, f"漏检太多，召回只有 {recall:.2f}"


# ------------------------------------------------------------------ 闸门
def test_no_detail_no_drill(store):
    _seed(store)
    res = l2_search(store, "s1", "我搬到哪了")
    out = drill(store, res, text="我搬到哪了")
    assert out.drilled == []
    assert out.fired is False
    assert {r for _, r in out.skipped} == {"not_detail"}
    assert all(it.raw_text == "" and it.depth == DEPTH_SUMMARY for it in res.items)


def test_detail_drills_with_raw(store):
    _seed(store)
    res = l2_search(store, "s1", "我搬家的事当时怎么说的")
    out = drill(store, res, text="我搬家的事当时怎么说的")
    assert out.fired
    top = out.drilled[0]
    assert "搬家" in top.raw_text
    assert top.depth == DEPTH_RAW
    assert top.raw_tokens > 0
    assert out.used == sum(i.raw_tokens for i in out.drilled)


def test_budget_below_min_never_drills(store):
    """剩不到 200 token 就不拉。拉了也是残句。"""
    _seed(store)
    res = l2_search(store, "s1", "我搬家的事当时怎么说的")
    out = drill(store, res, text="我搬家的事当时怎么说的", budget=199)
    assert out.drilled == []
    assert "budget" in {r for _, r in out.skipped}
    assert out.used == 0


def test_zero_budget_is_not_negative(store):
    """L2 被 pinned 撑爆时，预算可能算成负数——不能变成"倒欠"。"""
    _seed(store)
    res = l2_search(store, "s1", "我搬家的事当时怎么说的")
    res.total_tokens = 10_000        # 假装 L2 已经用超
    out = drill(store, res, text="我搬家的事当时怎么说的")
    assert out.budget == 0
    assert out.drilled == []


def test_never_overspends(store):
    """预算 250，两条都想下钻，总量不能超 250。"""
    for i in range(3):
        _ev(store, f"搬家那天的事我讲了很久，第{i}条，" + "细节" * 60,
            _ts(2026, 11, 4), keywords="搬家 细节")
    res = l2_search(store, "s1", "搬家当时怎么说的")
    out = drill(store, res, text="搬家当时怎么说的", budget=250)
    assert out.used <= 250
    assert sum(i.raw_tokens for i in out.drilled) <= 250


def test_only_top_two(store):
    """第 3 条之后永不下钻，哪怕预算充足、max_raws 很大。"""
    for i in range(6):
        _ev(store, f"搬家第{i}条记录", _ts(2026, 11, 4) - i,
            keywords="搬家 记录", summary=f"搬家第{i}条")
    res = l2_search(store, "s1", "搬家当时怎么说的")
    assert len(res.items) >= 4
    out = drill(store, res, text="搬家当时怎么说的", budget=5000)
    drilled_ids = {i.id for i in out.drilled}
    tail_ids = {i.id for i in res.items[TOP_N:]}
    assert not (drilled_ids & tail_ids), "top-2 之外的事件被下钻了"


def test_max_raws_caps_count(store):
    for i in range(4):
        _ev(store, f"搬家第{i}条记录", _ts(2026, 11, 4) - i,
            keywords="搬家 记录", summary=f"搬家第{i}条")
    from hippo.config import Config
    cfg = Config()
    cfg.retrieve.max_raws = 1
    store.attach_config(cfg)
    res = l2_search(store, "s1", "搬家当时怎么说的")
    out = drill(store, res, text="搬家当时怎么说的", budget=5000)
    assert len(out.drilled) == 1
    assert any(r == "max_raws" for _, r in out.skipped)


def test_pinned_drills_without_detail(store):
    """钉住 = 用户说"这个必须记得"，不看这句话问没问细节。"""
    _ev(store, "我妈住院了，在协和，下周一手术", _ts(2026, 11, 1),
        keywords="妈 住院 协和 手术", pinned=1, summary="用户母亲住院，下周一手术")
    res = l2_search(store, "s1", "我妈最近怎么样")
    assert any(i.pinned for i in res.items)
    out = drill(store, res, text="我妈最近怎么样")
    assert out.fired
    assert out.drilled[0].pinned


def test_long_raw_truncated_to_cap(store):
    _ev(store, "搬家那天" + "很累" * 400, _ts(2026, 11, 4), keywords="搬家")
    res = l2_search(store, "s1", "搬家当时怎么说的")
    out = drill(store, res, text="搬家当时怎么说的", budget=5000)
    it = out.drilled[0]
    assert it.raw_tokens <= RAW_MAX_TOKENS
    assert it.raw_text.endswith("…")


def test_empty_raw_is_skipped(store):
    """raw 是 NOT NULL 且不可变，空串只可能来自迁移前的老库/外部写入。

    这里直接构造一条 MemoryItem 测边界：drill 不许因为一条脏数据抛异常，
    也不许把空原文当成"下钻成功"占掉 max_raws 的名额。
    """
    e = Event(id=1, session="s1", event_time=_ts(2026, 11, 4),
              ingest_time=_ts(2026, 11, 4), raw="", summary="搬家",
              keywords="搬家")
    it = MemoryItem(event=e, score=1.0, kw_hit=1, text="搬家", tokens=2)
    res = L2Result(items=[it], candidates=1, matched=1,
                   total_tokens=2, elapsed_ms=0.0)
    out = drill(store, res, text="搬家当时怎么说的")
    assert out.drilled == []
    assert "no_raw" in {r for _, r in out.skipped}


def test_second_drill_does_not_double_count(store):
    """重复下钻不叠加 raw_tokens（幂等）。M13 可能重排后重跑一次。"""
    _seed(store)
    res = l2_search(store, "s1", "我搬家的事当时怎么说的")
    a = drill(store, res, text="我搬家的事当时怎么说的")
    first = res.items[0].raw_tokens
    b = drill(store, res, text="我搬家的事当时怎么说的")
    assert a.used == b.used
    assert res.items[0].raw_tokens == first


def test_degraded_result_never_drills(store):
    """关键词一个都没命中时，items 是按 weight 兜底的——给它们贴"原话"
    比不给更糟：模型会把无关的原文当答案。

    这一条是施工中发现的：`recall "我当时怎么说的来着" --raw` 在真机库上
    命中 0 条，却把"老板骂人"那两段原文当成了"我"说的话。
    """
    _seed(store)
    res = l2_search(store, "s1", "量子纠缠的退相干时间")
    assert res.degraded == "no_keyword_hit"
    out = drill(store, res, text="量子纠缠当时怎么说的")
    assert out.fired is False
    assert "degraded" in {r for _, r in out.skipped}
