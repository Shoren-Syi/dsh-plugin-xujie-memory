"""M29 · DSH 内核接口（/health / event / assemble）。

这是插件形态的"最小可用面"：宿主半只调这几个接口，所以它们必须稳。
"""

from __future__ import annotations

import sqlite3
import asyncio
import subprocess
import sys
import time

import pytest
import httpx
from fastapi.testclient import TestClient

from hippo.config import Config
from hippo.dsh_kernel import create_kernel


@pytest.fixture()
def kernel(tmp_path):
    cfg = Config()
    cfg.db_path = str(tmp_path / "mem.db")
    app = create_kernel(cfg, api_key="")
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        yield client


def _turns(db_path) -> int:
    c = sqlite3.connect(db_path)
    try:
        return c.execute("select count(*) from turns").fetchone()[0]
    finally:
        c.close()


def test_health(kernel) -> None:
    r = kernel.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"]
    assert body["extract"] is False
    assert kernel.get("/board/data").json()["extraction_configured"] is False


def test_event_writes_turns(kernel, tmp_path) -> None:
    assert kernel.post("/event", json={
        "session": "s1", "role": "user", "text": "我叫张三，住在杭州，对花生过敏。"}).json()["ok"]
    assert kernel.post("/event", json={
        "session": "s1", "role": "assistant", "text": "记住了"}).json()["ok"]
    assert _turns(tmp_path / "mem.db") == 2


def test_empty_text_skipped(kernel, tmp_path) -> None:
    r = kernel.post("/event", json={"session": "s1", "role": "user", "text": "   "})
    assert r.json().get("skipped") == "empty"
    assert _turns(tmp_path / "mem.db") == 0


def test_assemble_returns_text_and_anchor(kernel) -> None:
    kernel.post("/event", json={"session": "s1", "role": "user", "text": "你好"})
    r = kernel.post("/assemble", json={"session": "s1", "text": "你好"})
    body = r.json()
    assert body["ok"] is True
    assert "现在" in body["text"]  # 时间锚必须在
    assert body["tokens"] > 0


def test_assemble_missing_session_is_safe(kernel) -> None:
    r = kernel.post("/assemble", json={"session": "", "text": "你好"})
    assert r.status_code == 200
    assert isinstance(r.json().get("text"), str)


def test_extract_without_key_is_noop(kernel) -> None:
    r = kernel.post("/extract")
    assert r.json()["ok"] is True


def test_browser_boundary_rejects_cross_origin_and_dns_rebinding(kernel, tmp_path) -> None:
    payload = {"session": "s1", "role": "user", "text": "untrusted"}
    for origin in ("https://evil.example", "null", "http://localhost:8766", "http://127.0.0.1:9999"):
        assert kernel.get("/board/data", headers={"origin": origin}).status_code == 403
        assert kernel.post("/event", json=payload, headers={"origin": origin}).status_code == 403
    assert kernel.get("/board/data", headers={"host": "evil.example:8766"}).status_code == 403
    assert kernel.get("/board/data", headers={"origin": "http://127.0.0.1:8766"}).status_code == 200
    assert _turns(tmp_path / "mem.db") == 0


def test_parent_watch_does_not_kill_parent_and_exits_after_parent():
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    watcher = subprocess.Popen([
        sys.executable, "-c",
        "import sys; from hippo.dsh_kernel import _watch_parent; _watch_parent(int(sys.argv[1]))",
        str(parent.pid),
    ])
    try:
        time.sleep(2)
        assert parent.poll() is None, "监视操作不能终止父进程"
        assert watcher.poll() is None, "父进程仍在时内核不能退出"
        parent.terminate()
        parent.wait(timeout=5)
        assert watcher.wait(timeout=8) == 0
    finally:
        for proc in (parent, watcher):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_drain_waits_for_complete_turns_and_queued_sessions(tmp_path, monkeypatch):
    from hippo import dsh_kernel
    from hippo.core.extractor import SessionReport

    cfg = Config()
    cfg.db_path = str(tmp_path / 'mem.db')
    cfg.extract.every_n_turns = 1
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    class FakeExtractor:
        def __init__(self, *_args):
            pass

        async def run(self, session, force=False):
            calls.append(session)
            if session == 'a':
                started.set()
                await release.wait()
            return SessionReport(session=session)

    monkeypatch.setattr(dsh_kernel, 'Extractor', FakeExtractor)
    app = create_kernel(cfg, api_key='test')
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            async def send(session, role):
                return await client.post('/event', json={'session': session, 'role': role, 'text': 'test'})
            await send('a', 'user')
            assert calls == []  # no extraction before the assistant message
            await send('a', 'assistant')
            await asyncio.wait_for(started.wait(), 2)
            await send('b', 'user')
            await send('b', 'assistant')
            release.set()
            result = await client.post('/drain')
            assert result.json() == {'ok': True, 'remaining': 0}
            assert calls == ['a', 'b']
