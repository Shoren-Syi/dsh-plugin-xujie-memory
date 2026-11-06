"""M1.1 验收：DDL、幂等、约束、事务回滚、批量性能。"""

from __future__ import annotations

import sqlite3
import time

import pytest

from hippo.core.db import connect
from hippo.core.migrate import available_migrations, current_version, migrate

EXPECTED_TABLES = {
    "events", "entities", "card_traits", "facts", "doubts", "plans",
    "affect", "ledger", "turns", "kw_index", "metrics", "schema_meta",
    "events_fts",
    # 002 / 003（M5 前置）
    "event_entities", "extract_runs",
    # 005（M6.3）
    "maint_flags",
    # 010（M12）
    "session_state",
    # 011（M15）
    "consolidation_log",
    # 015（M25）
    "curiosity",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'events_fts_%'"
    ).fetchall()
    return {r["name"] for r in rows}


def test_tables_exist(conn):
    assert _tables(conn) == EXPECTED_TABLES


def test_indexes_created(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    )}
    for required in (
        "idx_events_session_tier", "idx_events_time", "idx_kw_event",
        "idx_plans_due", "idx_traits_entity", "idx_facts_lookup",
    ):
        assert required in names


def test_init_is_idempotent(db_path):
    """首次迁移覆盖到最新版本，第二次什么也不做。

    这里曾经写死 `== [1,2,3,4,5]`，于是每加一个迁移这条就假红一次。
    版本号是工程进度，不是契约；契约是"一次到底、二次幂等"。
    """
    from hippo.core.migrate import available_migrations

    latest = max(m.version for m in available_migrations())
    c = connect(db_path)
    first = migrate(c)
    assert first == list(range(1, latest + 1))
    second = migrate(c)
    assert second == []
    assert current_version(c) == latest
    c.close()


def test_foreign_keys_enabled(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO card_traits(entity_id, trait, evidence) VALUES (99999,'x','[]')"
        )


def test_wal_mode(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_check_constraints(conn):
    base = ("INSERT INTO events(session, event_time, ingest_time, tier, raw)"
            " VALUES ('s', 1, 1, ?, ?)")
    with pytest.raises(sqlite3.IntegrityError):   # tier 越界
        conn.execute(base, (5, "x"))
    with pytest.raises(sqlite3.IntegrityError):   # raw 为空
        conn.execute(base, (2, ""))
    conn.execute(base, (2, "正常内容"))


def test_source_kind_constraints(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events(session,event_time,ingest_time,source,raw)"
            " VALUES ('s',1,1,'nobody','x')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events(session,event_time,ingest_time,kind,raw)"
            " VALUES ('s',1,1,'whatever','x')"
        )


def test_broken_migration_rolls_back(tmp_path):
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "001_ok.sql").write_text(
        "CREATE TABLE good(id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE schema_meta(version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
        " applied_at INTEGER NOT NULL);",
        encoding="utf-8",
    )
    (mig_dir / "002_bad.sql").write_text(
        "CREATE TABLE half_done(id INTEGER PRIMARY KEY);\n"
        "INSERT INTO table_that_does_not_exist(id) VALUES (1);\n",  # 运行时失败
        encoding="utf-8",
    )
    assert len(available_migrations(mig_dir)) == 2

    db = tmp_path / "t.db"
    c = connect(db)
    with pytest.raises(sqlite3.Error):
        migrate(c, directory=mig_dir)
    tables = {r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "half_done" not in tables, "失败的迁移必须整体回滚"
    assert current_version(c) == 1, "版本号不能前进"
    c.close()


def test_duplicate_migration_version_rejected(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    (d / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (d / "001_b.sql").write_text("SELECT 2;", encoding="utf-8")
    with pytest.raises(RuntimeError, match="版本号重复"):
        available_migrations(d)


def test_bulk_insert_100k_performance(store, tmp_path):
    """10 万条事件批量写入，**必须比逐条写快至少 3 倍**。

    这里曾经断言 `elapsed < 5.0`。同一个测试在开发机 1.4s 通过、
    换一台机器 5.6s 变红，而代码一行没动 —— 绝对秒数是**机器的属性**，
    不是这段代码的属性。它测的是"我这台机器快不快"。

    真正要守的性质是那条优化本身：逐行 add_event 会让 FTS 逐条增量索引，
    所以批量路径摘触发器 + 一次 rebuild，应当是数量级的差距。
    比值在同机同盘下才可比，所以基线也在同一次运行里测。

    行数从 10 万降到 5 万：断言的是比值，样本越大比值越稳，
    但 10 万 × 两轮要 15s，把整个测试套拖慢一倍不值。
    """
    n_rows = 50_000
    now = int(time.time())
    rows = [
        ("bench", now - i, now, 2, 0.5, 0.5,
         f"关键词{i % 500} 样本词", f"摘要内容样本 {i} 测试数据", f"原文 {i}")
        for i in range(n_rows)
    ]
    t0 = time.perf_counter()
    n = store.bulk_import_events(rows)
    bulk_s = time.perf_counter() - t0
    assert n == n_rows
    assert store.conn.execute("SELECT count(*) FROM events").fetchone()[0] == n_rows

    # 基线：同一台机器上逐行写 200 条，换算成单行耗时
    from hippo.core.db import connect as _connect
    from hippo.core.migrate import migrate as _migrate
    from hippo.core.store import Store

    base_db = tmp_path / "baseline.db"
    c2 = _connect(base_db)
    _migrate(c2)
    s2 = Store(c2)
    n_base = 200
    t0 = time.perf_counter()
    for i in range(n_base):
        s2.add_event(session="bench", event_time=now - i, ingest_time=now, tier=2,
                     weight=0.5, w0=0.5, keywords=f"关键词{i % 500} 样本词",
                     summary=f"摘要内容样本 {i} 测试数据", raw=f"原文 {i}")
    per_row_single = (time.perf_counter() - t0) / n_base
    s2.close()

    per_row_bulk = bulk_s / n_rows
    speedup = per_row_single / per_row_bulk
    print(f"\n{n_rows} 批量 = {bulk_s:.2f}s ({per_row_bulk * 1000:.2f}ms/行)；"
          f"逐行 {per_row_single * 1000:.2f}ms/行；快 {speedup:.1f}x")
    assert speedup > 3.0, (
        f"批量路径只比逐行快 {speedup:.1f}x（批量 {per_row_bulk * 1000:.2f}ms/行，"
        f"逐行 {per_row_single * 1000:.2f}ms/行）。"
        "优化退化了：检查 _FTS_TRIGGERS 是不是没被摘掉。"
    )


def test_bulk_import_equivalent_to_single_insert(store):
    """批量路径必须与逐条路径产生同样的索引结果，且触发器要恢复。"""
    now = int(time.time())
    rows = [("s1", now, now, 2, 0.5, 0.5, "老王 跳槽", "老王从字节跳槽了", "老王从字节跳槽了")]
    store.bulk_import_events(rows)

    # 触发器已恢复：后续单条写入仍然同步索引
    eid = store.add_event(session="s1", raw="后来他去了创业公司。",
                          summary="老王去了创业公司", keywords=["老王", "创业公司"])
    triggers = {r["name"] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"events_ai", "events_ad", "events_au"} <= triggers

    assert len(store.search_by_tokens("s1", ["老王"])) == 2, "批量行也要进 kw_index"
    assert store.fts_search("老王从字节跳槽了"), "批量行也要进 FTS"
    assert eid > 0
