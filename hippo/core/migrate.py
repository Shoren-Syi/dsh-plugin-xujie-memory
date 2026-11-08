"""迁移执行器。

规则（对应手册 M1.1）：
  - 迁移文件命名 `NNN_name.sql`，编号递增，只增不改。
  - 每个文件在**独立事务**里执行：失败立刻回滚，库里不留半截结构。
  - 版本行与结构变更同一事务提交，不会出现"表建了但没记账"。
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .db import table_exists
from .migrate_lock import migration_lock

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "data" / "migrations"
_NAME_RE = re.compile(r"^(\d+)_(.+)\.sql$")
# 文件头 `-- @rebuild: entities` = 本迁移要重建带外键引用的表。
# 由执行器在事务外关外键，而不是让 SQL 自己写 PRAGMA：
# 迁移是连续跑的，一个没配对的 ON 会让后面所有迁移失去外键保护。
_REBUILD_RE = re.compile(r"^--\s*@rebuild:\s*(\w+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


def available_migrations(directory: Path | str | None = None) -> list[Migration]:
    d = Path(directory) if directory else MIGRATIONS_DIR
    if not d.exists():
        return []
    found: list[Migration] = []
    for f in d.iterdir():
        m = _NAME_RE.match(f.name)
        if m:
            found.append(Migration(int(m.group(1)), m.group(2), f))
    found.sort(key=lambda x: x.version)
    _assert_unique(found)
    return found


def _assert_unique(items: list[Migration]) -> None:
    seen: set[int] = set()
    for it in items:
        if it.version in seen:
            raise RuntimeError(f"迁移版本号重复: {it.version} ({it.path.name})")
        seen.add(it.version)


def current_version(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "schema_meta"):
        return 0
    row = conn.execute("SELECT max(version) FROM schema_meta").fetchone()
    return int(row[0] or 0)


def migrate(
    conn: sqlite3.Connection,
    target: int | None = None,
    directory: Path | str | None = None,
) -> list[int]:
    """把库升到最新（或 target）。返回本次实际应用的版本号列表。

    幂等：重复调用只返回空列表。
    """
    # 跨进程串行：两个实例同时打开新库时，只有一个能跑迁移，
    # 另一个等锁释放后重新读版本号（已是最新，直接返回空列表）。
    with migration_lock(conn):
        applied: list[int] = []
        for mig in available_migrations(directory):
            if target is not None and mig.version > target:
                break
            if mig.version <= current_version(conn):
                continue
            _apply(conn, mig)
            applied.append(mig.version)
        return applied


def _child_counts(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    """所有外键指向 table 的子表，以及它们的行数。

    重建表时这是唯一的兜底：`DROP TABLE` 在 foreign_keys=ON 时会顺着
    ON DELETE CASCADE 把子表数据删干净，而且**不报错**。
    事后 `foreign_key_check` 也查不出来——级联删完恰好是一致的，
    只是数据没了。第一版 006 就这么把 event_entities 清空了，
    `foreign_key_check` 给了个空列表，测试却在回填断言上红了。
    """
    out: dict[str, int] = {}
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )]
    for name in tables:
        if name == table:
            continue
        for fk in conn.execute(f'PRAGMA foreign_key_list("{name}")').fetchall():
            if fk["table"] == table:
                out[name] = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                break
    return out


def _apply(conn: sqlite3.Connection, mig: Migration) -> None:
    body = mig.path.read_text(encoding="utf-8")
    if "PRAGMA foreign_keys" in body.upper():
        # 迁移里不许动这个开关，否则事务中途关掉外键会留下脏数据
        raise RuntimeError(f"{mig.path.name} 不允许包含 PRAGMA foreign_keys")

    rebuild = _REBUILD_RE.search(body)
    ts = int(time.time())
    name = mig.name.replace("'", "''")
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{body}\n"
        "INSERT INTO schema_meta(version, name, applied_at) "
        f"VALUES ({mig.version}, '{name}', {ts});\n"
    )

    before: dict[str, int] = {}
    if rebuild:
        if conn.in_transaction:
            conn.execute("COMMIT")
        before = _child_counts(conn, rebuild.group(1))
        conn.execute("PRAGMA foreign_keys = OFF")

    try:
        # executescript 会先 COMMIT 再执行脚本，所以 BEGIN 必须写在脚本里
        conn.executescript(script)
        if rebuild:
            after = _child_counts(conn, rebuild.group(1))
            lost = {k: (before[k], after.get(k, 0)) for k in before if after.get(k, 0) < before[k]}
            if lost:
                raise RuntimeError(
                    f"{mig.path.name} 重建 {rebuild.group(1)} 时子表数据丢失 {lost}"
                )
        conn.execute("COMMIT")
    except BaseException:
        # executescript 抛错时事务仍开着，必须显式回滚
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        if rebuild:
            conn.execute("PRAGMA foreign_keys = ON")
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise RuntimeError(
                    f"{mig.path.name} 重建后外键失配 {len(broken)} 处："
                    f"{[tuple(r) for r in broken[:5]]}"
                )


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """external content 表重建。数据导入或触发器修好后用。"""
    conn.execute("INSERT INTO events_fts(events_fts) VALUES ('rebuild')")
