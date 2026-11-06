"""DSH 插件内核（M29）：给 DSH 插件用的最小 HTTP 接口。

与代理形态（`hippo serve`）共用同一套 core，但不走 OpenAI 协议：
插件只发三种请求：

    POST /event     {"session", "role", "text"}   写一轮（user / assistant）
    POST /assemble  {"session", "text"}           返回本轮记忆文本
    GET  /health                                   就绪探针

自动抽取：写满 `every_n_turns` 轮后后台抽一次（失败只记日志，不阻塞对话）。

为什么单独写一个内核而不是复用 proxy：
代理入口是 OpenAI 协议，DSH 插件不经过 HTTP 客户端，它直接拿到宿主事件。
把「事件 → 写库」和「组装 → 注入」拆成两个小接口，插件壳只做搬运。

启动（插件壳负责，用户不需要手跑）：

    python -m hippo.dsh_kernel --db ~/.hippo/mem.db --port 8766 --api-key sk-...
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from . import __version__
from .config import Config, load_config
from .core.budget import check as budget_check
from .core.encode import Encoder
from .core.extractor import Extractor
from .core.pack import pack
from .core.store import Store
from .log import get_logger, setup

log = get_logger("dsh_kernel")


class EventBody(BaseModel):
    session: str
    role: str = "user"
    text: str


class AssembleBody(BaseModel):
    session: str
    text: str = ""


def _open_store(db: Path, cfg: Config) -> Store:
    return Store.open(db, cfg)


def create_kernel(cfg: Config, api_key: str = "") -> FastAPI:
    """构造内核 app。cfg.db_path 必须是绝对路径。"""
    db = Path(cfg.db_path).expanduser()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.parent.mkdir(parents=True, exist_ok=True)
        setup(log_dir=db.parent / "logs")
        with _open_store(db, cfg):  # 首次启动跑迁移；已存在则幂等
            pass
        app.state.extract_tasks = set()
        app.state.api_key = api_key
        log.info("kernel.ready", extra={"fields": {"db": str(db), "version": __version__}})
        yield
        for task in list(app.state.extract_tasks):
            task.cancel()

    app = FastAPI(title="hippo-dsh-kernel", lifespan=lifespan)
    app.state.db = db
    app.state.cfg = cfg

    def _store() -> Store:
        return _open_store(db, cfg)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": __version__, "db": str(db), "extract": bool(api_key)}

    @app.post("/event")
    async def event(body: EventBody) -> dict:
        if not body.text.strip():
            return {"ok": True, "skipped": "empty"}
        store = _store()
        try:
            store.add_turns(body.session, [(body.role, body.text, 0)])
        finally:
            store.close()
        _maybe_extract(app, body.session)
        return {"ok": True}

    @app.post("/assemble")
    async def assemble(body: AssembleBody) -> dict:
        store = _store()
        try:
            degrade = False
            try:
                degrade = budget_check(store, cfg).degraded
            except Exception as exc:  # noqa: BLE001
                log.warning("kernel.budget.failed", extra={"fields": {"err": repr(exc)}})
            pres = pack(store, body.session, body.text, cfg=cfg, degrade=degrade)
            return {
                "ok": True,
                "text": pres.text,
                "tokens": pres.total_tokens,
                "degraded": pres.degraded,
                "trigger": pres.trigger.reason if pres.trigger else "",
            }
        except Exception as exc:  # noqa: BLE001
            # 组装失败 = 这一轮没有记忆，绝不能影响对话
            log.warning("kernel.assemble.failed", extra={"fields": {"err": repr(exc)}})
            return {"ok": False, "text": "", "tokens": 0, "error": repr(exc)}
        finally:
            store.close()

    @app.post("/extract")
    async def extract(session: str = "", force: bool = False) -> dict:
        task = _start_extract(app, session, force=force)
        return {"ok": True, "started": bool(task)}

    def _maybe_extract(app: FastAPI, session: str) -> None:
        """攒够轮数才抽；同一时间只跑一个抽取任务。"""
        if not app.state.api_key:
            return
        if any(not t.done() for t in app.state.extract_tasks):
            return
        try:
            store = _store()
            try:
                pending = store.pending_turn_count(session)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("kernel.pending.failed", extra={"fields": {"err": repr(exc)}})
            return
        if pending < cfg.extract.every_n_turns:
            return
        _start_extract(app, session, force=False)

    def _start_extract(app: FastAPI, session: str, *, force: bool) -> asyncio.Task | None:
        async def run() -> None:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=cfg.extract.timeout,
                                      write=30.0, pool=10.0),
            ) as client:
                store = _store()
                try:
                    ex = Extractor(store, cfg, Encoder(cfg, client, app.state.api_key))
                    sessions = [session] if session else store.list_sessions()
                    for s in sessions:
                        rep = await ex.run(s, force=force)
                        log.info("kernel.extract.done", extra={"fields": {
                            "session": s, "windows": len(rep.results),
                            "events": rep.events, "cost": rep.cost}})
                except Exception as exc:  # noqa: BLE001
                    log.warning("kernel.extract.failed", extra={"fields": {"err": repr(exc)}})
                finally:
                    store.close()

        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            return None
        app.state.extract_tasks.add(task)
        task.add_done_callback(lambda t: app.state.extract_tasks.discard(t))
        return task

    return app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hippo.dsh_kernel",
                                description="DSH 插件内核（本地 HTTP）")
    p.add_argument("--db", default="", help="记忆库路径（默认 ~/.hippo/mem.db）")
    p.add_argument("--config", default="", help="config.yaml 路径（默认跟着 db 目录）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--api-key", default="", help="抽取用 DeepSeek key（空=不抽取）")
    p.add_argument("--parent-pid", type=int, default=0,
                   help="宿主进程退出时内核跟着退（防孤儿）")
    a = p.parse_args(argv)

    cfg = load_config(a.config or None) if a.config else load_config()
    if a.db:
        cfg.db_path = str(Path(a.db).expanduser())
    elif not Path(cfg.db_path).expanduser().is_absolute():
        cfg.db_path = str(Path.home() / ".hippo" / "mem.db")

    if a.parent_pid:
        def _watch_parent() -> None:
            import time

            while True:
                try:
                    os.kill(a.parent_pid, 0)
                except OSError:
                    sys.exit(0)
                time.sleep(5)

        import threading

        threading.Thread(target=_watch_parent, daemon=True).start()

    import uvicorn

    uvicorn.run(create_kernel(cfg, api_key=a.api_key), host=a.host, port=a.port,
                log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
