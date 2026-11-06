"""M2.3 会话识别验收。

session 是记忆的主键。这里错一次，后面所有检索都在错误的分区里找，
表现为"AI 完全不记得你"，而你会以为是记忆模块坏了。
"""

from __future__ import annotations

import pytest

from hippo.adapters.proxy import create_app
from hippo.adapters.session import identify, first_user_text
from tests.conftest import serve, metrics_rows
from tests.fake_deepseek import unary_fake


def test_header_wins():
    s, how = identify({"X-Hippo-Session": "alice"}, {"user": "bob",
                                                    "messages": [{"role": "user", "content": "x"}]})
    assert (s, how) == ("alice", "header")


def test_body_user_second():
    s, how = identify({}, {"user": "bob", "messages": [{"role": "user", "content": "x"}]})
    assert (s, how) == ("bob", "body.user")


def test_hash_fallback_is_stable():
    body = {"messages": [{"role": "user", "content": "我叫阿岚"}]}
    a, how_a = identify({}, body)
    b, _ = identify({}, body)
    assert a == b and how_a == "first_user_hash"
    assert a.startswith("auto-") and len(a) == 17


def test_hash_uses_first_user_message_not_whole_history():
    """用整个 messages 的哈希是致命错误：每轮都变，记忆永远为 0。"""
    first = {"messages": [{"role": "user", "content": "我叫阿岚"}]}
    later = {"messages": [
        {"role": "user", "content": "我叫阿岚"},
        {"role": "assistant", "content": "记住了"},
        {"role": "user", "content": "我叫什么"},
    ]}
    assert identify({}, first)[0] == identify({}, later)[0]


def test_unsafe_chars_cleaned():
    s, _ = identify({"X-Hippo-Session": "  a/b c\n"}, {})
    assert s == "a_b_c"


def test_empty_everything_falls_back_to_default():
    s, how = identify({}, {}, default="main")
    assert (s, how) == ("main", "default")


def test_multimodal_content_text_extracted():
    text = first_user_text([{"role": "user", "content": [
        {"type": "text", "text": "这张图里"},
        {"type": "image_url", "image_url": {"url": "data:..."}},
        {"type": "text", "text": "是什么"},
    ]}])
    assert text == "这张图里是什么"


async def test_ten_turns_keep_same_session(cfg, db_path, conn):
    app = create_app(cfg, store_path=db_path, upstream=unary_fake().transport())
    async with serve(app) as c:
        for i in range(10):
            await c.post("/v1/chat/completions", json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "同一个窗口"}],
            }, headers={"authorization": "Bearer sk-x"})

    sessions = {r["session"] for r in metrics_rows(db_path)}
    assert len(sessions) == 1 and sessions != {None}


async def test_switch_header_isolates_memory(cfg, db_path, conn):
    app = create_app(cfg, store_path=db_path, upstream=unary_fake().transport())
    async with serve(app) as c:
        for sid in ("alice", "bob"):
            await c.post("/v1/chat/completions", json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "你好"}],
            }, headers={"authorization": "Bearer sk-x", "X-Hippo-Session": sid})

    sessions = [r["session"] for r in metrics_rows(db_path)]
    assert sessions == ["alice", "bob"]
