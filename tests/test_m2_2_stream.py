"""M2.2 流式透传验收 —— 全项目体感最重要的一环。

这里有个容易糊弄过去的点：**"流式"不等于"分块"**。
如果代理先 read() 全量再一次性 yield，客户端照样能收到，
但首字延迟等于整个生成时间，用户会觉得 AI 变笨了。
所以下面的用例断言的是"块数 > 10 且逐块到达"，不是"200 且非空"。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from hippo.adapters.proxy import create_app
from tests.conftest import serve, metrics_rows
from tests.fake_deepseek import FakeDeepSeek, USAGE, delta, sse, streaming_fake

BODY = {"model": "deepseek-chat", "stream": True,
        "messages": [{"role": "user", "content": "讲个长故事"}]}


async def _collect(client, payload=BODY, headers=None):
    chunks: list[str] = []
    async with client.stream("POST", "/v1/chat/completions", json=payload,
                             headers=headers or {"authorization": "Bearer sk-x"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        async for text in r.aiter_text():
            chunks.append(text)
    return chunks


async def test_relay_yields_per_line():
    """精确验证：每一行 SSE 单独 yield，不是攒完再一次吐。

    比"数 TCP 包"可靠得多 —— 块数会受 MTU、Nagle、事件循环影响，
    而有没有缓冲是代码结构问题，这里一眼能看出。
    """
    from hippo.adapters.proxy import _relay
    from hippo.core.telemetry import Span

    class _NullSink:
        def commit(self, *a, **kw):
            pass

    body = sse(*[delta(f"第{i}段") for i in range(20)], usage=None, done=True)
    upstream = httpx.Response(200, content=body.encode("utf-8"))
    span = Span()

    out = [c async for c in _relay(upstream, span, _NullSink())]
    assert len(out) == 21, f"20 个 delta + [DONE] 应逐行 yield，实际 {len(out)}"
    assert all(c.endswith(b"\n\n") for c in out)


async def test_stream_is_incremental_over_tcp(cfg, db_path, conn, live_server):
    """首个 SSE 事件必须在上游允许结束之前穿过真实 TCP 到达客户端。"""
    release = asyncio.Event()

    class GatedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse(delta("第一段"), done=False).encode("utf-8")
            await release.wait()
            yield sse(delta("第二段"), usage=USAGE).encode("utf-8")

    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, stream=GatedStream(), headers={"content-type": "text/event-stream"}))
    app = create_app(cfg, store_path=db_path, upstream=transport)
    base = await live_server(app)

    try:
        async with httpx.AsyncClient(base_url=base, timeout=15) as c:
            async with c.stream("POST", "/v1/chat/completions", json=BODY,
                                headers={"authorization": "Bearer sk-x"}) as r:
                assert r.status_code == 200
                assert r.headers["content-type"].startswith("text/event-stream")
                assert r.headers.get("x-accel-buffering") == "no"
                lines = r.aiter_lines()
                first = await asyncio.wait_for(anext(lines), timeout=3)
                assert "第一段" in first
                release.set()
                rest = [line async for line in lines]
                assert any("第二段" in line for line in rest)
                assert "data: [DONE]" in rest
    finally:
        release.set()


async def test_stream_ends_with_done(cfg, db_path, conn):
    app = create_app(cfg, store_path=db_path, upstream=streaming_fake(3).transport())
    async with serve(app) as c:
        joined = "".join(await _collect(c))
    assert joined.rstrip().endswith("data: [DONE]")


async def test_stream_usage_captured(cfg, db_path, conn):
    app = create_app(cfg, store_path=db_path, upstream=streaming_fake(5).transport())
    async with serve(app) as c:
        await _collect(c)

    rows = metrics_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == USAGE["prompt_tokens"]
    assert rows[0]["completion_tokens"] == USAGE["completion_tokens"]
    assert rows[0]["cached_tokens"] == USAGE["prompt_cache_hit_tokens"]


async def test_include_usage_injected(cfg, db_path, conn):
    fake = streaming_fake(2)
    app = create_app(cfg, store_path=db_path, upstream=fake.transport())
    async with serve(app) as c:
        await _collect(c)
    assert fake.last_json()["stream_options"]["include_usage"] is True


async def test_include_usage_merged_not_overwritten(cfg, db_path, conn):
    fake = streaming_fake(2)
    app = create_app(cfg, store_path=db_path, upstream=fake.transport())
    async with serve(app) as c:
        await _collect(c, payload={**BODY, "stream_options": {"include_usage": False,
                                                              "vendor_opt": 1}})
    opts = fake.last_json()["stream_options"]
    assert opts["include_usage"] is True
    assert opts["vendor_opt"] == 1, "不能把客户端自己的 stream_options 冲掉"


async def test_reasoner_not_injected(cfg, db_path, conn):
    fake = streaming_fake(2)
    app = create_app(cfg, store_path=db_path, upstream=fake.transport())
    async with serve(app) as c:
        await _collect(c, payload={**BODY, "model": "deepseek-reasoner"})
    assert "stream_options" not in fake.last_json()


async def test_stream_error_status_passed_through(cfg, db_path, conn):
    fake = FakeDeepSeek(status=429, json_body={"error": {"message": "too many"}})
    app = create_app(cfg, store_path=db_path, upstream=fake.transport())
    async with serve(app) as c:
        async with c.stream("POST", "/v1/chat/completions", json=BODY,
                            headers={"authorization": "Bearer sk-x"}) as r:
            assert r.status_code == 429
            body = await r.aread()
    assert json.loads(body)["error"]["message"] == "too many"


async def test_blank_lines_preserved_between_events(cfg, db_path, conn):
    """aiter_lines 会吃掉空行；不补回来客户端会把两条 data 黏成一条。"""
    body = sse(delta("A"), delta("B"), usage=None, done=False)
    fake = FakeDeepSeek(body=body)
    app = create_app(cfg, store_path=db_path, upstream=fake.transport())
    async with serve(app) as c:
        joined = "".join(await _collect(c))

    events = [e for e in joined.split("\n\n") if e.strip()]
    assert len(events) == 2
    assert all(e.startswith("data: ") for e in events)


async def test_ttfb_earlier_than_total(cfg, db_path, conn):
    app = create_app(cfg, store_path=db_path, upstream=streaming_fake(10).transport())
    async with serve(app) as c:
        await _collect(c)
    row = metrics_rows(db_path)[0]
    assert row["t2"] - row["t0"] <= row["t3"] - row["t0"]
