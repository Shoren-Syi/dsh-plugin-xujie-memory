"""M3.1 turns 写入验收。

这一层的 bug 都很隐蔽：多写一条不会报错，少写一条也不会报错，
只会在两周后表现为"它怎么不记得"。
所以用例全部直接数行、直接比内容。
"""

from __future__ import annotations

import pytest

from hippo.adapters.proxy import create_app
from hippo.core.turns import clip, estimate_tokens, pick_user_text, plan_delta, record_pair
from hippo.core.models import Turn
from tests.conftest import serve, turns_rows
from tests.fake_deepseek import FakeDeepSeek, delta, sse, streaming_fake, unary_fake

AUTH = {"authorization": "Bearer sk-x"}


async def _chat(client, messages, headers=None):
    return await client.post("/v1/chat/completions",
                             json={"model": "deepseek-chat", "messages": messages},
                             headers=headers or AUTH)


async def test_twenty_rounds_yield_forty_turns(cfg, db_path, conn):
    app = create_app(cfg, store_path=db_path, upstream=unary_fake("收到").transport())
    history: list[dict] = []
    async with serve(app) as c:
        for i in range(20):
            history.append({"role": "user", "content": f"第{i}轮"})
            r = await _chat(c, list(history))
            assert r.status_code == 200
            history.append({"role": "assistant", "content": "收到"})

    rows = turns_rows(db_path)
    assert len(rows) == 40, f"20 轮应为 40 条，实际 {len(rows)}"
    assert [r["idx"] for r in rows] == list(range(40)), "idx 有空洞"
    assert [r["role"] for r in rows[:4]] == ["user", "assistant", "user", "assistant"]
    assert rows[0]["content"] == "第0轮"
    assert rows[1]["content"] == "收到"


async def test_stream_accumulates_assistant_text(cfg, db_path, conn):
    app = create_app(cfg, store_path=db_path, upstream=streaming_fake(5).transport())
    async with serve(app) as c:
        async with c.stream("POST", "/v1/chat/completions",
                            json={"model": "deepseek-chat", "stream": True,
                                  "messages": [{"role": "user", "content": "数数"}]},
                            headers=AUTH) as r:
            async for _ in r.aiter_text():
                pass

    rows = turns_rows(db_path)
    assert len(rows) == 2
    assert rows[0]["content"] == "数数"
    assert rows[1]["role"] == "assistant"
    assert rows[1]["content"] == "字0字1字2字3字4", "流式内容没拼全"
    assert rows[1]["partial"] == 0


async def test_stream_without_done_marked_partial(cfg, db_path, conn):
    """上游半路断了：内容留下，但必须标 partial，M15 不能拿它当事实。"""
    body = sse(delta("半截话"), delta("还没说完"), usage=None, done=False)
    app = create_app(cfg, store_path=db_path,
                     upstream=FakeDeepSeek(body=body).transport())
    async with serve(app) as c:
        async with c.stream("POST", "/v1/chat/completions",
                            json={"model": "deepseek-chat", "stream": True,
                                  "messages": [{"role": "user", "content": "讲个故事"}]},
                            headers=AUTH) as r:
            async for _ in r.aiter_text():
                pass

    rows = turns_rows(db_path)
    assert rows[1]["content"] == "半截话还没说完"
    assert rows[1]["partial"] == 1, "中断的响应没有标记 partial"


async def test_retry_does_not_duplicate(cfg, db_path, conn):
    """客户端重试会原样重发上次请求，不能因此多记一轮。"""
    app = create_app(cfg, store_path=db_path, upstream=unary_fake("收到").transport())
    req = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "你好"}]}
    async with serve(app) as c:
        await c.post("/v1/chat/completions", json=req, headers=AUTH)
        await c.post("/v1/chat/completions", json=req, headers=AUTH)
        await c.post("/v1/chat/completions", json=req, headers=AUTH)

    assert len(turns_rows(db_path)) == 2, "重复请求被写了多次"


async def test_single_message_client(cfg, db_path, conn):
    """有些客户端每轮只发当前一条，不带历史。

    注意必须显式给 session：不给的话，兜底是 sha256(首条 user 消息)，
    每轮内容不同 → 每轮一个新会话 → 记忆碎裂。
    这是 M2.3 兜底策略的真实边界，已登记在 R-3.1 遗留问题里。
    """
    app = create_app(cfg, store_path=db_path, upstream=unary_fake("嗯").transport())
    solo = {**AUTH, "X-Hippo-Session": "solo"}
    async with serve(app) as c:
        await _chat(c, [{"role": "user", "content": "第一句"}], solo)
        await _chat(c, [{"role": "user", "content": "第二句"}], solo)

    rows = turns_rows(db_path)
    assert [r["content"] for r in rows] == ["第一句", "嗯", "第二句", "嗯"]


async def test_sessions_are_isolated(cfg, db_path, conn):
    app = create_app(cfg, store_path=db_path, upstream=unary_fake("好").transport())
    async with serve(app) as c:
        await _chat(c, [{"role": "user", "content": "A 的话"}],
                    {**AUTH, "X-Hippo-Session": "a"})
        await _chat(c, [{"role": "user", "content": "B 的话"}],
                    {**AUTH, "X-Hippo-Session": "b"})

    a = [r for r in turns_rows(db_path) if r["session"] == "a"]
    b = [r for r in turns_rows(db_path) if r["session"] == "b"]
    assert len(a) == 2 and len(b) == 2
    assert a[0]["idx"] == 0 and b[0]["idx"] == 0, "不同 session 的 idx 应各自从 0 开始"


async def test_no_assistant_text_still_records_user(cfg, db_path, conn):
    """模型返回空内容（只有 finish_reason）：user 那条不能丢。"""
    fake = FakeDeepSeek(json_body={
        "id": "x", "object": "chat.completion", "created": 1, "model": "deepseek-chat",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": ""},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 0},
    })
    app = create_app(cfg, store_path=db_path, upstream=fake.transport())
    async with serve(app) as c:
        await _chat(c, [{"role": "user", "content": "在吗"}])

    rows = turns_rows(db_path)
    assert len(rows) == 1 and rows[0]["role"] == "user"


async def test_long_content_is_clipped(cfg, db_path, conn):
    app = create_app(cfg, store_path=db_path, upstream=unary_fake("收到").transport())
    huge = "啊" * 70_000
    async with serve(app) as c:
        await _chat(c, [{"role": "user", "content": huge}])

    row = turns_rows(db_path)[0]
    assert len(row["content"]) <= 64_000 + 16
    assert row["content"].endswith("…[truncated]")
    assert row["partial"] == 1


# ---------------------------------------------------------------- 纯函数
def _t(idx, role, content):
    return Turn(session="s", idx=idx, role=role, content=content, ts=0)


def test_plan_delta_on_new_turn():
    known = [_t(0, "user", "早"), _t(1, "assistant", "早呀")]
    msgs = [{"role": "user", "content": "早"},
            {"role": "assistant", "content": "早呀"},
            {"role": "user", "content": "今天冷吗"}]
    assert plan_delta(msgs, known) == [("user", "今天冷吗")]


def test_plan_delta_when_client_resends_only_last():
    """客户端只发一条时，公共后缀为 0，整条都算新的 —— 由去重逻辑兜底。"""
    known = [_t(0, "user", "早"), _t(1, "assistant", "早呀")]
    assert plan_delta([{"role": "user", "content": "今天冷吗"}], known) == [("user", "今天冷吗")]


def test_plan_delta_when_history_matches_fully():
    known = [_t(0, "user", "早"), _t(1, "assistant", "早呀")]
    msgs = [{"role": "user", "content": "早"}, {"role": "assistant", "content": "早呀"}]
    assert plan_delta(msgs, known) == []


def test_pick_user_falls_back_to_last_user():
    known = [_t(0, "user", "早"), _t(1, "assistant", "早呀")]
    msgs = [{"role": "system", "content": "你是阿岚"},
            {"role": "user", "content": "早"},
            {"role": "assistant", "content": "早呀"},
            {"role": "user", "content": "在干嘛"}]
    assert pick_user_text(msgs, known) == "在干嘛"


def test_clip_boundary():
    assert clip("短") == ("短", False)
    long_text, cut = clip("x" * 10, limit=5)
    assert cut and long_text.startswith("xxxxx")


def test_estimate_tokens_cjk_higher_than_latin():
    assert estimate_tokens("你好世界") > estimate_tokens("abcd")
    assert estimate_tokens("") == 0


def test_record_pair_skips_empty():
    assert record_pair(":memory:", "s", "", "") == []


async def test_last_turn_saved_when_client_quits_at_done(cfg, db_path, conn, live_server):
    """客户端读到 [DONE] 立刻断开——最后一轮不能丢。

    这条是 P0 冒烟炸出来的：20 轮只落了 19 轮。
    根因是记账写在 finally 里，而客户端关连接会取消生成器。
    必须过真 TCP 才复现（ASGITransport 会把流读完，永远测不出来）。
    """
    import asyncio

    import httpx as _httpx

    app = create_app(cfg, store_path=db_path, upstream=streaming_fake(5).transport())
    base = await live_server(app)
    async with _httpx.AsyncClient(base_url=base, timeout=15) as c:
        async with c.stream("POST", "/v1/chat/completions",
                            json={"model": "deepseek-chat", "stream": True,
                                  "messages": [{"role": "user", "content": "最后一轮"}]},
                            headers=AUTH) as r:
            async for line in r.aiter_lines():
                if line.strip() == "data: [DONE]":
                    break

    await asyncio.sleep(0.05)
    rows = turns_rows(db_path)
    assert len(rows) == 2, f"最后一轮没落库，只有 {len(rows)} 条"
    assert rows[1]["content"] == "字0字1字2字3字4"
    assert rows[1]["partial"] == 0, "正常结束不该标 partial"
