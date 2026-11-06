"""M29 · DSH 内核接口（/health / event / assemble）。

这是插件形态的"最小可用面"：宿主半只调这几个接口，所以它们必须稳。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from hippo.config import Config
from hippo.dsh_kernel import create_kernel


@pytest.fixture()
def kernel(tmp_path):
    cfg = Config()
    cfg.db_path = str(tmp_path / "mem.db")
    app = create_kernel(cfg, api_key="")
    with TestClient(app) as client:
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
