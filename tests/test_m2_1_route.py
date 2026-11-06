"""M2.1 路由与请求解析验收。"""

from __future__ import annotations

import json

import httpx
import pytest

from hippo.adapters.proxy import create_app
from tests.conftest import serve, metrics_rows
from tests.fake_deepseek import FakeDeepSeek, completion, unary_fake

BODY = {
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "我叫什么？"}],
}


async def test_unary_returns_openai_shape(cfg, db_path, conn):
    fake = unary_fake("你叫阿岚。")
    app = create_app(cfg, store_path=db_path, upstream=fake.transport())
    async with serve(app) as c:
        r = await c.post("/v1/chat/completions", json=BODY,
                         headers={"authorization": "Bearer sk-x"})

    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "你叫阿岚。"
    assert data["usage"]["prompt_tokens"] == 1024


async def test_unknown_params_pass_through(cfg, db_path, conn):
    fake = unary_fake()
    app = create_app(cfg, store_path=db_path, upstream=fake.transport())
    payload = {**BODY, "top_p": 0.7, "presence_penalty": 0.3,
               "seed": 42, "some_vendor_field": {"nested": [1, 2]}}
    async with serve(app) as c:
        r = await c.post("/v1/chat/completions", json=payload,
                         headers={"authorization": "Bearer sk-x"})

    assert r.status_code == 200
    sent = fake.last_json()
    assert sent["top_p"] == 0.7
    assert sent["presence_penalty"] == 0.3
    assert sent["seed"] == 42
    assert sent["some_vendor_field"] == {"nested": [1, 2]}


async def test_messages_not_rewritten(cfg, db_path, conn):
    fake = unary_fake()
    app = create_app(cfg, store_path=db_path, upstream=fake.transport())
    msgs = [
        {"role": "system", "content": "你是阿岚。"},
        {"role": "user", "content": "早"},
        {"role": "assistant", "content": "早呀"},
        {"role": "user", "content": "今天几号"},
    ]
    async with serve(app) as c:
        await c.post("/v1/chat/completions", json={**BODY, "messages": msgs},
                     headers={"authorization": "Bearer sk-x"})
    sent = fake.last_json()["messages"]
    # M13 之后允许**前置注入**（system 头部 + 时间锚），但不许改写对话本身：
    # 用户说的每句话、AI 回的每句话，逐字逐序都不能动。
    #
    # 唯一的例外是**最后一条 user**：时间锚以 `[现在 …]\n` 前缀的形式挂在它
    # 前面（M13 实测：锚单独成条 93% 命中，并入 user 94%，差在一条消息的
    # 格式开销，所以选并入）。例外要写成精确断言，不能放宽成"内容大致相同"——
    # 一旦放宽，后面有人往里塞别的东西也不会被发现。
    dialog = [m for m in sent if m["role"] != "system"]
    orig = [m for m in msgs if m["role"] != "system"]
    assert [m["role"] for m in dialog] == [m["role"] for m in orig], "消息条数/角色被改了"
    assert dialog[:-1] == orig[:-1], "除最后一条外，对话内容被改写了"
    last, last_orig = dialog[-1], orig[-1]
    assert last["content"].endswith("\n" + last_orig["content"]), "用户原话没有完整保留"
    prefix = last["content"][: -len(last_orig["content"]) - 1]
    assert prefix.startswith("[现在 "), f"user 消息前面只允许时间锚，实际是 {prefix!r}"
    assert "\n" not in prefix, "前缀只能是单行时间锚"

    assert sent[0]["role"] == "system"
    assert sent[0]["content"].endswith("你是阿岚。"), "客户端 system 应被保留在末尾"


async def test_missing_key_returns_401(db_path, conn):
    from hippo.config import Config
    cfg = Config()
    cfg.db_path = str(db_path)
    cfg.deepseek.api_key = ""

    app = create_app(cfg, store_path=db_path, upstream=unary_fake().transport())
    async with serve(app) as c:
        r = await c.post("/v1/chat/completions", json=BODY)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_api_key"


async def test_models_endpoint(cfg, db_path, conn):
    app = create_app(cfg, store_path=db_path, upstream=unary_fake().transport())
    async with serve(app) as c:
        r = await c.get("/v1/models")
    ids = [m["id"] for m in r.json()["data"]]
    assert "deepseek-chat" in ids and "deepseek-reasoner" in ids


async def test_health_reports_db(cfg, db_path, conn):
    app = create_app(cfg, store_path=db_path, upstream=unary_fake().transport())
    async with serve(app) as c:
        r = await c.get("/health")
    body = r.json()
    assert body["status"] == "ok" and body["db"] == "ok"
    assert body["version"]


async def test_upstream_429_is_passed_through(cfg, db_path, conn):
    fake = FakeDeepSeek(status=429, json_body={"error": {"message": "rate limited"}})
    app = create_app(cfg, store_path=db_path, upstream=fake.transport())
    async with serve(app) as c:
        r = await c.post("/v1/chat/completions", json=BODY,
                         headers={"authorization": "Bearer sk-x"})
    assert r.status_code == 429
    assert r.json()["error"]["message"] == "rate limited"


async def test_upstream_unreachable_returns_502(cfg, db_path, conn):
    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    app = create_app(cfg, store_path=db_path, upstream=httpx.MockTransport(boom))
    async with serve(app) as c:
        r = await c.post("/v1/chat/completions", json=BODY,
                         headers={"authorization": "Bearer sk-x"})
    assert r.status_code == 502
    assert "上游连接失败" in r.json()["error"]["message"]


async def test_bad_json_returns_400(cfg, db_path, conn):
    app = create_app(cfg, store_path=db_path, upstream=unary_fake().transport())
    async with serve(app) as c:
        r = await c.post("/v1/chat/completions", content=b"{not json",
                         headers={"authorization": "Bearer sk-x",
                                  "content-type": "application/json"})
    assert r.status_code == 400


async def test_metrics_written_with_four_timestamps(cfg, db_path, conn):
    app = create_app(cfg, store_path=db_path, upstream=unary_fake().transport())
    async with serve(app) as c:
        await c.post("/v1/chat/completions", json=BODY,
                     headers={"authorization": "Bearer sk-x"})

    rows = metrics_rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert all(row[k] is not None for k in ("t0", "t1", "t2", "t3")), row
    assert row["t0"] <= row["t1"] <= row["t2"] <= row["t3"]
    assert row["prompt_tokens"] == 1024
    assert row["completion_tokens"] == 231
    assert row["cached_tokens"] == 768
    assert row["cost"] > 0
