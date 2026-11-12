# -*- coding: utf-8 -*-
"""M74 验收：备份 / 列表 / 恢复（面板入口）。

背景：`backup` / `restore` / `ensure_daily` 以前只挂在 CLI，插件用户看不到。
现在内核有 `/backup`、`/backups`、`/restore`，维护循环还会每日自动备份。
"""
from __future__ import annotations

import sqlite3

from hippo.config import Config
from hippo.core import backup as backup_mod
from hippo.core.store import Store


def _make(db, text: str) -> None:
    cfg = Config()
    cfg.db_path = str(db)
    with Store.open(db, cfg) as store:
        store.add_turns("s", [("user", text, 0)], ts=1_760_000_000)


def _turns(db) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("select content from turns order by idx")]
    finally:
        conn.close()


def test_backup_creates_snapshot(tmp_path):
    db = tmp_path / "mem.db"
    _make(db, "第一条")
    target = backup_mod.backup(db, keep=3)
    assert target.exists()
    assert target.stat().st_size > 0
    backups = backup_mod.list_backups(target.parent)
    assert len(backups) == 1


def test_restore_replaces_db_and_keeps_safety_copy(tmp_path):
    db = tmp_path / "mem.db"
    _make(db, "备份时的内容")
    snap = backup_mod.backup(db, keep=3)
    _make(db, "备份之后新增的内容")
    assert len(_turns(db)) == 2

    restored = backup_mod.restore(db, snap)
    assert restored == db
    assert _turns(db) == ["备份时的内容"], "恢复后只应剩备份时的内容"
    safety = list(db.parent.glob("mem.db.pre-restore-*.bak"))
    assert safety, "旧库要留一份后悔药"


def test_ensure_daily_runs_once_per_day(tmp_path):
    db = tmp_path / "mem.db"
    _make(db, "内容")
    first = backup_mod.ensure_daily(db, keep=3)
    second = backup_mod.ensure_daily(db, keep=3)
    assert first is not None
    assert second is None, "同一天第二次不应再备份"
