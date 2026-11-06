"""M1.3 验收：DAO 覆盖全表、注入防护、事务原子性、并发写入。"""

from __future__ import annotations

import pathlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

import hippo
from hippo.core.store import NoEvidenceError, Store, StoreError


# ------------------------------------------------------------------ events
def test_event_crud_and_kw_index(store):
    eid = store.add_event(
        session="s1", raw="我在杭州住了三年，住在西湖区。",
        summary="用户住在杭州西湖区，已三年。",
        keywords=["杭州", "居住", "西湖区"],
        event_time=1700000000, tier=1, w0=0.75, arousal=0.4,
    )
    ev = store.get_event(eid)
    assert ev is not None
    assert ev.keyword_list == ["杭州", "居住", "西湖区"]
    assert ev.tier == 1 and ev.weight == pytest.approx(0.75)
    assert ev.event_time == 1700000000

    tokens = [r["token"] for r in store.conn.execute(
        "SELECT token FROM kw_index WHERE event_id = ?", (eid,)
    )]
    assert set(tokens) == {"杭州", "居住", "西湖区"}

    assert store.delete_event(eid) is True
    assert store.get_event(eid) is None
    assert store.conn.execute(
        "SELECT count(*) FROM kw_index WHERE event_id = ?", (eid,)
    ).fetchone()[0] == 0


def test_empty_raw_rejected(store):
    with pytest.raises(StoreError, match="原文"):
        store.add_event(session="s1", raw="   ")


def test_get_events_keeps_order(store):
    ids = [store.add_event(session="s1", raw=f"内容{i}", keywords=[f"词{i}X"])
           for i in range(5)]
    shuffled = [ids[3], ids[0], ids[4]]
    assert [e.id for e in store.get_events(shuffled)] == shuffled


def test_search_ranks_by_hit_count(store):
    a = store.add_event(session="s1", raw="a", keywords=["老王", "跳槽", "字节"])
    b = store.add_event(session="s1", raw="b", keywords=["老王", "天气"])
    c = store.add_event(session="s1", raw="c", keywords=["完全", "无关"])
    hits = store.search_by_tokens("s1", ["老王", "跳槽"])
    assert [h[0].id for h in hits] == [a, b]
    assert hits[0][1] == 2 and hits[1][1] == 1
    assert c not in [h[0].id for h in hits]


def test_search_isolated_by_session(store):
    store.add_event(session="s1", raw="a", keywords=["共享词"])
    store.add_event(session="s2", raw="b", keywords=["共享词"])
    hits = store.search_by_tokens("s1", ["共享词"])
    assert len(hits) == 1 and hits[0][0].session == "s1"


def test_search_excludes_superseded_by_default(store):
    old = store.add_event(session="s1", raw="旧", keywords=["老王"])
    new = store.add_event(session="s1", raw="新", keywords=["老王"])
    store.conn.execute("UPDATE events SET superseded_by = ? WHERE id = ?", (new, old))
    visible = store.search_by_tokens("s1", ["老王"])
    assert [h[0].id for h in visible] == [new], "被取代的旧事件默认不该出现"
    assert len(store.search_by_tokens("s1", ["老王"], include_historical=True)) == 2


def test_touch_events(store):
    eid = store.add_event(session="s1", raw="a", keywords=["x1"])
    assert store.touch_events([eid]) == 1
    ev = store.get_event(eid)
    assert ev.recall_count == 1 and ev.last_access is not None
    store.touch_events([])
    assert store.get_event(eid).recall_count == 1


# ------------------------------------------------------------------ entities / traits
def test_entity_alias_merge(store):
    a = store.upsert_entity(session="s1", canonical="老王", aliases=["王哥"])
    b = store.upsert_entity(session="s1", canonical="老王", aliases=["隔壁老王"])
    assert a == b
    ent = store.get_entity(a)
    assert set(ent.aliases) == {"王哥", "隔壁老王"}


def test_trait_requires_evidence(store):
    eid = store.upsert_entity(session="s1", canonical="老王")
    with pytest.raises(NoEvidenceError):
        store.add_trait(entity_id=eid, trait="在字节做后端", evidence=[])


def test_trait_dedup_and_close(store):
    eid = store.upsert_entity(session="s1", canonical="老王")
    ev = store.add_event(session="s1", raw="r", keywords=["老王"])
    t1 = store.add_trait(entity_id=eid, trait="在字节做后端", evidence=[ev])
    t2 = store.add_trait(entity_id=eid, trait="在字节做后端", evidence=[ev + 1])
    assert t1 == t2
    traits = store.list_traits(eid)
    assert len(traits) == 1
    assert traits[0].confidence > 0.5

    assert store.close_trait(t1) is True
    assert store.list_traits(eid) == []
    assert len(store.list_traits(eid, active_only=False)) == 1


def test_trait_cascade_delete(store):
    eid = store.upsert_entity(session="s1", canonical="临时")
    ev = store.add_event(session="s1", raw="r", keywords=["临时"])
    store.add_trait(entity_id=eid, trait="x", evidence=[ev])
    store.conn.execute("DELETE FROM entities WHERE id = ?", (eid,))
    assert store.conn.execute(
        "SELECT count(*) FROM card_traits WHERE entity_id = ?", (eid,)
    ).fetchone()[0] == 0


# ------------------------------------------------------------------ facts
def test_fact_idempotent_when_same_value(store):
    """同值重复写只抬置信度，不新开一条。返回从 (id, bool) 换成 FactWrite。"""
    w1 = store.set_fact(session="s1", key="居住城市", value="上海")
    assert w1.action == "insert"
    w2 = store.set_fact(session="s1", key="居住城市", value="上海")
    assert w2.action == "same" and w1.id == w2.id
    assert len(store.get_fact("s1", "居住城市")) == 1


def test_fact_contradiction_keeps_history(store):
    store.set_fact(session="s1", key="居住城市", value="北京",
                   when=1_700_000_000, confidence=0.9)
    w = store.set_fact(session="s1", key="居住城市", value="上海",
                       when=1_710_000_000, confidence=0.9)
    assert w.action == "replace"
    active = store.get_fact("s1", "居住城市")
    assert [f.value for f in active] == ["上海"]
    history = store.get_fact("s1", "居住城市", include_historical=True)
    assert [f.value for f in history] == ["上海", "北京"]
    old = [f for f in history if f.value == "北京"][0]
    assert old.valid_to == 1_710_000_000   # 旧值关在新值生效的那一刻


# ------------------------------------------------------------------ doubts / plans / affect / ledger
def test_doubt_lifecycle(store):
    d = store.add_doubt(session="s1", question="「那边」指公司还是老家？", impact=0.8)
    active = store.active_doubts("s1")
    assert [x.id for x in active] == [d]
    store.mark_doubt_asked(d, turn=3)
    assert store.active_doubts("s1")[0].asked_count == 1
    assert store.resolve_doubt(d) is True
    assert store.active_doubts("s1") == []


def test_plan_due_and_fire_once(store):
    past = store.add_plan(session="s1", due_at=1, content="叫他起床")
    future = store.add_plan(session="s1", due_at=9_999_999_999, content="以后的事")
    due = store.due_plans(now=100)
    assert [p.id for p in due] == [past]
    assert store.mark_plan_fired(past) is True
    assert store.mark_plan_fired(past) is False, "同一条计划只能触发一次"
    assert store.due_plans(now=100) == []
    assert future > 0


def test_affect_defaults_and_update(store):
    a = store.upsert_affect("s1")
    assert a.joy == 60 and a.anger == 5 and a.updated > 0
    b = store.upsert_affect("s1", anger=80, joy=30)
    assert b.anger == 80 and b.joy == 30 and b.sadness == 10


def test_ledger_settlement(store):
    aid = store.add_ledger(session="s1", action="擅自改配置", predicted=0.5)
    assert [e.action_id for e in store.open_ledger("s1")] == [aid]
    rpe = store.settle_ledger(aid, actual=-1.0)
    assert rpe == pytest.approx(-1.5)
    assert store.open_ledger("s1") == []
    with pytest.raises(StoreError):
        store.settle_ledger("不存在的id", 1.0)


# ------------------------------------------------------------------ turns / metrics
def test_turn_index_is_sequential(store):
    for i in range(20):
        assert store.add_turn(session="s1", role="user", content=f"m{i}") == i
    turns = store.get_turns("s1", limit=100)
    assert [t.idx for t in turns] == list(range(20))
    assert store.get_turns("s1", limit=3)[-1].content == "m19"


def test_mark_turns_extracted(store):
    for i in range(4):
        store.add_turn(session="s1", role="user", content=str(i))
    assert store.mark_turns_extracted("s1", upto_idx=2) == 3
    assert store.mark_turns_extracted("s1", upto_idx=2) == 0


def test_metrics_and_token_sum(store):
    store.add_metric(ts=1000, prompt_tokens=100, completion_tokens=40,
                     cached_tokens=80, cost=0.01)
    store.add_metric(ts=2000, prompt_tokens=200, completion_tokens=60,
                     cached_tokens=150, cost=0.02)
    s = store.token_sum()
    assert s["prompt_tokens"] == 300 and s["calls"] == 2
    assert s["cached_tokens"] == 230
    assert store.token_sum(since=1500)["calls"] == 1


def test_stats(store):
    store.add_event(session="s1", raw="a", tier=1)
    store.add_event(session="s1", raw="b", tier=3)
    counts = store.counts()
    assert counts["events"] == 2 and counts["turns"] == 0
    assert store.tier_distribution() == {1: 1, 3: 1}


# ------------------------------------------------------------------ 约束与防护
def test_sql_injection_is_inert(store):
    evil = "x'); DROP TABLE events;--"
    store.add_turn(session=evil, role="user", content=evil)
    store.add_event(session=evil, raw=evil, keywords=[evil])
    assert store.conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()[0] == 1
    assert len(store.get_turns(evil)) == 1


def test_no_direct_db_access_outside_dao():
    """业务模块不许自己碰 SQL —— 保证 SQL 只有一个出口。"""
    root = pathlib.Path(hippo.__file__).parent
    allowed = {"store.py", "migrate.py", "db.py"}
    offenders = []
    for f in root.rglob("*.py"):
        if f.name in allowed:
            continue
        src = f.read_text(encoding="utf-8")
        if ".execute(" in src or ".executemany(" in src:
            offenders.append(str(f.relative_to(root)))
    assert offenders == [], f"这些文件绕过了 DAO: {offenders}"


def test_transaction_rolls_back_on_error(store):
    eid = store.add_event(session="s1", raw="保留我", keywords=["保留"])
    with pytest.raises(sqlite3.IntegrityError):
        with store.conn:
            store.conn.execute("UPDATE events SET raw = '' WHERE id = ?", (eid,))
    assert store.get_event(eid).raw == "保留我"


# ------------------------------------------------------------------ 并发
def test_concurrent_writes(db_path, conn):
    """20 线程各写 5 轮，验证 WAL + busy_timeout 下不丢、不错号。"""
    def work(i: int) -> None:
        s = Store.open(db_path)
        try:
            for j in range(5):
                s.add_turn(session="cc", role="user", content=f"{i}-{j}")
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(work, range(20)))

    s = Store.open(db_path)
    try:
        turns = s.get_turns("cc", limit=200)
        assert len(turns) == 100
        assert [t.idx for t in turns] == list(range(100)), "idx 不许重号"
    finally:
        s.close()
