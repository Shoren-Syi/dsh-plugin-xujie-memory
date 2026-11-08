# -*- coding: utf-8 -*-
"""M57 验收：迁移跨进程/线程串行化。

真实 bug：两个实例同时打开新库，都读到"还没迁移"，都去 ALTER TABLE，
后提交的进程崩在 `duplicate column name`；修好迁移锁后，第二个又崩在
`PRAGMA journal_mode=WAL`（WAL 切换要排他锁，而 busy_timeout 还没设）。
两个都要锁住。
"""
from __future__ import annotations

import sqlite3
import threading

from hippo.core.db import connect
from hippo.core.migrate import available_migrations, current_version, migrate


def test_two_threads_migrate_same_fresh_db(tmp_path):
    db = tmp_path / "race.db"
    latest = max(m.version for m in available_migrations())
    errors: list[BaseException] = []

    def run() -> None:
        try:
            conn = connect(db)
            try:
                migrate(conn)
            finally:
                conn.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"并发迁移不应该报错：{errors!r}"
    conn = connect(db)
    try:
        assert current_version(conn) == latest
        cols = [r[1] for r in conn.execute("pragma table_info(events)")]
        assert "importance" in cols and "key_values" in cols
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path):
    db = tmp_path / "twice.db"
    conn = connect(db)
    try:
        first = migrate(conn)
        second = migrate(conn)
        assert first and second == []
    finally:
        conn.close()


def test_journal_mode_tolerates_locked(tmp_path, monkeypatch):
    """WAL 切换被锁住时不应该让启动失败。"""
    import hippo.core.db as dbmod

    real_connect = sqlite3.connect

    class FakeConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args):
            if sql.strip().upper().startswith("PRAGMA JOURNAL_MODE"):
                raise sqlite3.OperationalError("database is locked")
            return self._inner.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(dbmod.sqlite3, "connect",
                        lambda *a, **k: FakeConn(real_connect(*a, **k)))
    conn = dbmod.connect(tmp_path / "locked.db")
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    finally:
        conn._inner.close()


def test_lock_treats_permission_error_as_busy(tmp_path, monkeypatch):
    """Windows 的 O_EXCL 共享冲突抛 PermissionError，必须当成"占用"而不是崩溃。

    真实复现：6 次并发迁移里挂 4 次，栈顶是
    `PermissionError: [Errno 13] ... mem.db.migrate.lock`。
    """
    import os

    import hippo.core.migrate_lock as lockmod

    db = sqlite3.connect(tmp_path / "perm.db")
    db.execute("PRAGMA journal_mode = WAL")
    real_open = os.open
    state = {"raised": 0}

    def flaky_open(path, flags, *args, **kwargs):
        if str(path).endswith(".migrate.lock") and state["raised"] < 1:
            state["raised"] += 1
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(lockmod.os, "open", flaky_open)
    with lockmod.migration_lock(db, timeout=3):
        pass
    assert state["raised"] == 1, "应该先撞一次 PermissionError，然后重试成功"
    db.close()
