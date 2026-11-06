"""M5.3 验收：结果入库。

重点验三件事：
  1. 行数与 JSON 条目数一致（不多不少）
  2. 中途异常**全回滚**（不许留半截数据）
  3. evidence 下标映射保留空洞（错挂是静默故障，最容易埋雷）
"""

from __future__ import annotations

import time

import pytest

from hippo.core.persist import SELF, persist_result
from hippo.core.schema import ExtractResult, parse_result
from hippo.core.turns import slice_windows


def make_window(store, texts, *, session="s", ts=1_760_000_000):
    items = []
    for t in texts:
        items.append(("user", t, 0))
        items.append(("assistant", "嗯。", 0))
    store.add_turns(session, items, ts=ts)
    return slice_windows(store.pending_turns(session), session=session,
                         per_window=5)[0]


def R(**kw) -> ExtractResult:
    return parse_result(__import__("json").dumps(kw, ensure_ascii=False))


# ---------------------------------------------------------------- 基本写入
def test_persist_full(store):
    w = make_window(store, ["我昨天下午搬家了", "老王说他下周要去深圳出差"])
    r = R(
        events=[
            {"raw": "我昨天下午搬家了", "summary": "用户搬家",
             "keywords": ["搬家", "下午"], "entities": [{"name": "用户"}],
             "event_time_hint": "昨天下午", "arousal": 0.3},
            {"raw": "老王说他下周要去深圳出差", "summary": "老王下周出差深圳",
             "keywords": ["老王", "深圳", "出差"],
             "entities": [{"name": "老王"}], "arousal": 0.1},
        ],
        # key 必须在 data/fact_keys.yaml 里。"最近搬家"是 event 不是属性，
        # M8 起会被拒绝——这里改用 city，同时保留"事实能写进去"这个验收点。
        facts=[{"entity": "用户", "key": "city", "value": "深圳", "confidence": 0.9,
                "evidence": [1]}],
        traits=[{"entity": "老王", "trait": "经常出差", "status": "inferred",
                 "evidence": [1]}],
        plans=[{"content": "提醒用户收快递", "due_hint": "明天8点"}],
        doubts=[{"question": "用户搬到哪个城市了？", "confidence": 0.3, "impact": 0.6}],
        affect_delta={"joy": 5},
    )
    st = persist_result(store, "s", w, r)
    c = store.counts()
    assert len(st.event_ids) == 2
    assert c["events"] == 2 and c["facts"] == 1 and c["card_traits"] == 1
    assert c["plans"] == 1 and c["doubts"] == 1
    assert st.verbatim_fail == 0


def test_raw_is_verbatim(store):
    """入库的 raw 必须是原文切片，不是模型给的那份。"""
    w = make_window(store, ["我昨天下午搬家了，从朝阳搬到海淀"])
    r = R(events=[{"raw": "我昨天下午搬家了", "summary": "x"}])
    st = persist_result(store, "s", w, r)
    ev = store.get_event(st.event_ids[0])
    assert ev.raw == "我昨天下午搬家了"
    assert ev.raw in "我昨天下午搬家了，从朝阳搬到海淀"


def test_kw_index_synced(store):
    w = make_window(store, ["我在字节做后端，天天加班"])
    r = R(events=[{"raw": "我在字节做后端，天天加班",
                   "keywords": ["字节", "后端", "加班"]}])
    st = persist_result(store, "s", w, r)
    hits = store.search_by_tokens("s", ["字节"])
    assert [e.id for e, _ in hits] == st.event_ids
    assert store.conn.execute(
        "SELECT count(*) FROM kw_index WHERE event_id = ?", (st.event_ids[0],)
    ).fetchone()[0] == 3


def test_event_entities_synced(store):
    w = make_window(store, ["老王和小李一起吃饭了"])
    r = R(events=[{"raw": "老王和小李一起吃饭了",
                   "entities": [{"name": "老王"}, {"name": "小李"}]}])
    st = persist_result(store, "s", w, r)
    ents = store.entities_of_event(st.event_ids[0])
    assert len(ents) == 2
    for eid in ents:
        assert store.events_of_entity("s", eid) == st.event_ids


# ---------------------------------------------------------------- 锚定失败
def test_polished_raw_rejected(store):
    w = make_window(store, ["我实在受不了了，这破公司天天加班"])
    r = R(events=[{"raw": "我无法忍受这家公司长期加班", "summary": "x"}])
    st = persist_result(store, "s", w, r)
    assert st.verbatim_fail == 1
    assert st.event_ids == []
    assert store.counts()["events"] == 0


def test_partial_reject_keeps_others(store):
    w = make_window(store, ["我养了只猫叫豆豆"])
    r = R(events=[
        {"raw": "我养了只猫", "summary": "养猫"},          # 锚得上
        {"raw": "猫的名字是豆豆（音）", "summary": "编的"},  # 锚不上
    ])
    st = persist_result(store, "s", w, r)
    assert len(st.event_ids) == 1 and st.verbatim_fail == 1


def test_evidence_index_keeps_holes(store):
    """第 0 条被丢弃时，evidence=[1] 必须挂到**第 2 条**，不能压缩到第 1 条。

    这是最容易写错的一行：`zip(kept, event_ids)` 看起来天经地义，
    但它把"原下标"抹掉了，于是 evidence=[1] 会挂到原第 0 条的位置。
    """
    w = make_window(store, ["第一句真的话", "第二句真的话"])
    r = R(
        events=[
            {"raw": "第一句假的话", "summary": "锚不上"},     # 下标 0，丢弃
            {"raw": "第二句真的话", "summary": "留下"},       # 下标 1，入库
        ],
        traits=[{"entity": "用户", "trait": "爱说话", "evidence": [1]}],
    )
    st = persist_result(store, "s", w, r)
    assert len(st.event_ids) == 1
    tr = store.list_traits(store.entity_id_of("s", SELF))[0]
    assert tr.evidence == [st.event_ids[0]], "下标映射错位了"


def test_evidence_out_of_range_dropped(store):
    w = make_window(store, ["我说了句话"])
    r = R(events=[{"raw": "我说了句话"}],
          traits=[{"entity": "用户", "trait": "话多", "evidence": [9]}])
    st = persist_result(store, "s", w, r)
    assert st.traits == 0 and st.traits_dropped == 1


def test_trait_without_evidence_dropped(store):
    """没证据的推断不许落库——它会变成三个月后 AI 嘴里的"你就是这样的人"。"""
    w = make_window(store, ["我说了句话"])
    r = R(events=[{"raw": "我说了句话"}],
          traits=[{"entity": "用户", "trait": "内向", "status": "inferred"}])
    st = persist_result(store, "s", w, r)
    assert st.traits == 0 and st.traits_dropped == 1


# ---------------------------------------------------------------- plans
def test_plan_parsed(store):
    w = make_window(store, ["明天8点叫我起床"])
    r = R(events=[{"raw": "明天8点叫我起床"}],
          plans=[{"content": "叫用户起床", "due_hint": "明天8点"}])
    st = persist_result(store, "s", w, r)
    assert st.plans == 1
    p = store.due_plans(now=2 ** 31)[0]
    assert p.content == "叫用户起床"
    assert p.raw_hint == "明天8点"


def test_plan_without_time_dropped(store):
    """due_at 是 NOT NULL。解析不出来就不能入库，宁可少记也不要瞎定闹钟。"""
    w = make_window(store, ["有空一起吃饭"])
    r = R(events=[{"raw": "有空一起吃饭"}],
          plans=[{"content": "一起吃饭", "due_hint": "有空的时候"}])
    st = persist_result(store, "s", w, r)
    assert st.plans == 0 and st.plans_dropped == 1


# ---------------------------------------------------------------- affect
def test_affect_delta_applied(store):
    """M20.2 起走 apply_delta：joy↑ 会连带压制 sadness（交叉抑制 -0.3）。

    sadness = 10 + (-5) + (-0.3 × 15) = 0.5——不是旧语义的 5。
    """
    w = make_window(store, ["今天太开心了"])
    r = R(events=[{"raw": "今天太开心了", "arousal": 0.8}],
          affect_delta={"joy": 15, "sadness": -5})
    persist_result(store, "s", w, r)
    a = store.get_affect("s")
    assert a.joy == 75 and a.sadness == 0.5


def test_affect_clamped(store):
    w = make_window(store, ["太开心了"])
    for _ in range(5):
        persist_result(store, "s", w, R(events=[{"raw": "太开心了"}],
                                        affect_delta={"joy": 20}))
    a = store.get_affect("s")
    assert a.joy == 100, "情绪必须夹在 0~100，否则渲染时会溢出"


def test_affect_zero_delta_no_write(store):
    w = make_window(store, ["今天周三"])
    persist_result(store, "s", w, R(events=[{"raw": "今天周三"}]))
    assert store.get_affect("s") is None, "全 0 的 delta 不该建 affect 行"


# ---------------------------------------------------------------- 事务
def test_rollback_on_midway_error(store, monkeypatch):
    """plans 写崩 → events 也不能留。半截数据比没数据更难查。"""
    w = make_window(store, ["明天8点叫我起床"])
    r = R(events=[{"raw": "明天8点叫我起床"}],
          facts=[{"entity": "用户", "key": "k", "value": "v"}],
          plans=[{"content": "叫用户起床", "due_hint": "明天8点"}])

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "add_plan", boom)
    with pytest.raises(RuntimeError):
        persist_result(store, "s", w, r)

    c = store.counts()
    assert c["events"] == 0 and c["facts"] == 0 and c["entities"] == 0
    assert c["kw_index"] == 0


def test_transaction_is_fast(store):
    """单窗口 5 条事件，写入 < 20ms（不含锚定）。"""
    w = make_window(store, [f"第{i}句话在这里" for i in range(5)])
    events = [{"raw": f"第{i}句话在这里", "keywords": [f"词{i}", "事情"]}
              for i in range(5)]
    r = R(events=events,
          facts=[{"entity": "用户", "key": f"k{i}", "value": "v"} for i in range(5)],
          doubts=[{"question": f"用户的某个习惯是什么样子的{i}",
                   "impact": 0.6} for i in range(3)])
    t0 = time.perf_counter()
    persist_result(store, "s", w, r)
    ms = (time.perf_counter() - t0) * 1000
    assert ms < 20, f"{ms:.1f}ms"


# ---------------------------------------------------------------- 实体去重
def test_entity_not_duplicated(store):
    w = make_window(store, ["老王来了"])
    r = R(events=[{"raw": "老王来了", "entities": [{"name": "老王"}]}])
    persist_result(store, "s", w, r)
    st2 = persist_result(store, "s", w, r)
    assert st2.entities_new == 0 and st2.entities_hit == 1
    assert len(store.list_entities("s")) == 1


def test_alias_merged(store):
    w = make_window(store, ["王哥，就是老王，他来了"])
    r = R(events=[{"raw": "王哥，就是老王，他来了",
                   "entities": [{"name": "老王", "aliases": ["王哥"]}]}])
    persist_result(store, "s", w, r)
    e = store.list_entities("s")[0]
    assert e.canonical == "老王" and e.aliases == ["王哥"]


def test_self_entity_created_only_when_needed(store):
    w = make_window(store, ["今天周三"])
    persist_result(store, "s", w, R(events=[{"raw": "今天周三"}]))
    assert store.list_entities("s") == []


# ---------------------------------------------------------------- 时间
def test_event_time_parsed(store):
    w = make_window(store, ["昨天下午搬家了"], ts=1_760_000_000)
    r = R(events=[{"raw": "昨天下午搬家了", "event_time_hint": "昨天下午"}])
    st = persist_result(store, "s", w, r)
    ev = store.get_event(st.event_ids[0])
    assert ev.event_time < 1_760_000_000
    assert ev.time_precision in ("hour", "minute", "day")


def test_event_time_unparsed_falls_back(store):
    w = make_window(store, ["很久以前的事了"], ts=1_760_000_000)
    r = R(events=[{"raw": "很久以前的事了", "event_time_hint": "很久以前"}])
    st = persist_result(store, "s", w, r)
    ev = store.get_event(st.event_ids[0])
    assert ev.event_time == 1_760_000_000
    assert ev.time_guessed == 1


def test_high_arousal_goes_hot(store):
    w = make_window(store, ["我们吵架了，很凶"])
    r = R(events=[{"raw": "我们吵架了，很凶", "arousal": 0.9}])
    st = persist_result(store, "s", w, r)
    assert store.get_event(st.event_ids[0]).tier == 1
