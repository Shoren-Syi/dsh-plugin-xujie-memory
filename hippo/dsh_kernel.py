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
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
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

_BOARD_HTML = Path(__file__).resolve().parent / "data" / "board.html"


class EventBody(BaseModel):
    session: str
    role: str = "user"
    text: str


class AssembleBody(BaseModel):
    session: str
    text: str = ""


def _open_store(db: Path, cfg: Config) -> Store:
    return Store.open(db, cfg)


def _watch_parent(parent_pid: int) -> None:
    """父进程退出时结束内核；Windows 不能用 os.kill(pid, 0) 探测。"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
        if not handle:
            if ctypes.get_last_error() == 87:  # 进程不存在
                os._exit(0)
            log.warning("kernel.parent.watch_failed")
            return
        try:
            result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            kernel32.CloseHandle(handle)
        if result == 0:  # WAIT_OBJECT_0
            os._exit(0)
        log.warning("kernel.parent.watch_failed")
        return

    import time
    while True:
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            os._exit(0)
        except PermissionError:
            pass
        time.sleep(5)


def create_kernel(cfg: Config, api_key: str = "", instance_id: str = "") -> FastAPI:
    """构造内核 app。cfg.db_path 必须是绝对路径。"""
    db = Path(cfg.db_path).expanduser()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.parent.mkdir(parents=True, exist_ok=True)
        setup(log_dir=db.parent / "logs")
        with _open_store(db, cfg):  # 首次启动跑迁移；已存在则幂等
            pass
        app.state.extract_tasks = set()
        app.state.dirty_sessions = set()
        app.state.api_key = api_key
        log.info("kernel.ready", extra={"fields": {"db": str(db), "version": __version__}})
        yield
        for task in list(app.state.extract_tasks):
            task.cancel()

    app = FastAPI(title="hippo-dsh-kernel", lifespan=lifespan)
    @app.middleware("http")
    async def local_browser_boundary(request: Request, call_next):
        # localhost 并非认证：拒绝 DNS 重绑定和其他网站发起的跨源请求。
        # 无 Origin 的本机宿主进程仍可访问；本机其他进程不在此边界内。
        host = request.headers.get("host", "")
        try:
            parsed = urlsplit("http://" + host)
            allowed_host = (parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                            and not parsed.username and not parsed.password
                            and not parsed.path and not parsed.query and not parsed.fragment)
            _ = parsed.port  # 拒绝非法端口
        except ValueError:
            allowed_host = False
        origin = request.headers.get("origin")
        if not allowed_host or (origin is not None and origin.lower() != f"http://{host}".lower()):
            return PlainTextResponse("Forbidden", status_code=403)
        return await call_next(request)
    app.state.db = db
    app.state.cfg = cfg

    def _store() -> Store:
        return _open_store(db, cfg)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": __version__, "db": str(db), "extract": bool(api_key), "instance": instance_id}

    # ------------------------------------------------------------ 记忆看板（M30）
    @app.get("/board/data")
    def board_data() -> dict:
        store = _store()
        try:
            snap = store.board_snapshot()
            snap["db"] = str(db)
            return {"ok": True, "extraction_configured": bool(api_key), **snap}
        except Exception as exc:  # noqa: BLE001
            log.warning("kernel.board.failed", extra={"fields": {"err": repr(exc)}})
            return {"ok": False, "error": repr(exc)}
        finally:
            store.close()

    @app.get("/board", response_class=HTMLResponse)
    def board_page() -> str:
        try:
            return _BOARD_HTML.read_text(encoding="utf-8")
        except OSError:
            return "<h1>board.html 缺失</h1>"

    @app.get("/favicon.ico")
    def favicon() -> Response:
        # 看板页面没有图标，浏览器会请求 favicon；给 204 消掉控制台 404。
        return Response(status_code=204)

    @app.post("/event")
    async def event(body: EventBody) -> dict:
        if not body.text.strip():
            return {"ok": True, "skipped": "empty"}
        store = _store()
        try:
            text = body.text
            # 重复投递幂等：DSH 重试、会话重放、用户重复粘贴都会把同一条再送一遍。
            # 只对**有实质内容的长消息**（>=8 字）做去重——"嗯""好"这种短句
            # 用户本来就会连发，不能被吞掉。
            last = store.last_turn(body.session)
            if len(text.strip()) >= 8 and last == (body.role, text):
                log.info("kernel.event.duplicate", extra={"fields": {"session": body.session}})
                return {"ok": True, "skipped": "duplicate"}
            store.add_turns(body.session, [(body.role, text, 0)])
            # 被动纠正：只从用户这句话里认"旧值→新值"，静默修正已有事实。
            # 不调模型、不改提示词、不加延迟（一次正则 + 一条库查询）。
            if body.role == "user":
                from .core.reflect import apply_corrections, parse_correction
                pairs = parse_correction(text)
                if pairs:
                    applied = apply_corrections(store, body.session, pairs,
                                                cfg=cfg, now=int(time.time()))
                    if applied:
                        log.info("kernel.correction.applied",
                                 extra={"fields": {"session": body.session,
                                                   "n": len(applied)}})
        finally:
            store.close()
        _maybe_extract(app, body.session)
        return {"ok": True}

    @app.post("/assemble")
    async def assemble(body: AssembleBody) -> dict:
        # pack 是同步的（检索 + 装配 + SQLite 读）。20 并发时如果直接跑在
        # 事件循环上，p95 会被同步点拖到 700ms+；丢线程池后并发不再互相堵。
        def _do() -> dict:
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

        return await asyncio.to_thread(_do)

    @app.post("/extract")
    async def extract(session: str = "", force: bool = False) -> dict:
        task = _start_extract(app, session, force=force)
        return {"ok": True, "started": bool(task)}

    @app.post("/drain")
    async def drain() -> dict:
        """Give already accepted background work a bounded shutdown window."""
        deadline = asyncio.get_running_loop().time() + 30
        _pump_extract(app)
        # 会话结束时补一条摘要：每个会话一次调用，成本有界。
        if app.state.api_key:
            try:
                await _refresh_digests(app)
            except Exception as exc:  # noqa: BLE001
                log.warning("kernel.digest.failed", extra={"fields": {"err": repr(exc)[:160]}})
        while app.state.extract_tasks:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.wait(app.state.extract_tasks.copy(), timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            await asyncio.sleep(0)  # run completion callbacks before inspecting the queue
        return {"ok": not bool(app.state.extract_tasks), "remaining": len(app.state.extract_tasks)}

    async def _refresh_digests(app: FastAPI) -> None:
        """给"已经有内容但还没摘要"的会话补一条。"""
        from .core.digest import build_digest, get_digest
        store = _store()
        try:
            sessions = store.list_turn_sessions()
            for session in sessions:
                if get_digest(store, session):
                    continue
                if store.pending_turn_count(session) == 0 and store.get_turns(session, limit=2):
                    continue
        finally:
            store.close()
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=cfg.extract.timeout, write=30.0, pool=10.0),
        ) as client:
            store = _store()
            try:
                for session in sessions:
                    if get_digest(store, session):
                        continue
                    await build_digest(store, session, cfg, client, app.state.api_key)
            finally:
                store.close()

    def _maybe_extract(app: FastAPI, session: str) -> None:
        """Remember work arriving during an active extraction, including other sessions."""
        if not app.state.api_key:
            return
        app.state.dirty_sessions.add(session)
        _pump_extract(app)

    def _pump_extract(app: FastAPI) -> None:
        if not app.state.api_key or app.state.extract_tasks:
            return
        while app.state.dirty_sessions:
            session = app.state.dirty_sessions.pop()
            try:
                store = _store()
                try:
                    pending = store.pending_turns(session)
                    first = store.extracted_turn_count(session) == 0
                finally:
                    store.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("kernel.pending.failed", extra={"fields": {"err": repr(exc)}})
                continue
            from .core.extractor import extract_threshold
            need = extract_threshold(cfg, first=first)
            if (not pending or pending[-1].role != "assistant"
                    or sum(t.role == "user" for t in pending) < need):
                continue
            _start_extract(app, session, force=False)
            return

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
        task.add_done_callback(lambda _t: _pump_extract(app))
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
        import threading

        threading.Thread(target=_watch_parent, args=(a.parent_pid,), daemon=True).start()

    import uvicorn

    uvicorn.run(create_kernel(cfg, api_key=a.api_key or os.environ.get("HIPPO_EXTRACT_API_KEY", ""),
                              instance_id=os.environ.get("HIPPO_KERNEL_INSTANCE", "")), host=a.host, port=a.port,
                log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
