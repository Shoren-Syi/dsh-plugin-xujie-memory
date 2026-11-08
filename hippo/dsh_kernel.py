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
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from typing import Literal
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
    # 只收这两种：其它角色（system/tool/随便乱传）进来只会污染记忆，
    # 而且它们本来就不是"人说过的内容"。
    role: Literal["user", "assistant"] = "user"
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
        # 摘要后台补写：DSH 很少走 /drain，只靠它等于永远不生成。
        app.state.digest_task = asyncio.create_task(_digest_loop(app))
        yield
        app.state.digest_task.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.digest_task
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

    @app.get("/stats")
    def stats() -> dict:
        """面板"状态"页用：抽取实况 + 队列 + 最近一次结果。"""
        store = _store()
        try:
            data = store.stats_summary()
        finally:
            store.close()
        data.update({
            "ok": True,
            "version": __version__,
            "db": str(db),
            "extract_configured": bool(api_key),
            "digest_enabled": bool(getattr(cfg.digest, "enabled", True)),
            "extract_model": str(getattr(cfg.deepseek, "extract_model", "")),
            "retry_empty": bool(getattr(cfg.extract, "retry_empty", False)),
        })
        return data

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

    # ------------------------------------------------------------ QQ 桥接（0.8 实验分支移植）
    #
    # 设置与预算放服务端：Node 侧每 5 秒轮询 revision，变了就重建桥接。
    # 这样"改配置"不用重启 DSH，也不用把 price/token 逻辑复制到 JS 里。
    async def _qq_body(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            raise ValueError("请求体必须是 JSON")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是对象")
        return data

    @app.get("/qq/settings/active")
    def qq_settings_active() -> dict:
        from dataclasses import asdict as _asdict

        from .core import qq
        store = _store()
        try:
            settings = qq.active_settings(store.conn)
            return {"ok": True, "settings": _asdict(settings) if settings else None}
        finally:
            store.close()

    @app.post("/qq/settings")
    async def qq_settings_set(request: Request) -> Response:
        from .core import qq
        try:
            data = await _qq_body(request)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        store = _store()
        try:
            settings = qq.set_settings(store.conn, data, now=int(time.time()))
            return JSONResponse({"ok": True, "revision": settings.revision})
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        finally:
            store.close()

    @app.delete("/qq/settings")
    def qq_settings_clear() -> dict:
        from .core import qq
        store = _store()
        try:
            qq.clear_settings(store.conn)
            return {"ok": True}
        finally:
            store.close()

    @app.post("/qq/budget/configure")
    async def qq_budget_configure(request: Request) -> Response:
        from .core import qq
        try:
            data = await _qq_body(request)
            policy = qq.BudgetPolicy(
                per_request=int(data["per_request"]), daily=int(data["daily"]),
                monthly=int(data["monthly"]), input_price=int(data["input_price"]),
                output_price=int(data["output_price"]), daily_calls=int(data["daily_calls"]),
                cache_price=None if data.get("cache_price") is None else int(data["cache_price"]),
            )
            store = _store()
            try:
                qq.configure_budget(store.conn, str(data.get("scope_id") or ""), policy,
                                    data.get("activity_limits") or {})
            finally:
                store.close()
            return JSONResponse({"ok": True})
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/qq/budget/reserve")
    async def qq_budget_reserve(request: Request) -> Response:
        from .core import qq
        try:
            data = await _qq_body(request)
            store = _store()
            try:
                call_id = qq.reserve(store.conn, str(data.get("scope_id") or ""),
                                     str(data.get("activity") or ""),
                                     int(data["input_limit"]), int(data["output_limit"]))
            finally:
                store.close()
            return JSONResponse({"ok": True, "call_id": call_id})
        except qq.BudgetDenied as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=429)
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/qq/budget/settle")
    async def qq_budget_settle(request: Request) -> Response:
        from .core import qq
        try:
            data = await _qq_body(request)
            store = _store()
            try:
                amount = qq.settle(store.conn, str(data.get("call_id") or ""),
                                   int(data["input_tokens"]), int(data["output_tokens"]),
                                   int(data.get("cached_tokens") or 0))
            finally:
                store.close()
            return JSONResponse({"ok": True, "amount": amount})
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.get("/qq/budget/status")
    def qq_budget_status(scope: str = "") -> Response:
        from .core import qq
        if not scope:
            return JSONResponse({"ok": False, "error": "缺少 scope"}, status_code=400)
        store = _store()
        try:
            return JSONResponse({"ok": True, **qq.budget_status(store.conn, scope)})
        except qq.BudgetDenied as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        finally:
            store.close()

    async def _digest_loop(app: FastAPI) -> None:
        """每 60 秒看一眼：哪些会话该补/重写摘要了。

        成本边界：同一条会话最短 refresh_minutes 才重写一次；
        攒够 min_new_turns 才在活跃期生成，否则等它静下来（idle_minutes）。
        """
        from .core.digest import build_digest, get_digest, need_refresh, read_meta, save_meta
        while True:
            await asyncio.sleep(60)
            if not app.state.api_key or not cfg.digest.enabled:
                continue
            try:
                store = _store()
                try:
                    sessions = store.list_turn_sessions()
                    now = int(time.time())
                    todo: list[tuple[str, int]] = []
                    for session in sessions:
                        last_idx = store.last_turn_idx(session)
                        if not need_refresh(last_idx=last_idx, meta=read_meta(store, session),
                                            now=now, cfg=cfg):
                            continue
                        todo.append((session, last_idx))
                finally:
                    store.close()
                if not todo:
                    continue
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=10.0, read=cfg.extract.timeout,
                                          write=30.0, pool=10.0),
                ) as client:
                    for session, last_idx in todo:
                        store = _store()
                        try:
                            text = await build_digest(store, session, cfg, client,
                                                      app.state.api_key)
                            if text:
                                save_meta(store, session, idx=last_idx, at=int(time.time()))
                        finally:
                            store.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台任务不许把内核带崩
                log.warning("kernel.digest.loop_failed", extra={"fields": {"err": repr(exc)[:160]}})

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

    # keep-alive 必须比"最长一次阻塞"长。
    #
    # 实测：两个实例同时写同一个库时，SQLite 的 busy_timeout 是 5 秒，
    # 而 uvicorn 默认 keep_alive 也是 5 秒——请求正好卡在锁上时，服务端
    # 认为连接空闲并关掉它，客户端拿到的是 ReadError(''), 那条对话就丢了。
    # 300 次并发写丢 1 条，看起来是"偶发"，其实是两把 5 秒的刀撞在一起。
    uvicorn.run(create_kernel(cfg, api_key=a.api_key or os.environ.get("HIPPO_EXTRACT_API_KEY", ""),
                              instance_id=os.environ.get("HIPPO_KERNEL_INSTANCE", "")), host=a.host, port=a.port,
                log_level="warning", access_log=False, timeout_keep_alive=75)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
