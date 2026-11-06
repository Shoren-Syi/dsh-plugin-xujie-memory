"""M6.3 原文不可变。

这组测试的立场：`events.raw` 是唯一真相源。任何"能改"的路径都是 bug，
包括那些看起来不像修改的写法（`INSERT OR REPLACE`）。
"""

from __future__ import annotations

import sqlite3

import pytest

from hippo.core.db import connect
from hippo.core.migrate import migrate
from hippo.core.store import Store, StoreError


@pytest.fixture()
def store(tmp_path):
    db = tmp_path / "m.db"
    conn = connect(db)
    migrate(conn)
    s = Store(conn)
    yield s
    s.close()


def _add(store: Store, raw: str = "我昨天搬家了，从朝阳搬到海淀") -> int:
    return store.add_event(session="s", raw=raw, summary="用户搬家", keywords=["搬家"])


# ---------------------------------------------------------------- UPDATE
def test_update_raw_is_rejected(store):
    eid = _add(store)
    with pytest.raises(sqlite3.IntegrityError, match="events.raw is immutable"):
        store.conn.execute("UPDATE events SET raw = '用户搬到了海淀' WHERE id = ?", (eid,))


def test_update_raw_to_null_is_rejected(store):
    """`<>` 比较 NULL 会得到 NULL（假），所以改 NULL 这条路要单独锁。

    触发器里用的是 `IS NOT`，它能正确处理 NULL。
    """
    eid = _add(store)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.conn.execute("UPDATE events SET raw = NULL WHERE id = ?", (eid,))


def test_update_non_raw_columns_still_works(store):
    """不可变的只有 raw。权重、召回次数、归档标记都得能改，
    否则衰减（M14）和归档（M24）全部做不了。
    """
    eid = _add(store)
    store.conn.execute(
        "UPDATE events SET weight = 0.2, recall_count = 3, archived = 1 WHERE id = ?",
        (eid,))
    row = store.conn.execute(
        "SELECT weight, recall_count, archived FROM events WHERE id = ?", (eid,)).fetchone()
    assert row["weight"] == 0.2 and row["recall_count"] == 3 and row["archived"] == 1


def test_updating_raw_to_same_value_is_allowed(store):
    """写回同一个值不算修改——批量 UPDATE 里常见的 `SET raw = raw` 不该炸。"""
    eid = _add(store)
    store.conn.execute("UPDATE events SET raw = raw WHERE id = ?", (eid,))


# ---------------------------------------------------------------- DELETE
def test_delete_is_rejected(store):
    eid = _add(store)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM events WHERE id = ?", (eid,))


def test_delete_everything_is_rejected(store):
    """`DELETE FROM events`（不带 WHERE）也必须被拦。"""
    _add(store)
    _add(store)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM events")


def test_or_replace_cannot_bypass_immutability(store):
    """`INSERT OR REPLACE` 内部是"先删后插"，默认设置下 BEFORE DELETE 不触发。

    实测：`PRAGMA recursive_triggers = OFF` 时这句能成功把 raw 换掉，
    而所有 UPDATE 触发器都不会响。db.connect() 已全局打开该 PRAGMA。
    这条测试同时锁住"PRAGMA 没被谁改回去"。
    """
    eid = _add(store)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute(
            "INSERT OR REPLACE INTO events(id, session, event_time, ingest_time, raw)"
            " VALUES (?, 's', 1, 1, '被替换掉的原文')", (eid,))

    row = store.conn.execute("SELECT raw FROM events WHERE id = ?", (eid,)).fetchone()
    assert row["raw"] == "我昨天搬家了，从朝阳搬到海淀"


def test_recursive_triggers_is_on_for_every_connection(tmp_path):
    """PRAGMA 是连接级开关，不是库级。任何一个新连接都得重新打开。"""
    db = tmp_path / "p.db"
    for _ in range(2):
        c = connect(db)
        try:
            assert c.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
        finally:
            c.close()


# ---------------------------------------------------------------- 维护开关
def test_delete_event_uses_maintenance_flag_and_clears_it(store):
    eid = _add(store)
    assert store.delete_event(eid) is True
    assert store.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    # 开关不能残留：残留等于库长期处于"可以删"的状态
    left = store.conn.execute("SELECT count(*) FROM maint_flags").fetchone()[0]
    assert left == 0


def test_maintenance_flag_does_not_leak_after_failed_delete(store):
    """删一个不存在的 id 也应把开关清掉（事务保证）。

    如果开关漏掉，下一次任何 `DELETE FROM events` 都会静默成功——
    比崩溃危险，因为没人会发现。
    """
    _add(store)                       # 必须有一条真数据：空表上 DELETE 不触发任何行级触发器
    store.delete_event(999999)
    assert store.conn.execute("SELECT count(*) FROM maint_flags").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM events")


def test_delete_on_empty_table_cannot_be_blocked(store):
    """空表 DELETE 不报错，因为触发器是行级的、一行都没扫到。

    这不是漏洞（本来就没东西可删），但它解释了一类困惑：
    "我在测试里试了 DELETE 没被拦" —— 那条表里没有数据。
    """
    store.conn.execute("DELETE FROM events")      # 不抛
    assert store.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


# ---------------------------------------------------------------- 写入期校验
def test_add_event_rejects_polished_text(store):
    """模型爱把话说通顺。一旦允许，库里的"原话"就不是原话了。"""
    source = "我昨天搬家了，从朝阳搬到海淀"
    with pytest.raises(StoreError, match="不是原文片段"):
        store.add_event(session="s", raw="我昨天搬家了，从朝阳区搬到了海淀区",
                        source_text=source)


def test_add_event_accepts_exact_span(store):
    source = "我昨天搬家了，从朝阳搬到海淀"
    eid = store.add_event(session="s", raw="从朝阳搬到海淀", source_text=source)
    assert eid > 0


def test_add_event_rejects_width_variant(store):
    """全角 `８点` 与半角 `8点` 归一化后相同，但**原文里没有 ８ 这个字**。

    存进去的那一版必须能在原文里被指认，否则"原文"就是个近似值。
    """
    with pytest.raises(StoreError, match="不是原文片段"):
        store.add_event(session="s", raw="我８点起床", source_text="我8点起床")


def test_add_event_allows_upsert_when_source_not_given(store):
    """降级兜底（persist_degraded）存的是整窗原文，没有单一 source 可比。"""
    eid = store.add_event(session="s", raw="user: 早\nassistant: 早上好")
    assert eid > 0


# ---------------------------------------------------------------- 摘要截断标记
def test_summary_truncated_is_stored(store):
    eid = store.add_event(session="s", raw="原文", summary="上半句…",
                          summary_truncated=1)
    row = store.conn.execute(
        "SELECT summary_truncated FROM events WHERE id = ?", (eid,)).fetchone()
    assert row[0] == 1


def test_summary_truncated_defaults_to_zero(store):
    eid = _add(store)
    row = store.conn.execute(
        "SELECT summary_truncated FROM events WHERE id = ?", (eid,)).fetchone()
    assert row[0] == 0


# ---------------------------------------------------------------- 迁移
def test_migration_backfills_flag_column(tmp_path):
    """v4 老库升到 v5：新列有默认值，老行不该是 NULL。"""
    from hippo.core.migrate import current_version

    db = tmp_path / "old.db"
    c = connect(db)
    migrate(c, target=4)
    c.execute("INSERT INTO events(session, event_time, ingest_time, raw, summary)"
              " VALUES ('s',1,1,'老数据的原文','老摘要')")
    assert current_version(c) == 4

    migrate(c)
    assert current_version(c) >= 5
    row = c.execute("SELECT summary_truncated FROM events").fetchone()
    assert row[0] == 0
    # 老库里已经存在的 raw 不能被迁移碰到
    assert c.execute("SELECT raw FROM events").fetchone()[0] == "老数据的原文"
    c.close()
