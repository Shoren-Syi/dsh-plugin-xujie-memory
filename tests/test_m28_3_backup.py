"""M28.3 · 备份与恢复（VACUUM INTO 快照 / 保留 7 份 / 恢复 / 旧版读新库）。"""

from __future__ import annotations

import pytest

from hippo.core import backup as backup_mod
from hippo.core.store import Store


def _raws(db_path) -> list[str]:
    s = Store.open(db_path)
    try:
        return [r["raw"] for r in s.conn.execute("SELECT raw FROM events ORDER BY id")]
    finally:
        s.close()


# ------------------------------------------------------------------ 备份
def test_backup_creates_consistent_snapshot(store, db_path):
    store.add_event(session="b1", raw="备份我", keywords=["备份"])
    target = backup_mod.backup(db_path, now=1700000000)

    assert target.exists()
    assert target.parent == backup_mod.backups_dir_for(db_path)
    # 快照里必须有刚写的事件（WAL 直接 cp 会漏掉最后几轮，VACUUM INTO 不会）
    assert "备份我" in _raws(target)


def test_backup_prunes_to_keep(store, db_path):
    store.add_event(session="p", raw="保留最新", keywords=["保留"])
    t0 = 1700000000
    for i in range(9):
        backup_mod.backup(db_path, keep=7, now=t0 + i * 60)

    files = backup_mod.list_backups(backup_mod.backups_dir_for(db_path))
    assert len(files) == 7  # 9 份只留最新 7 份
    assert files == sorted(files, key=lambda p: p.name)


def test_backup_same_second_does_not_overwrite(store, db_path):
    store.add_event(session="s", raw="同秒两次", keywords=["同秒"])
    t0 = 1700000000
    a = backup_mod.backup(db_path, now=t0)
    b = backup_mod.backup(db_path, now=t0)
    assert a != b and b.exists() and a.exists()


def test_backup_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_mod.backup(tmp_path / "nope.db")


# ------------------------------------------------------------------ 恢复
def test_restore_latest_roundtrip(store, db_path):
    store.add_event(session="r", raw="旧的事", keywords=["旧"])
    target = backup_mod.backup(db_path, now=1700000000)
    store.add_event(session="r", raw="新的事", keywords=["新"])

    # 还原前必须关掉连接：Windows 上 rename 一个被打开的文件会 PermissionError
    store.close()
    backup_mod.restore(db_path, target)

    assert "旧的事" in _raws(db_path)
    assert "新的事" not in _raws(db_path)  # 备份后的写入被回退
    bak = list(db_path.parent.glob(f"{db_path.name}.pre-restore-*.bak"))
    assert bak, "旧库应改名留档，不能直接删"


def test_restore_missing_backup_raises(store, db_path, tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_mod.restore(db_path, tmp_path / "nope.db")


# ------------------------------------------------------------------ 每日自动备份
def test_ensure_daily_only_once_per_day(store, db_path):
    store.add_event(session="d", raw="有数据", keywords=["数据"])
    t0 = 1700000000
    first = backup_mod.ensure_daily(db_path, now=t0)
    assert first is not None
    # 同一天再启动 serve：不重复备份
    assert backup_mod.ensure_daily(db_path, now=t0 + 3600) is None
    # 第二天：补一份新的
    nxt = backup_mod.ensure_daily(db_path, now=t0 + 86400)
    assert nxt is not None and nxt != first


# ------------------------------------------------------------------ 旧版本读新库
def test_old_version_reads_new_db(store, db_path):
    """迁移只加不改的守门：未来加表/加列后，当前版本照样能打开读。"""
    store.add_event(session="u", raw="旧数据还在", keywords=["旧"])
    store.conn.execute("CREATE TABLE IF NOT EXISTS future_table (id INTEGER PRIMARY KEY, v TEXT)")
    store.conn.execute("ALTER TABLE events ADD COLUMN future_col INTEGER DEFAULT 0")
    store.close()

    assert _raws(db_path) == ["旧数据还在"]
