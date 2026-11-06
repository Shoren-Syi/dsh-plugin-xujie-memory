"""M5.0 验收：event_entities 关联表。

这一组测试的存在理由，是 001 那个 `LIKE '%id%'` 的 bug 藏了整整四个里程碑。
现在的规矩：**凡是"查询条件里带 id 的地方"，都要有一条 id>10 的断言。**
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from hippo.core.db import connect
from hippo.core.migrate import migrate


def _eid(store, name: str, aliases=None) -> int:
    return store.upsert_entity(session="s", canonical=name, aliases=aliases or [])


# ---------------------------------------------------------------- 核心回归
def test_entity_id_is_not_prefix_matched(store):
    """实体 1 不能命中挂在 11 / 21 上的事件。这是 002 存在的全部理由。

    ★ 必须真的把 id 顶到 11/21：如果测试里实体全是 id<10，
      `LIKE '%1%'` 和 `= 1` 的行为完全一样，这条测试就是装饰品。
    """
    e1 = _eid(store, "老王")                     # 1
    for i in range(9):
        _eid(store, f"填充甲{i}")                # 2..10
    e11 = _eid(store, "小李")                    # 11
    for i in range(9):
        _eid(store, f"填充乙{i}")                # 12..20
    e21 = _eid(store, "张三")                    # 21
    assert (e1, e11, e21) == (1, 11, 21), "id 没顶到位，这条测试测不到 bug"

    a = store.add_event(session="s", raw="老王说他下周去上海", entities=[e1])
    b = store.add_event(session="s", raw="小李换了工作", entities=[e11])
    c = store.add_event(session="s", raw="张三结婚了", entities=[e21])

    assert store.events_of_entity("s", e1) == [a]
    assert store.events_of_entity("s", e11) == [b]
    assert store.events_of_entity("s", e21) == [c]


def test_multi_entity_event_appears_for_each(store):
    a = _eid(store, "老王")
    b = _eid(store, "小李")
    ev = store.add_event(session="s", raw="老王和小李一起吃饭", entities=[a, b])
    assert store.events_of_entity("s", a) == [ev]
    assert store.events_of_entity("s", b) == [ev]
    assert store.entities_of_event(ev) == sorted([a, b])


def test_session_scoped(store):
    """同名实体在不同 session 是两行，事件不能串。"""
    a = _eid(store, "老王")
    store.add_event(session="s1", raw="老王来了", entities=[a])
    assert store.events_of_entity("s2", a) == []


def test_archived_and_superseded_excluded(store):
    a = _eid(store, "老王")
    e1 = store.add_event(session="s", raw="老王昨天来过", entities=[a])
    e2 = store.add_event(session="s", raw="老王今天来过", entities=[a])
    store.conn.execute("UPDATE events SET archived = 1 WHERE id = ?", (e1,))
    store.conn.execute("UPDATE events SET superseded_by = ? WHERE id = ?", (e2, e2))
    assert store.events_of_entity("s", a) == []


def test_ordering_by_weight(store):
    a = _eid(store, "老王")
    low = store.add_event(session="s", raw="老王随口提了一句", entities=[a], weight=0.2)
    high = store.add_event(session="s", raw="老王住院了", entities=[a], weight=0.9)
    assert store.events_of_entity("s", a) == [high, low]


# ---------------------------------------------------------------- 触发器
def test_trigger_covers_direct_sql_insert(conn):
    """绕过 DAO 直接 INSERT 也要建关联——迁移脚本和归档导入都走这条路。"""
    eid = conn.execute(
        "INSERT INTO entities(session, canonical) VALUES ('s','老王')"
    ).lastrowid
    ev = conn.execute(
        "INSERT INTO events(session, event_time, ingest_time, raw, entities)"
        " VALUES ('s',1,1,'老王来了',?)",
        (f"[{eid}]",),
    ).lastrowid
    rows = conn.execute(
        "SELECT entity_id FROM event_entities WHERE event_id = ?", (ev,)
    ).fetchall()
    assert [r[0] for r in rows] == [eid]


def test_trigger_syncs_on_update(store):
    a = _eid(store, "老王")
    b = _eid(store, "小李")
    ev = store.add_event(session="s", raw="老王来了", entities=[a])
    store.conn.execute("UPDATE events SET entities = ? WHERE id = ?", (f"[{b}]", ev))
    assert store.events_of_entity("s", a) == []
    assert store.events_of_entity("s", b) == [ev]


def test_dirty_entities_json_ignored(conn):
    """entities 是 TEXT，可能被手工写脏。脏值不能把迁移/触发器搞崩。"""
    # 建一个实体，证明"库里有实体"不会让脏 JSON 被误关联
    conn.execute("INSERT INTO entities(session, canonical) VALUES ('s','老王')")
    ev = conn.execute(
        "INSERT INTO events(session, event_time, ingest_time, raw, entities)"
        " VALUES ('s',1,1,'x','not-json')",
    ).lastrowid
    assert conn.execute(
        "SELECT count(*) FROM event_entities WHERE event_id = ?", (ev,)
    ).fetchone()[0] == 0


def test_cascade_delete(store):
    a = _eid(store, "老王")
    ev = store.add_event(session="s", raw="老王来了", entities=[a])
    store.delete_event(ev)
    assert store.conn.execute(
        "SELECT count(*) FROM event_entities WHERE event_id = ?", (ev,)
    ).fetchone()[0] == 0


# ---------------------------------------------------------------- 迁移回填
def test_backfill_from_existing_rows(tmp_path):
    """001 的老库升到 002 后，历史事件的关联要补齐。"""
    db = tmp_path / "old.db"
    c = connect(db)
    migrate(c, target=1)
    eid = c.execute("INSERT INTO entities(session, canonical) VALUES ('s','老王')").lastrowid
    ev = c.execute(
        "INSERT INTO events(session, event_time, ingest_time, raw, entities)"
        " VALUES ('s',1,1,'老王来了',?)", (f"[{eid}]",)
    ).lastrowid
    # 一条脏的、一条空的
    c.execute("INSERT INTO events(session, event_time, ingest_time, raw, entities)"
              " VALUES ('s',1,1,'x','[\"abc\"]')")
    c.execute("INSERT INTO events(session, event_time, ingest_time, raw, entities)"
              " VALUES ('s',1,1,'y','[]')")

    applied = migrate(c)
    assert applied[0] == 2 and len(applied) >= 4
    rows = c.execute("SELECT event_id, entity_id FROM event_entities").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(ev, eid)]
    c.close()


# ---------------------------------------------------------------- 写入耗时
def test_link_cost(store):
    a = _eid(store, "老王")
    ev = store.add_event(session="s", raw="x", entities=[])
    t0 = time.perf_counter()
    for _ in range(50):
        store.link_entities(ev, [a])
    ms = (time.perf_counter() - t0) * 1000
    assert ms < 200, f"50 次关联 {ms:.0f}ms，慢得不像话"


# ---------------------------------------------------------------- extract_runs
def test_run_upsert_same_window(store):
    kw = dict(session="s", start_idx=0, end_idx=9)
    store.record_run(status="degraded", error="timeout", **kw)
    store.record_run(status="ok", n_events=3, **kw)
    rows = store.conn.execute("SELECT * FROM extract_runs").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "ok" and rows[0]["n_events"] == 3


def test_run_stats(store):
    store.record_run(session="s", start_idx=0, end_idx=9, status="ok",
                     n_events=2, cost=0.001)
    store.record_run(session="s", start_idx=10, end_idx=19, status="degraded",
                     n_events=0, cost=0.002)
    store.record_run(session="s", start_idx=20, end_idx=29, status="skipped")
    st = store.run_stats("s")
    assert st["total"] == 3
    assert st["ok"] == 1
    # 成功率的分母只算"真的调过模型"的窗口：skipped 是预筛挡掉的，
    # 算进去会让"一次都没失败"看起来像 33%。
    assert st["attempted"] == 2
    assert abs(st["success_rate"] - 0.5) < 1e-4
    assert st["events"] == 2
    assert abs(st["cost"] - 0.003) < 1e-9


def test_run_rejects_bad_status(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.record_run(session="s", start_idx=0, end_idx=1, status="weird")
