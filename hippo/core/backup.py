"""数据库备份与恢复编排（M28.3）。

分工：所有 SQL 在 `store.py`（唯一 DAO）；本模块只做文件命名、保留策略、
恢复流程与"每日自动备份"的日程判断。

铁律：WAL 模式下**不许直接 cp** mem.db —— 会拷到半个事务。快照统一走
`Store.backup_to()`（内部 `VACUUM INTO`）。恢复时旧库的 `-wal` / `-shm`
必须一并移走，否则 SQLite 会把旧文件的未完成事务套到新库上。
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path

from ..log import get_logger
from .store import Store

log = get_logger("backup")

BACKUP_PREFIX = "mem-"
DEFAULT_KEEP = 7


def backups_dir_for(db_path: str | Path) -> Path:
    """备份目录：与库文件同级，`<dir>/backups/`。"""
    return Path(db_path).expanduser().parent / "backups"


def _stamp(now: int | None = None) -> str:
    ts = time.time() if now is None else now
    return datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M%S")


def list_backups(backups_dir: str | Path) -> list[Path]:
    """按文件名（即时间戳）升序列出备份，最旧的在前。"""
    d = Path(backups_dir).expanduser()
    if not d.is_dir():
        return []
    return sorted(d.glob(f"{BACKUP_PREFIX}*.db"), key=lambda p: p.name)


def prune(backups_dir: str | Path, *, keep: int = DEFAULT_KEEP,
          protect: str | Path | None = None) -> int:
    """只保留最新 keep 份，返回删掉的份数。protect 指向的那份永不删。"""
    files = list_backups(backups_dir)
    keep = max(1, int(keep))
    if len(files) <= keep:
        return 0
    protect_path = Path(protect).expanduser() if protect is not None else None
    removed = 0
    for p in files[:-keep]:
        if protect_path is not None and p == protect_path:
            continue
        try:
            p.unlink()
            removed += 1
        except OSError as exc:
            log.warning("backup.prune_failed",
                        extra={"fields": {"path": str(p), "err": str(exc)}})
    return removed


def backup(db_path: str | Path, *, keep: int = DEFAULT_KEEP,
           now: int | None = None,
           backups_dir: str | Path | None = None) -> Path:
    """生成一份一致快照，返回备份文件路径。"""
    db_path = Path(db_path).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    outdir = (Path(backups_dir).expanduser() if backups_dir is not None
              else backups_dir_for(db_path))
    outdir.mkdir(parents=True, exist_ok=True)

    stamp = _stamp(now)
    target = outdir / f"{BACKUP_PREFIX}{stamp}.db"
    n = 1
    while target.exists():  # 同一秒内第二次备份：加序号，不覆盖
        target = outdir / f"{BACKUP_PREFIX}{stamp}-{n}.db"
        n += 1

    store = Store.open(db_path)
    try:
        store.backup_to(target)
        size = target.stat().st_size
    finally:
        store.close()

    removed = prune(outdir, keep=keep, protect=target)
    log.info("backup.ok", extra={"fields": {
        "path": str(target), "bytes": size, "pruned": removed}})
    return target


def latest_backup(backups_dir: str | Path) -> Path | None:
    files = list_backups(backups_dir)
    return files[-1] if files else None


def restore(db_path: str | Path, backup_path: str | Path,
            *, now: int | None = None) -> Path:
    """用备份覆盖当前库。

    调用前提：**代理已停**（这是 ROLLBACK.md 的第一步；运行中恢复会被
    文件锁挡住）。旧库不直接删，改名成 `mem.db.pre-restore-<ts>.bak`
    留作后悔药。
    """
    db_path = Path(db_path).expanduser()
    backup_path = Path(backup_path).expanduser()
    if not backup_path.exists():
        raise FileNotFoundError(f"备份不存在：{backup_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    safety: Path | None = None
    if db_path.exists():
        safety = db_path.with_name(f"{db_path.name}.pre-restore-{_stamp(now)}.bak")
        db_path.rename(safety)
        # 旧库的 WAL/SHM 描述的是旧文件；留着会被当成新库的未完成事务。
        for suffix in ("-wal", "-shm"):
            side = Path(str(db_path) + suffix)
            if side.exists():
                side.unlink()

    shutil.copy2(backup_path, db_path)
    log.info("backup.restore", extra={"fields": {
        "db": str(db_path), "from": str(backup_path),
        "safety": str(safety) if safety else ""}})
    return db_path


def ensure_daily(db_path: str | Path, *, keep: int = DEFAULT_KEEP,
                 now: int | None = None,
                 backups_dir: str | Path | None = None) -> Path | None:
    """当天还没有备份就补一份；有了返回 None。供 serve 启动时调用。"""
    db_path = Path(db_path).expanduser()
    if not db_path.exists():
        return None
    outdir = (Path(backups_dir).expanduser() if backups_dir is not None
              else backups_dir_for(db_path))
    today = _stamp(now)[:8]
    if any(p.name.startswith(f"{BACKUP_PREFIX}{today}") for p in list_backups(outdir)):
        return None
    return backup(db_path, keep=keep, now=now, backups_dir=outdir)
