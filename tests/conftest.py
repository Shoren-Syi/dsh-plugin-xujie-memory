from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from hippo.config import Config
from hippo.core.db import connect
from hippo.core.migrate import migrate
from hippo.core.store import Store


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "mem.db"


@pytest.fixture()
def conn(db_path):
    c = connect(db_path)
    migrate(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def store(conn):
    return Store(conn)


@pytest.fixture()
def cfg(db_path):
    c = Config()
    c.db_path = str(db_path)
    c.deepseek.api_key = "sk-test"
    c.deepseek.base_url = "https://api.deepseek.com"
    return c


@asynccontextmanager
async def serve(app, **client_kw):
    """跑一个 ASGI 客户端，并且**手动驱动 lifespan**。

    httpx.ASGITransport 不会触发 startup/shutdown，所以上游 client
    不会被创建 —— 这是新手用 ASGITransport 最常踩的坑（表现为
    `app.state.client` 不存在）。
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hippo", **client_kw) as c:
            yield c


def metrics_rows(db_path) -> list[dict]:
    c = connect(db_path)
    try:
        return [dict(r) for r in c.execute("SELECT * FROM metrics ORDER BY id").fetchall()]
    finally:
        c.close()


def read_jsonl(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture()
async def live_server():
    """起一个真的 uvicorn，绑 127.0.0.1 的随机端口。

    为什么非要有它：httpx 的 ASGITransport 会把所有响应块
    `b"".join()` 成一个 —— 于是"流式"测试永远测不出缓冲问题。
    唯一可信的验证是真过一遍 TCP。
    """
    import asyncio

    import uvicorn

    started: list[tuple[uvicorn.Server, asyncio.Task]] = []

    async def _start(app) -> str:
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="error", access_log=False,
        ))
        task = asyncio.create_task(server.serve())
        for _ in range(1000):
            if server.started:
                break
            if task.done():
                task.result()
            await asyncio.sleep(0.005)
        else:
            raise RuntimeError("uvicorn 启动超时")
        port = server.servers[0].sockets[0].getsockname()[1]
        started.append((server, task))
        return f"http://127.0.0.1:{port}"

    yield _start

    for server, task in started:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, Exception):
            task.cancel()


def turns_rows(db_path) -> list[dict]:
    c = connect(db_path)
    try:
        return [dict(r) for r in c.execute(
            "SELECT * FROM turns ORDER BY idx ASC").fetchall()]
    finally:
        c.close()
