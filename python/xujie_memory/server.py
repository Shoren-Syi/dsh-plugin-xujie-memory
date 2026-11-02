"""把 xujie_memory 暴露成一个本地 HTTP 服务，供外部宿主（如 DeepSeek Harness）调用。

## 为什么要有这一层

内核是 Python，宿主插件是 JS。两种语言之间只有三种连接方式：

    MCP        外部进程、只有 tools、默认不启用 → 模型得主动查，丢掉「无感」
    FFI/子进程  每次调用起一个进程，启动开销压在每一轮上
    本地 HTTP   一次进程常驻，每轮一次往返（亚毫秒）

选第三种。**它是唯一不改变内核、也不牺牲「无感」的路。**

## 接口就三个

    GET  /health     宿主用它判断服务在不在
    POST /turn       用户说了一句话 → 写记忆 + 给回材料（宿主每轮调一次）
    POST /context    只读材料，不写（宿主想在别处注入时用）

`/turn` 一次调用做完「写」和「读」，因为宿主的两个时机（消息进来、组装上下文）
之间没有别的活要干。拆成两次只会多一次往返。

## 会话怎么映射

宿主的一个会话 = 一个 scope。scope 由宿主给的 session 标识决定：

    tenant  dsh
    agent   <session_id>
    world   default

**会话之间不共享记忆**。这是刻意的：DSH 里「会话」是用户心里的边界，
跨会话串味比记不住更让人不安。

## 关于「待确认」

宿主形态下没有「用户点确认」这个交互——模型不会停下来等一句话。
所以默认 `auto_confirm=False`，未定的解释会作为「待确认」写进材料，
由主模型自己判断该不该问。这是这条路上必须做的降级，
不能靠"悄悄替他确认"糊过去（那等于把 assisted 升成 auto）。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .encoder import RuleEncoder
from .engine import Engine, SpecRegistry
from .memory import Memory
from .management import Management, ManagementError
from .orchestrator import Orchestrator, TurnBudget
from .profile import ProfileStore

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TENANT = "dsh"
DEFAULT_WORLD = "default"

# v0.52 输入上限。旧版不设限：一条 8 万字的消息处理了 7 秒（超过宿主超时），
# 整段原文还被存成了「偏好」。
MAX_BODY_BYTES = 256 * 1024
MAX_TEXT_CHARS = 2000
MAX_ID_CHARS = 200

# 记忆范围：
#   user     画像/事实/锚点按用户共享，跨会话有效（默认，长期记忆的本意）
#   session  每个会话一份，互不相通（旧版行为）
SCOPE_MODES = ("user", "session")


class BadRequest(ValueError):
    """参数不合法 → 400，而不是 500。"""


class MemoryService:
    """一个进程、一个库、按会话分 scope。"""

    def __init__(
        self,
        db_path: str | Path,
        specs_dir: str | Path,
        *,
        auto_confirm: bool = False,
        encoder=None,
        scope_mode: str = "user",
    ):
        if scope_mode not in SCOPE_MODES:
            raise ValueError(f"scope_mode 必须是 {SCOPE_MODES} 之一")
        self.scope_mode = scope_mode
        self.db_path = str(db_path)
        self.engine = Engine(Path(db_path), SpecRegistry.from_dir(Path(specs_dir)))
        self.profiles = ProfileStore(self.engine)
        from .privacy import repair_legacy
        repair_legacy(self.engine, self.profiles)
        self.orchestrator = Orchestrator(
            self.engine,
            profiles=self.profiles,
            encoder=encoder or RuleEncoder(),
            auto_confirm=auto_confirm,
        )
        # scope 解析要加锁：thread-local 连接保证 SQL 安全，
        # 但「同一个 session 两次请求同时建 scope」会各建一次。
        # `INSERT OR IGNORE` 兜住了，这里只是避免无谓的重复计算。
        self._scopes: dict[str, str] = {}
        self._lock = threading.Lock()
        # Serialize HTTP writes with reads of dependent projections. In
        # particular, a /turn cannot republish a half-edited graph.
        self.request_lock = threading.RLock()

    # ------------------------------------------------------------ scope
    def scope_of(self, session: str, user: str = "user") -> str:
        key = f"{session}\x00{user}"
        with self._lock:
            cached = self._scopes.get(key)
            if cached:
                return cached
            # v0.52：默认按用户分 scope，记忆跨会话有效。
            # 旧版按会话分，新开一个会话就是「还不了解这个人」。
            agent = f"user:{user}" if self.scope_mode == "user" else session
            scope = self.engine.ensure_scope(DEFAULT_TENANT, agent, DEFAULT_WORLD)
            self._scopes[key] = scope
            return scope

    # ------------------------------------------------------------ 主入口
    def turn(
        self,
        session: str,
        text: str,
        *,
        user: str = "user",
        context: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """用户说了一句话：写记忆，然后给回这一轮要用的材料。

        返回的 `context` 就是宿主该注入的那段文字。
        """
        scope = self.scope_of(session, user)
        self.profiles.ensure(scope, user)

        turn_budget = _budget_from(budget)
        result = self.orchestrator.handle(
            scope, user, text, context=dict(context or {}), budget=turn_budget,
            session=session,
        )
        return {
            "ok": True,
            "session": session,
            "scope": scope,
            "context": result.context,
            "outcomes": [
                {
                    "relation": o.interpretation.relation,
                    "decision": o.decision.outcome,
                    "reasons": list(o.decision.reasons),
                }
                for o in result.outcomes
            ],
            "clarifications": [
                {"request_id": c["request_id"], "question": c.get("question", "")}
                for c in result.clarifications
            ],
            "anchored": [t["content"] for t in result.anchored],
            "recalled": [t["content"] for t in result.recalled],
            "warnings": list(getattr(result, "warnings", []) or []),
        }

    def context(
        self,
        session: str,
        text: str = "",
        *,
        user: str = "user",
        budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """只读：不写记忆，只给材料。宿主想在别处注入时用。"""
        scope = self.scope_of(session, user)
        turn_budget = _budget_from(budget)
        composed = self.orchestrator.compose_context(
            scope, user, text, budget=turn_budget, session=session,
        )
        return {"ok": True, "session": session, "scope": scope,
                "context": composed}

    # ------------------------------------------------------------ 三层记忆
    def memory_layers(self, session: str, *, user: str = "user") -> dict[str, Any]:
        scope = self.scope_of(session, user)
        return {"ok": True, "session": session,
                **Memory(self.engine).layers(scope)}

    def memory_search(self, session: str, query: str, *, limit: int = 5,
                      user: str = "user") -> dict[str, Any]:
        scope = self.scope_of(session, user)
        return {"ok": True, "session": session, "query": query,
                "hits": Memory(self.engine).search(scope, query, limit=limit)}

    def memory_mark(self, session: str, trace_id: str, importance: float = 4.0,
                    note: str = "", *, user: str = "user") -> dict[str, Any]:
        scope = self.scope_of(session, user)
        return {"ok": True, "session": session,
                **Memory(self.engine).mark(scope, trace_id, importance, note)}

    def memory_unmark(self, session: str, trace_id: str, *,
                      user: str = "user") -> dict[str, Any]:
        scope = self.scope_of(session, user)
        return {"ok": True, "session": session,
                **Memory(self.engine).unmark(scope, trace_id)}

    def memory_curve(self, session: str, trace_id: str, points: int = 12, *,
                     user: str = "user") -> dict[str, Any]:
        scope = self.scope_of(session, user)
        return {"ok": True, "session": session,
                "points": Memory(self.engine).curve(scope, trace_id, points)}

    def memory_forget(self, session: str, trace_id: str, *,
                      user: str = "user") -> dict[str, Any]:
        """删一条记忆。主 AI 主动删时用；用户说"忘掉这条"也走这里。"""
        scope = self.scope_of(session, user)
        return {"ok": True, "session": session,
                **Memory(self.engine).forget(scope, trace_id)}

    def memory_stats(self, session: str, *, user: str = "user") -> dict[str, Any]:
        """库里有多少条记忆、多少条被删过、多少条锚住——透明化用。"""
        scope = self.scope_of(session, user)
        return {"ok": True, "session": session,
                **Memory(self.engine).stats(scope)}

    def close(self) -> None:
        self.engine.close()


def _budget_from(raw: dict[str, Any] | None) -> TurnBudget:
    if not raw:
        return TurnBudget()
    if not isinstance(raw, dict):
        raise BadRequest("budget 必须是对象")
    defaults = TurnBudget()
    allowed: dict[str, Any] = {}
    for name in TurnBudget.__dataclass_fields__:
        if name not in raw:
            continue
        value = raw[name]
        want = type(getattr(defaults, name))
        if want is bool:
            if not isinstance(value, bool):
                raise BadRequest(f"budget.{name} 必须是布尔值")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise BadRequest(f"budget.{name} 必须是数字")
            value = max(0, min(int(value), 100000))
        allowed[name] = value
    return TurnBudget(**allowed)


def _str_field(body: dict[str, Any], name: str, *, default: str = "",
               limit: int = MAX_ID_CHARS) -> str:
    value = body.get(name, default)
    if value is None:
        value = default
    if isinstance(value, (dict, list)):
        raise BadRequest(f"{name} 必须是字符串")
    return str(value).strip()[:limit]


def _num_field(body: dict[str, Any], name: str, default: float,
               lo: float, hi: float) -> float:
    value = body.get(name, default)
    if value is None:
        return default
    if isinstance(value, bool):
        raise BadRequest(f"{name} 必须是数字")
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"{name} 必须是数字") from exc
    return max(lo, min(hi, value))


# ---------------------------------------------------------------- HTTP


class _Handler(BaseHTTPRequestHandler):
    service: MemoryService
    token: str = ""
    server_version = f"xujie-memory/{__version__}"
    protocol_version = "HTTP/1.1"

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.service.engine.close_thread_connection()

    # 单行日志太吵：DSH 每轮都会打一条，讲的东西信息量比 print 还少。
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        return

    # ---------------------------------------------------------- 鉴权
    def _authorized(self) -> bool:
        """v0.52：本机任何进程（包括浏览器里的网页）都能打 127.0.0.1。
        旧版不校验，网页用 text/plain 发个 POST 就能往记忆里写东西——
        那等于往主模型的提示词里注入内容。现在要求 token。"""
        if not self.token:
            return True
        got = self.headers.get("X-Xujie-Token") or ""
        return hmac.compare_digest(got.encode(), self.token.encode())

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] == "/health":
            # health 不带敏感信息，但带上版本和库路径：
            # 插件靠它判断端口上跑的是不是**自己这一版**内核。
            payload = {"ok": True, "version": __version__,
                       "pid": os.getpid(),
                       "scope_mode": self.service.scope_mode,
                       "auth": bool(self.token)}
            if self._authorized():
                payload["db"] = self.service.db_path
            self._json(200, payload)
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        # 不给 CORS 头：浏览器的跨域预检一律失败。
        self._json(405, {"ok": False, "error": "method_not_allowed"})

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?")[0]
        # 没读请求体就拒绝时必须断开连接：HTTP/1.1 keep-alive 下，
        # 没读走的请求体会被当成下一个请求解析，同一连接上的下一次调用
        # 就会收到一页 HTML 错误（测试里抓到的）。
        if not self._authorized():
            self.close_connection = True
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self.close_connection = True
            self._json(415, {"ok": False, "error": "content_type_must_be_json"})
            return
        try:
            body = self._read_json()
        except BadRequest as exc:
            self.close_connection = True
            self._json(413 if "too_large" in str(exc) else 400,
                       {"ok": False, "error": str(exc)})
            return
        except ValueError as exc:
            self._json(400, {"ok": False, "error": f"bad_json: {exc}"})
            return

        self.service.request_lock.acquire()
        try:
            if route.startswith("/manage/"):
                payload = self._manage(route, body)
                self._json(200, payload)
                return
            session = _str_field(body, "session")
            if not session:
                self._json(400, {"ok": False, "error": "missing_session"})
                return
            user = _str_field(body, "user", default="user") or "user"

            if route == "/turn":
                text = _str_field(body, "text", limit=MAX_TEXT_CHARS * 4)
                if not text:
                    self._json(400, {"ok": False, "error": "missing_text"})
                    return
                truncated = len(text) > MAX_TEXT_CHARS
                text = text[:MAX_TEXT_CHARS]
                ctx = body.get("context")
                if ctx is not None and not isinstance(ctx, dict):
                    raise BadRequest("context 必须是对象")
                payload = self.service.turn(
                    session, text, user=user, context=ctx,
                    budget=body.get("budget"),
                )
                if truncated:
                    payload.setdefault("warnings", []).append(
                        f"text 超过 {MAX_TEXT_CHARS} 字，只记了前 {MAX_TEXT_CHARS} 字")
            elif route == "/context":
                payload = self.service.context(
                    session, _str_field(body, "text", limit=MAX_TEXT_CHARS),
                    user=user, budget=body.get("budget"),
                )
            elif route == "/memory/layers":
                payload = self.service.memory_layers(session, user=user)
            elif route == "/memory/search":
                payload = self.service.memory_search(
                    session, _str_field(body, "query", limit=MAX_TEXT_CHARS),
                    user=user,
                    limit=int(_num_field(body, "limit", 5, 1, 50)),
                )
            elif route == "/memory/stats":
                payload = self.service.memory_stats(session, user=user)
            elif route == "/memory/forget":
                trace_id = _str_field(body, "trace_id")
                if not trace_id:
                    raise BadRequest("missing_trace_id")
                try:
                    payload = self.service.memory_forget(session, trace_id, user=user)
                except ValueError as exc:
                    # 库里没有这条 / 不属于这个 scope：调用方参数问题，不是服务端崩了。
                    raise BadRequest(str(exc)) from exc
            elif route == "/memory/edit":
                payload = Management(self.service).edit(
                    self.service.scope_of(session, user),
                    _str_field(body, "trace_id"),
                    _str_field(body, "content", limit=MAX_TEXT_CHARS),
                    _str_field(body, "expected_hash"),
                )
            elif route in ("/memory/mark", "/memory/unmark", "/memory/curve"):
                trace_id = _str_field(body, "trace_id")
                if not trace_id:
                    raise BadRequest("missing_trace_id")
                if route == "/memory/mark":
                    payload = self.service.memory_mark(
                        session, trace_id, user=user,
                        importance=_num_field(body, "importance", 4.0, 1.0, 5.0),
                        note=_str_field(body, "note", limit=500),
                    )
                elif route == "/memory/unmark":
                    payload = self.service.memory_unmark(session, trace_id, user=user)
                else:
                    payload = self.service.memory_curve(
                        session, trace_id, user=user,
                        points=int(_num_field(body, "points", 12, 2, 200)),
                    )
            else:
                self._json(404, {"ok": False, "error": "not_found"})
                return
        except ManagementError as exc:
            status = 409 if exc.code == "version_conflict" else (
                404 if exc.code.endswith("_not_found") else 400
            )
            self._json(status, {"ok": False, "error": exc.code})
            return
        except BadRequest as exc:
            self._json(400, {"ok": False, "error": "bad_request", "detail": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            # 宿主那边只看到"这一轮没有记忆"，看不到堆栈。
            # 所以错误必须回传，不能吞——静默失败比没装插件更糟。
            import sys
            import traceback
            traceback.print_exc(file=sys.stderr)
            self._json(500, {"ok": False, "error": type(exc).__name__,
                             "detail": str(exc)[:300]})
            return
        finally:
            self.service.request_lock.release()

        self._json(200, payload)

    def _manage(self, route: str, body: dict[str, Any]) -> dict[str, Any]:
        manager = Management(self.service)
        if route == "/manage/catalog":
            return manager.catalog()
        scope = _str_field(body, "scope_id")
        if not scope:
            raise BadRequest("missing_scope_id")
        if route == "/manage/snapshot":
            return manager.snapshot(scope, int(_num_field(body, "limit", 250, 1, 500)))
        if route == "/manage/search":
            return manager.search(
                scope, _str_field(body, "query", limit=MAX_TEXT_CHARS),
                int(_num_field(body, "limit", 30, 1, 50)),
            )
        if route == "/manage/action":
            return manager.action(
                scope, _str_field(body, "action"), _str_field(body, "account_id"),
                _str_field(body, "target_id"), _str_field(body, "value", limit=MAX_TEXT_CHARS),
                _str_field(body, "kind"), _str_field(body, "expected", limit=MAX_TEXT_CHARS),
            )
        trace = _str_field(body, "trace_id")
        if route == "/manage/detail":
            return manager.detail(scope, trace)
        expected = _str_field(body, "expected_hash")
        if route == "/manage/edit":
            return manager.edit(
                scope, trace, _str_field(body, "content", limit=MAX_TEXT_CHARS), expected
            )
        if route == "/manage/forget":
            return manager.forget(scope, trace, expected)
        raise ManagementError("route_not_found")

    # ---------------------------------------------------------- 工具
    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise BadRequest("bad_content_length") from exc
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            # 读掉也不读：直接拒绝并关闭连接。
            self.close_connection = True
            raise BadRequest("body_too_large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(str(exc)) from exc
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(blob)


def load_token(token_file: str | Path | None) -> str:
    """读（或第一次生成）共享 token。插件和内核读同一个文件。"""
    if not token_file:
        return ""
    path = Path(token_file)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(24)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(token, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return token


def serve(
    db_path: str | Path,
    specs_dir: str | Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    auto_confirm: bool = False,
    token: str = "",
    scope_mode: str = "user",
) -> ThreadingHTTPServer:
    """起服务。返回 server 对象，调用方负责 `serve_forever()`/`shutdown()`。"""
    service = MemoryService(db_path, specs_dir, auto_confirm=auto_confirm,
                            scope_mode=scope_mode)
    handler = type("_BoundHandler", (_Handler,), {"service": service,
                                                  "token": token or ""})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.service = service            # 方便调用方 close()
    return httpd


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="xujie-memory-server",
        description="把记忆内核暴露成本地 HTTP 服务（供 DSH 等宿主调用）",
    )
    here = Path(__file__).resolve().parent.parent
    parser.add_argument("--db", default=str(Path.home() / ".xujie-memory" / "memory.db"))
    parser.add_argument("--specs", default=str(here / "specs"))
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--auto-confirm", action="store_true",
                        help="自动确认澄清（演示/压测用，生产不要开）")
    parser.add_argument("--token-file", default="",
                        help="共享 token 文件；给了就要求请求头 X-Xujie-Token")
    parser.add_argument("--scope", default="user", choices=SCOPE_MODES,
                        help="记忆范围：user=跨会话（默认），session=每个会话一份")
    parser.add_argument("--parent-pid", type=int, default=0,
                        help="宿主进程 pid；宿主没了内核自己退出，不留孤儿进程")
    args = parser.parse_args(argv)

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    token = load_token(args.token_file) if args.token_file else \
        os.environ.get("XUJIE_MEMORY_TOKEN", "")
    httpd = serve(args.db, args.specs, host=args.host, port=args.port,
                  auto_confirm=args.auto_confirm, token=token,
                  scope_mode=args.scope)
    if args.parent_pid:
        _watch_parent(args.parent_pid, httpd)
    print(f"xujie-memory {__version__} listening on "
          f"http://{args.host}:{args.port}  db={args.db}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.service.close()
    return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _watch_parent(pid: int, httpd: ThreadingHTTPServer) -> None:
    """宿主崩溃时 child.kill() 不会被调用；没有这一步，旧版内核会一直占着端口，
    升级插件后新版连上的还是旧内核。"""
    import time

    def loop() -> None:
        while True:
            time.sleep(3)
            if not _pid_alive(pid):
                httpd.shutdown()
                return

    threading.Thread(target=loop, name="parent-watch", daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
