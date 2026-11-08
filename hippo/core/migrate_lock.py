# -*- coding: utf-8 -*-
"""跨进程迁移锁。

为什么需要：两个 DSH 实例（桌面 + Web）可能同时打开同一个库。
两边都在 `migrate()` 里读到"还没升到 019"，然后都去 `ALTER TABLE`，
后提交的那个会拿到 `duplicate column name` 直接崩——实测确实会崩。

做法：迁移前抢一个 `<db>.migrate.lock`（`O_CREAT|O_EXCL`）。
- 抢到的进程跑迁移；
- 抢不到的进程按 50ms 轮询等待，等锁释放后再跑（此时 `current_version`
  已经是最新的，自然不会重复执行）；
- 锁超过 `stale_seconds` 未释放视为残留（进程被强杀），强行接管。
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

__all__ = ["migration_lock", "db_path_of"]

STALE_SECONDS = 120


def db_path_of(conn) -> str:
    """从连接里问数据库文件路径；内存库返回空串。"""
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
            if name == "main" and file:
                return str(file)
    except Exception:  # noqa: BLE001
        return ""
    return ""


@contextmanager
def migration_lock(conn, *, timeout: float = 20.0, poll: float = 0.05):
    """拿迁移锁；内存库/拿不到路径时直接放行（退化为原行为）。"""
    path = db_path_of(conn)
    if not path:
        yield False
        return
    lock_path = Path(path + ".migrate.lock")
    deadline = time.monotonic() + timeout
    acquired = False
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            acquired = True
            break
        except (FileExistsError, PermissionError):
            # Windows 上 `O_CREAT|O_EXCL` 和"另一个进程正在创建/删除同一个文件"
            # 撞车时抛的是 PermissionError（共享冲突），不是 FileExistsError。
            # 不接住它，并发迁移就会偶发崩在抢锁这一步——实测 6 次里挂 4 次。
            # 语义上两者都是"现在拿不到"，继续等就行。
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0
            if age > STALE_SECONDS:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                # 等超时：放行但不持锁。宁可两个进程都尝试（幂等性由
                # schema_meta 版本行兜底），也不要让内核启动失败。
                yield False
                return
            time.sleep(poll)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                lock_path.unlink()
            except OSError:
                pass
