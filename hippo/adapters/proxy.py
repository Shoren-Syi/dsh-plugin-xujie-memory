"""OpenAI 兼容代理（M2）。

目标只有一个：**让客户端改一行 base_url 就能用，且感觉不到中间多了一层。**

    client = OpenAI(base_url="http://localhost:8787/v1", api_key="sk-...")

M2 阶段这里是**纯透传**：不改 messages、不改参数、不动 model。
唯一的例外是 `stream_options.include_usage`（见 _inject_stream_options），
因为不加它就拿不到 usage，成本统计全部归零 —— 那是后面所有优化的前提。

三个不做的事，都是刻意的：
  - 不缓存响应（会破坏流式体感）
  - 不改写 prompt（M13 才接 pack，那时也只追加、不改写）
  - 不阻塞响应写库（记忆写入必须 fire-and-forget）
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import __version__
from ..config import Config, load_config
from ..core import turns as turnlog
from ..core.budget import check as budget_check
from ..core.db import connect as connect_db
from ..core.curiosity import Curiosity
from ..core.heartbeat import Heartbeat
from ..core.store import Store
from ..core.telemetry import Span, record_metric
from ..core.pack import inject, pack
from ..core.turns import pick_user_text
from ..log import bind, get_logger
from .session import identify

log = get_logger("proxy")

_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "accept-encoding", "proxy-authorization", "te", "trailer",
}


class ChatRequest(BaseModel):
    """只做校验，不做约束。

    extra="allow" 是必须的：客户端会带几十个字段（logit_bias / seed /
    response_format / 各种私有扩展），漏一个就 422，用户会以为你坏了。
    """

    model_config = ConfigDict(extra="allow")

    model: str = "deepseek-flash"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    user: str | None = None


class StreamAccumulator:
    """从 SSE 分块里拣出 assistant 说的话。

    只认 `delta.content`，不认 `reasoning_content`——
    推理过程是模型的草稿纸，不是它说的话，记进 turns 会污染后续抽取。
    """

    __slots__ = ("parts", "finish_reason", "usage", "done")

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None
        self.done = False

    def feed(self, chunk: dict[str, Any]) -> None:
        choices = chunk.get("choices") or []
        if choices:
            first = choices[0]
            delta = first.get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str):
                self.parts.append(piece)
            if first.get("finish_reason"):
                self.finish_reason = first["finish_reason"]
        usage = chunk.get("usage")
        if usage:
            self.usage = usage

    @property
    def text(self) -> str:
        return "".join(self.parts)

    @property
    def complete(self) -> bool:
        """正常收尾才算完整。靠 finish_reason 而不是"有没有收到内容"，
        因为空回复（模型只说了个句号又删掉）也是完整的一轮。"""
        return self.done or self.finish_reason in ("stop", "length", "content_filter")


class _TurnSink:
    """把一轮对话写进 turns，顺带把 metrics 也写掉。

    **一个连接写两件事**。分两次开库要多一次 fsync，实测 5.0ms vs 2.5ms——
    对"客户端关连接前必须结完账"这个场景，这 2.5ms 是实打实的。

    单独拎出来还为了一个原则：**记账失败不能影响聊天**。
    库没初始化、磁盘满、锁等待——统统只记一条 warning。
    """

    def __init__(self, db_path: Path, session: str, user_text: str,
                 known: list, pricing: dict[str, float] | None = None,
                 writer=None, on_committed=None):
        self.writer = writer
        self.db_path = db_path
        self.session = session
        self.user_text = user_text
        self.known = known
        self.pricing = pricing or {}
        self.written = False
        # 结账成功后的钩子（M5.4 用它触发抽取）。
        # 放在这里而不是调用处，是因为结账有两个入口：流式 settle 和非流式
        # _respond_unary。写两遍迟早会漏一个——上一轮"20 轮只落 19 轮"
        # 就是同一类错误。
        self.on_committed = on_committed

    def _is_duplicate(self, assistant_text: str) -> bool:
        """客户端重试会原样重发上一次请求，不能因此多记一轮。

        判定条件是「同一条 user + 同一条 assistant 已经紧挨着存在」。
        只比 content 不比时间，是因为重试通常发生在几秒内；
        隔很久说同一句话（"早"→"早"）不会被误判，因为中间隔着别的轮次。
        """
        if not self.user_text.strip():
            return False
        for i in range(len(self.known) - 1, -1, -1):
            t = self.known[i]
            if t.role != "user":
                continue
            if t.content.strip() != self.user_text.strip():
                return False
            following = [x for x in self.known[i + 1:] if x.role == "assistant"]
            if not following:
                return False
            return following[-1].content.strip() == assistant_text.strip()
        return False

    def commit(self, assistant_text: str, *, partial: bool = False, span=None) -> None:
        if self.written:
            return
        self.written = True

        items = turnlog.build_items(self.user_text, assistant_text, partial=partial)
        if items and not partial and self._is_duplicate(assistant_text):
            log.info("turn.skipped.duplicate", extra={"fields": {
                "session": self.session, "len": len(self.user_text)}})
            items = []

        try:
            if self.writer is not None:
                store = Store(self.writer)
                idxs = store.add_turns(self.session, items) if items else []
                if span is not None:
                    store.add_metric(**span.as_row(pricing=self.pricing))
            else:
                # 库没初始化时退化到短连接，失败会被下面捕获
                with Store.open(self.db_path) as store:
                    idxs = store.add_turns(self.session, items) if items else []
                    if span is not None:
                        store.add_metric(**span.as_row(pricing=self.pricing))
            if idxs:
                log.info("turn.written", extra={"fields": {
                    "idxs": idxs, "partial": partial,
                    "u_len": len(self.user_text), "a_len": len(assistant_text)}})
                if self.on_committed is not None:
                    # 钩子自己吞异常：抽取排期失败不该让"记对话"失败，
                    # 更不该让用户看到 500。
                    try:
                        self.on_committed(self.session)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("extract.schedule.failed", extra={"fields": {
                            "session": self.session, "err": repr(exc)}})
        except Exception as exc:  # noqa: BLE001
            log.warning("turn.write.failed", extra={"fields": {
                "session": self.session, "err": str(exc)}})


def _recent_turns(db_path: Path, session: str, limit: int = 40, writer=None):
    try:
        if writer is not None:
            return Store(writer).get_turns(session, limit)
        with Store.open(db_path) as store:
            return store.get_turns(session, limit)
    except Exception:  # noqa: BLE001
        return []


def _endpoint(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base + path


def _inject_stream_options(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """流式必须带 include_usage，否则 usage 永远是 0。

    reasoner 跳过：它的参数集和 chat 不一样，多塞一个字段有 400 的风险，
    而这个风险的收益只是"推理模型也统计成本"——不值。
    """
    if "reasoner" in model.lower():
        return payload
    opts = payload.get("stream_options")
    if isinstance(opts, dict):
        if opts.get("include_usage") is not True:
            payload["stream_options"] = {**opts, "include_usage": True}
    else:
        payload["stream_options"] = {"include_usage": True}
    return payload


def _forward_headers(request: Request, api_key: str) -> dict[str, str]:
    headers = {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in _HOP_BY_HOP or lk == "authorization":
            continue
        # 只带 x- 自定义头（业务透传），其余交给 httpx 自己生成
        if lk.startswith("x-") and lk not in ("x-hippo-session",):
            headers[lk] = v
    return headers


def create_app(
    config: Config | None = None,
    *,
    store_path: str | Path | None = None,
    upstream: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    cfg = config or load_config()
    db_path = Path(store_path) if store_path else cfg.db_file

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # upstream 为 None 时走真实网络；测试注入 MockTransport
        # 常驻写连接：新建连接的页缓存冷启动要 4.5ms，复用后 0.03ms。
        # 这 4.5ms 发生在客户端关连接之前，是实打实的体感。
        app.state.writer = (connect_db(db_path, check_same_thread=False)
                            if Path(db_path).exists() else None)
        app.state.client = httpx.AsyncClient(
            transport=upstream,
            timeout=httpx.Timeout(connect=10.0, read=cfg.deepseek.timeout,
                                 write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
        # ---- M5.4 抽取相关状态 ----
        app.state.extract_enabled = bool(cfg.extract.enabled and app.state.writer is not None)
        app.state.extract_tasks: set[asyncio.Task] = set()
        app.state.extract_running: set[str] = set()
        # M27.3：最近一轮算出来的预算状态。抽取调度读它，而不是自己再查一遍库
        # （抽取每轮都会被问一次，多一次聚合没必要）。没有请求就没有抽取，
        # 所以这个缓存不会被"过期状态"误导。
        app.state.degraded = False
        # 客户端可能只在 Authorization 头里带 key、配置里没有。
        # 抽取是后台任务，拿不到那个头，所以按 session 记住最近一次的 key。
        # 只放内存，不落盘、不进日志。
        app.state.session_keys: dict[str, str] = {}

        # ---- M24 心跳：60s 一轮，六步全是 SQL/算术，唯一花钱的口是
        # Escalator（白名单 + 日上限）。首轮延迟一个周期：生产上晚 60 秒
        # 没有区别，但集成测试往往活不过 60 秒，心跳若立刻跑就会在
        # 别人的断言中间改 tier。
        app.state.hb = None
        app.state.hb_task = None
        app.state.hb_stop = asyncio.Event()
        hb_cfg = getattr(cfg, "heartbeat", None)
        if (app.state.writer is not None and hb_cfg is not None
                and int(getattr(hb_cfg, "interval_seconds", 60)) > 0):
            # 好奇挂在心跳的同一条 60s tick 上，不另造定时器（M24 的交代）。
            # 只读约束（M25.3）在 curiosity 内部：它只负责"准备问题"，
            # 从来不调模型、不写记忆。
            store_for_hb = Store(app.state.writer)
            engine = Curiosity(store_for_hb, cfg)
            app.state.hb = Heartbeat(store_for_hb, cfg=cfg,
                                     on_curiosity=engine.tick)
            app.state.hb_task = asyncio.create_task(
                app.state.hb.run_forever(stop=app.state.hb_stop,
                                         first_delay=app.state.hb.tick_seconds))
        log.info("proxy.start", extra={"fields": {
            "db": str(db_path), "upstream": cfg.deepseek.base_url,
            "mock": upstream is not None, "extract": app.state.extract_enabled,
        }})
        try:
            yield
        finally:
            # 抽取任务不阻塞响应，但**不能悄悄丢掉**：一条聊到一半的窗口
            # 如果被 shutdown 掐断，那段对话就永远不会进记忆。
            pending = [t for t in app.state.extract_tasks if not t.done()]
            if pending:
                log.info("extract.drain", extra={"fields": {"n": len(pending)}})
                try:
                    await asyncio.wait(pending, timeout=15)
                except Exception:  # noqa: BLE001
                    pass
            # 心跳先停再关连接：反过来的话，正在跑的 tick 会撞上
            # "database is closed"，而它只是关机过程中的噪声，不该记 ERROR。
            if app.state.hb_task is not None:
                app.state.hb_stop.set()
                try:
                    await asyncio.wait_for(app.state.hb_task, timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    app.state.hb_task.cancel()
            await app.state.client.aclose()
            if app.state.writer is not None:
                app.state.writer.close()

    app = FastAPI(title="hippocampus-deepseek", version=__version__, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.db_path = db_path

    def _persist(span: Span) -> None:
        """埋点写库。失败只告警——统计坏了不该影响聊天。"""
        try:
            record_metric(db_path, span, pricing=cfg.pricing.as_dict())
        except Exception as exc:  # noqa: BLE001
            log.warning("metric.write.failed", extra={"fields": {
                "req_id": span.req_id, "err": str(exc)}})

    # ------------------------------------------------------------------ health
    @app.get("/health")
    async def health() -> JSONResponse:
        db_state = "missing"
        try:
            from ..core.store import Store
            if db_path.exists():
                with Store.open(db_path) as s:
                    s.ping()
                db_state = "ok"
            else:
                db_state = "missing"
        except Exception as exc:  # noqa: BLE001
            db_state = f"error: {exc}"
        return JSONResponse({
            "status": "ok" if db_state == "ok" else "degraded",
            "db": db_state,
            "version": __version__,
        })

    # ------------------------------------------------------------------ models
    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {"id": "deepseek-flash", "object": "model", "owned_by": "deepseek"},
                {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek"},
                {"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"},
                {"id": "deepseek-reasoner", "object": "model", "owned_by": "deepseek"},
            ],
        }

    # ------------------------------------------------------------------ chat
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        span = Span()
        raw_bytes = await request.body()
        try:
            raw = json.loads(raw_bytes or b"{}")
        except json.JSONDecodeError as exc:
            return JSONResponse(
                {"error": {"message": f"invalid JSON body: {exc}", "type": "invalid_request_error"}},
                status_code=400,
            )

        try:
            parsed = ChatRequest.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"error": {"message": str(exc), "type": "invalid_request_error"}},
                status_code=422,
            )

        session, how = identify(
            request.headers, raw,
            header_name=cfg.session.header, default=cfg.session.default,
        )
        span.session = session
        span.model = parsed.model
        span.stream = bool(parsed.stream)

        with bind(session=session, req_id=span.req_id, stage="proxy"):
            # 本轮真正要记的用户输入。取「最后一条 user」而不是整个 delta，
            # 理由见 core/turns.py:pick_user_text 的注释。
            writer = request.app.state.writer
            known = _recent_turns(db_path, session, writer=writer)
            user_text = pick_user_text(raw.get("messages") or [], known)
            sink = _TurnSink(db_path, session, user_text, known,
                             cfg.pricing.as_dict(), writer=writer,
                             on_committed=lambda s: _schedule_extract(request.app, s))
            api_key = cfg.resolve_api_key(request.headers.get("authorization"))
            if api_key and app.state.extract_enabled:
                app.state.session_keys[session] = api_key
            if not api_key:
                log.warning("request.no_key", extra={"fields": {"session_from": how}})
                return JSONResponse(
                    {"error": {
                        "message": "缺少 DeepSeek API Key：请设置 Authorization 头、"
                                   "config.deepseek.api_key 或环境变量 DEEPSEEK_API_KEY",
                        "type": "invalid_request_error",
                        "code": "missing_api_key",
                    }},
                    status_code=401,
                )

            payload = dict(raw)  # 原样；只在下面对 stream_options 动手
            if parsed.stream:
                _inject_stream_options(payload, parsed.model)

            # ---- M13 装配：检索 + 裁剪 + 注入 ----
            # 整段包在 try 里：记忆坏了不该让用户连聊天都聊不了。
            # 装不上就当没装（模型照常答，只是不记得事），并且明确记日志。
            try:
                store_pack = Store(writer) if writer is not None else None
                if store_pack is not None and user_text:
                    store_pack.attach_config(cfg)
                    # ---- M27.3 预算熔断：每轮开头看一眼，超了下一轮就降级
                    # 检查失败按"不降级"处理：预算读不出来是统计的毛病，
                    # 不该因此让用户失去记忆能力。
                    degrade = False
                    try:
                        bst = budget_check(store_pack, cfg)
                        degrade = bst.degraded
                        app.state.degraded = degrade
                        if degrade:
                            log.warning("budget.degrade", extra={"fields": {
                                "used": bst.used, "limit": bst.limit,
                                "chat": bst.chat_tokens,
                                "extract": bst.extract_tokens}})
                    except Exception as exc:  # noqa: BLE001
                        log.warning("budget.check.failed",
                                    extra={"fields": {"err": repr(exc)}})
                    pres = pack(store_pack, session, user_text, cfg=cfg,
                                degrade=degrade)
                    if pres.text:
                        payload["messages"] = inject(
                            payload.get("messages") or [], pres.text)
                    span.pack_tokens = pres.total_tokens
                    log.info("pack.done", extra={"fields": {
                        "tokens": pres.total_tokens, "reused": pres.reused,
                        "degraded": pres.degraded,
                        "trigger": pres.trigger.reason if pres.trigger else "",
                        "dropped": len(pres.dropped), "digest": pres.digest}})
            except Exception as exc:  # noqa: BLE001
                log.warning("pack.failed", extra={"fields": {"err": repr(exc)}})

            span.pack_done()
            url = _endpoint(cfg.deepseek.base_url, "/chat/completions")
            headers = _forward_headers(request, api_key)
            client: httpx.AsyncClient = request.app.state.client

            log.info("request.received", extra={"fields": {
                "session_from": how, "stream": span.stream, "turns": len(raw.get("messages") or []),
            }})

            try:
                upstream_req = client.build_request(
                    "POST", url, content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers=headers,
                )
                upstream = await client.send(upstream_req, stream=bool(parsed.stream))
            except httpx.HTTPError as exc:
                span.finish()
                log.error("upstream.error", extra={"fields": {"err": repr(exc)}})
                _persist(span)
                return JSONResponse(
                    {"error": {"message": f"上游连接失败: {exc}", "type": "upstream_error"}},
                    status_code=502,
                )

            # 上游报错：原样回给客户端，别包装成 200
            if upstream.status_code >= 400:
                body = await upstream.aread()
                await upstream.aclose()
                span.finish()
                log.warning("upstream.status", extra={"fields": {
                    "status": upstream.status_code, "span": span.summary()}})
                _persist(span)
                return Response(
                    content=body,
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"),
                )

            if not parsed.stream:
                return await _respond_unary(upstream, span, sink)

            return StreamingResponse(
                _relay(upstream, span, sink),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",  # 没有这行，nginx 会攒满 buffer 才吐
                },
            )

    return app


async def _respond_unary(upstream: httpx.Response, span: Span, sink) -> Response:
    try:
        body = await upstream.aread()
    finally:
        await upstream.aclose()

    span.first_byte()
    data = None
    try:
        data = json.loads(body)
        span.absorb_usage(data.get("usage"))
    except (ValueError, AttributeError):
        data = None

    assistant = ""
    if isinstance(data, dict):
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            if isinstance(message.get("content"), str):
                assistant = message["content"]

    span.finish()
    sink.commit(assistant, span=span)

    log.info("response.done", extra={"fields": span.summary()})
    return Response(
        content=body,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


async def _relay(upstream: httpx.Response, span: Span, sink):
    """SSE 逐行透传。

    几个必须做对的点：
      - 空行要重新补上：aiter_lines 会把 SSE 的空行吃掉，不补客户端会黏成一行
      - usage 在最后一块（choices 为 []），必须解析出来
      - 客户端断开时 aclose，否则上游连接会挂到超时
      - **转发的同时累积 assistant 文本**，否则 turns 里只有用户说的话，
        记忆就变成了"用户日记"而不是"对话"
    """
    acc = StreamAccumulator()
    settled = {"done": False}

    def settle(partial: bool) -> None:
        """结账，只结一次。

        必须在收到 `[DONE]` 的**当场**调用，不能只写在 finally 里。
        踩过的坑：客户端读到 `[DONE]` 就立刻关连接，Starlette 取消生成器，
        finally 里的落库可能整个不执行 —— 表现为"聊了 20 轮，库里只有 19 轮"。
        单元测试发现不了，因为测试会把流读完再断言。
        """
        if settled["done"]:
            return
        settled["done"] = True
        span.finish()
        sink.commit(acc.text, partial=partial, span=span)
        log.info("stream.done", extra={"fields": span.summary()})

    try:
        async for line in upstream.aiter_lines():
            if not line:
                continue
            span.first_byte()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data == "[DONE]":
                    acc.done = True
                    settle(False)
                elif data:
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        chunk = None
                    if isinstance(chunk, dict):
                        acc.feed(chunk)
                        if chunk.get("usage"):
                            span.absorb_usage(chunk["usage"])
            yield (line + "\n\n").encode("utf-8")
    except asyncio.CancelledError:
        log.info("stream.cancelled", extra={"fields": {"req_id": span.req_id}})
        raise
    finally:
        await upstream.aclose()
        # 断开或上游报错时 acc.complete 为 False → 半截内容标 partial
        settle(not acc.complete)


# ---------------------------------------------------------------- M5.4 抽取排期
def _schedule_extract(app: FastAPI, session: str) -> None:
    """结账后决定要不要排一次抽取。**同步、极快、绝不 await**。

    这个函数在"用户已经收到最后一个字之后"执行，理论上可以随便花时间，
    但它跑在请求协程里——一旦 await，客户端关连接的行为就会受影响。
    所以这里只做一次 count 查询（微秒级），然后把活扔给后台任务。

    三道闸：
      1. 抽取总开关（配置关掉 / 库不存在）
      2. 攒够轮数（every_n_turns × 2 条 turn）
      3. 这个 session 已经在抽了（避免一次对话排 5 个任务，5 次调用打同一个窗口）

    M27.3 加了第四道：预算降级中不抽。抽取是**后台的第二笔钱**
    （每 5 轮一次全窗口调用），对话还在花钱时继续抽等于一边漏水一边加水。
    攒着的 turn 不会丢——预算恢复后 pending 还在，下一轮会一次抽完。
    """
    if not getattr(app.state, "extract_enabled", False):
        return
    if getattr(app.state, "degraded", False):
        log.info("extract.skip.budget", extra={"fields": {"session": session}})
        return
    cfg: Config = app.state.cfg
    try:
        store = Store(app.state.writer)
        pending = store.pending_turn_count(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("extract.count.failed", extra={"fields": {"err": repr(exc)}})
        return

    if pending < cfg.extract.every_n_turns * 2:
        return
    if session in app.state.extract_running:
        return

    app.state.extract_running.add(session)
    task = asyncio.create_task(_extract_job(app, session))
    app.state.extract_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        app.state.extract_tasks.discard(t)
        app.state.extract_running.discard(session)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            log.warning("extract.job.failed", extra={"fields": {
                "session": session, "err": repr(exc)}})

    task.add_done_callback(_done)


async def _extract_job(app: FastAPI, session: str) -> None:
    """后台抽取。所有异常都在这里吃掉——它没有调用方能接住。"""
    from ..core.encode import Encoder
    from ..core.extractor import Extractor

    cfg: Config = app.state.cfg
    key = app.state.session_keys.get(session) or cfg.resolve_api_key(None)
    if not key:
        log.info("extract.skip.no_key", extra={"fields": {"session": session}})
        return

    try:
        store = Store(app.state.writer)
        encoder = Encoder(cfg, app.state.client, key)
        ex = Extractor(store, cfg, encoder)
        rep = await ex.run(session)
        if rep.results:
            log.info("extract.session.done", extra={"fields": {
                "session": session, "windows": len(rep.results),
                "counts": rep.counts(), "events": rep.events, "cost": rep.cost}})
    except Exception as exc:  # noqa: BLE001
        log.error("extract.job.error", extra={"fields": {
            "session": session, "err": repr(exc)}})
