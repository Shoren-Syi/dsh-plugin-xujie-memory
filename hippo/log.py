"""结构化日志（M0.3.1）。

一条日志 = 一行 JSON。理由很简单：出问题时你要的是
`jq 'select(.session=="a1" and .stage=="proxy")'` 这种查询，
而不是在一堆多行堆栈里翻。

三条约定：
  1. **contextvar 带上下文**：session / req_id / stage 由 `bind()` 注入，
     调用点不传，避免每个 log 语句都重复一长串参数。
  2. **按天切文件**，保留 7 天。日志是排障资料，不是数据资产，不该无限增长。
  3. **测试不起文件**：`HIPPO_LOG_DIR` 指向 tmp 或 `to_stderr=True` 时只走 stderr。

用法：
    from hippo.log import get_logger, bind

    log = get_logger("proxy")
    with bind(session="a1", req_id="r-7f3a"):
        log.info("request.received", extra={"fields": {"model": "deepseek-chat"}})
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

# 上下文里只放"每条日志都该带上"的短字段。别往里塞大对象。
_ctx: ContextVar[dict[str, Any]] = ContextVar("hippo_log_ctx", default={})

# logging 自带的属性，不算 extra
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime", "fields",
}

_configured = False


class JsonFormatter(logging.Formatter):
    """固定字段顺序，方便人眼扫，也方便 diff。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(_ctx.get())

        fields = getattr(record, "fields", None)
        if fields:
            payload.update(fields)

        # 调用点随手写的 extra={...}（非 fields）也带上，别丢
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                payload.setdefault(k, v)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class DailyFileHandler(logging.Handler):
    """按天切文件，文件名 `hippo-YYYYMMDD.jsonl`，顺手清理过期文件。

    没用 TimedRotatingFileHandler：它的 namer/suffix 只作用于**归档**文件，
    当前文件永远叫 hippo.log，跟手册约定的名字对不上；而且跨天要靠定时器。
    自己写一个 30 行的，行为可预测得多。
    """

    def __init__(self, directory: Path, keep_days: int = 7):
        super().__init__()
        self.dir = directory
        self.keep_days = keep_days
        self._day = ""
        self._fh = None

    def _open(self, day: str) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.dir / f"hippo-{day}.jsonl", "a", encoding="utf-8")
        self._day = day
        self._cleanup(day)

    def _cleanup(self, today: str) -> None:
        try:
            cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - self.keep_days * 86400))
            for f in self.dir.glob("hippo-*.jsonl"):
                day = f.stem.split("-")[-1]
                if day.isdigit() and day < cutoff:
                    f.unlink(missing_ok=True)
        except OSError:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = time.strftime("%Y%m%d", time.localtime(record.created))
            if day != self._day:
                self._open(day)
            self._fh.write(self.format(record) + "\n")
            self._fh.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
        super().close()


def current_log_file(log_dir: str | Path | None = None) -> Path:
    """当天日志文件路径。测试和 `tail -f` 都用得上。"""
    d = Path(log_dir or os.environ.get("HIPPO_LOG_DIR") or (Path.home() / ".hippo" / "logs"))
    return d / f"hippo-{time.strftime('%Y%m%d')}.jsonl"


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """在当前上下文叠加字段，退出时精确还原（不是清空）。"""
    token = _ctx.set({**_ctx.get(), **fields})
    try:
        yield
    finally:
        _ctx.reset(token)


def setup(
    level: str | int = "INFO",
    *,
    log_dir: str | Path | None = None,
    to_stderr: bool = True,
    keep_days: int = 7,
) -> None:
    """初始化根 logger。重复调用是安全的（幂等）。"""
    global _configured
    root = logging.getLogger("hippo")
    if _configured:
        return

    root.setLevel(level if isinstance(level, int) else getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    # HIPPO_LOG=off 时只留一个黑洞 handler。压测和基准测试用得上：
    # 每条日志都要 flush，几百条下来会污染延迟测量。
    if os.environ.get("HIPPO_LOG", "").lower() in ("off", "0", "false", "silent"):
        root.addHandler(logging.NullHandler())
        _configured = True
        return

    fmt = JsonFormatter()

    if to_stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    target = log_dir or os.environ.get("HIPPO_LOG_DIR") or (Path.home() / ".hippo" / "logs")
    target = Path(target)
    try:
        target.mkdir(parents=True, exist_ok=True)
        fh = DailyFileHandler(target, keep_days=keep_days)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        # 只读文件系统（容器/沙箱）不该让程序起不来
        root.warning("log.file.disabled", extra={"fields": {"dir": str(target)}})

    _configured = True


def get_logger(name: str) -> logging.Logger:
    if not _configured:
        setup()
    return logging.getLogger(f"hippo.{name}" if not name.startswith("hippo") else name)


def reset() -> None:
    """仅供测试：拆掉所有 handler，让下次 setup 重新来过。"""
    global _configured
    root = logging.getLogger("hippo")
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    _configured = False
