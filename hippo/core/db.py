"""连接管理与事务边界。

两条铁律：
  1. 每个连接都必须显式开 foreign_keys —— SQLite 是连接级开关，不是库级。
  2. WAL 是持久设置，但每个连接仍会 PRAGMA 一次，保证跨机器拷贝后行为一致。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开一个配置好的连接。

    check_same_thread=False 只给代理的常驻写连接用：它在事件循环线程创建，
    也在同一线程使用，但 Starlette 的 TestClient 会换线程，所以要放开。

    isolation_level=None 表示关闭 Python 的隐式事务管理，
    事务边界全部由 transaction() 显式控制 —— 这样才能保证
    迁移脚本里的 BEGIN/COMMIT 不被驱动偷偷插一条 COMMIT。
    """
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, isolation_level=None, timeout=10.0,
                           check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # busy_timeout 必须在 journal_mode **之前**：WAL 是库级持久设置，
    # 切换它要拿排他锁。另一个进程正在写时，先设 timeout 才能等，否则
    # 直接 `database is locked`（两个实例同时冷启动时实测会崩）。
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # 等不到锁也不致命：WAL 是持久设置，另一个进程已经设过就是 WAL；
        # 真的还是别的模式，后续写入也能工作，只是并发差一点。
        pass
    conn.execute("PRAGMA synchronous = NORMAL")
    # recursive_triggers 默认 OFF。OFF 时 `INSERT OR REPLACE` 与
    # `ON CONFLICT DO UPDATE` 的内部删除**不触发** BEFORE DELETE 触发器，
    # 于是 events.raw 的不可变约束可以被一句 OR REPLACE 绕过。
    # 这是实测出来的（tests/test_m6_3_immutable.py 锁住了它），不是文档推断。
    conn.execute("PRAGMA recursive_triggers = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务。BEGIN IMMEDIATE 立刻拿写锁，避免升级锁时死锁。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def supports_trigram() -> bool:
    """SQLite 是否支持 FTS5 + trigram 分词器（需 ≥3.34）。"""
    try:
        c = sqlite3.connect(":memory:")
        try:
            c.execute("CREATE VIRTUAL TABLE _t USING fts5(x, tokenize='trigram')")
        finally:
            c.close()
        return True
    except sqlite3.Error:
        return False
