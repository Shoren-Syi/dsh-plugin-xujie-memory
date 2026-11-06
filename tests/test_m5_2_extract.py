"""M5.2 验收：调用、重试、降级、幂等。

全部零网络零成本——`httpx.MockTransport` 顶替上游。
真实模型的行为差异（它会不会遵守规则 1）不在这里验，那要靠 cassette 真机录一次，
见 R-5.2 §"未验证"。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from hippo.core.encode import (
    Encoder, build_conversation, build_known_entities, degrade_event_text,
)
from hippo.core.persist import persist_degraded
from hippo.core.turns import Window, slice_windows

GOOD = {
    "events": [{
        "raw": "我昨天下午搬家了",
        "summary": "用户昨天下午搬家",
        "keywords": ["搬家", "下午"],
        "entities": [{"name": "老王", "type": "person"}],
        "event_time_hint": "昨天下午",
        "arousal": 0.3,
    }],
    "facts": [], "traits": [], "plans": [], "doubts": [],
    "affect_delta": {"joy": 2},
}


def _completion(text: str) -> dict[str, Any]:
    return {
        "id": "x", "object": "chat.completion", "created": 1, "model": "deepseek-chat",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 120},
    }


def _encoder(cfg, respond):
    """respond(body, call_no) -> str | Exception"""
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body)
        out = respond(body, len(calls))
        if isinstance(out, Exception):
            raise out
        return httpx.Response(200, json=_completion(out))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Encoder(cfg, client, "sk-test"), calls


def _window(store, *, session="s", turns=5):
    texts = []
    for i in range(turns):
        texts.append(("user", f"我昨天下午搬家了，第{i}次说"))
        texts.append(("assistant", "好的，记住了。"))
    store.add_turns(session, [(r, c, 0) for r, c in texts], ts=1_760_000_000)
    wins = slice_windows(store.pending_turns(session), session=session, per_window=5)
    return wins[0]


# ---------------------------------------------------------------- 正常路径
async def test_encode_ok(cfg, store):
    w = _window(store)
    enc, calls = _encoder(cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    out = await enc.encode(w)
    assert out.ok and out.status == "ok"
    assert out.result.events[0].keywords == ["搬家", "下午"]
    assert out.attempts == 1
    assert out.prompt_tokens == 900 and out.completion_tokens == 120
    assert out.cost > 0
    assert out.latency_ms >= 0
    assert len(calls) == 1


async def test_request_shape(cfg, store):
    """参数必须精确：温度 0.2（稳定）、非流式、json_object。"""
    w = _window(store)
    enc, calls = _encoder(cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    await enc.encode(w)
    body = calls[0]
    assert body["temperature"] == 0.2
    assert body["stream"] is False
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 1500
    assert body["model"] == cfg.deepseek.extract_model
    assert "json" in body["messages"][0]["content"].lower()


async def test_prompt_carries_entities(cfg, store):
    w = _window(store)
    enc, calls = _encoder(cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    await enc.encode(w, known="- 老王（别名：王哥）")
    assert "老王（别名：王哥）" in calls[0]["messages"][0]["content"]


# ---------------------------------------------------------------- 重试
async def test_retry_once_on_bad_json(cfg, store):
    w = _window(store)
    seq = ["这不是 JSON", json.dumps(GOOD, ensure_ascii=False)]
    enc, calls = _encoder(cfg, lambda b, n: seq[n - 1])
    out = await enc.encode(w)
    assert out.ok and out.attempts == 2
    # 第二次请求必须把错误输出放进 assistant 位，模型才知道自己错在哪
    msgs = calls[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert "不是合法的 json" in msgs[-1]["content"]


async def test_retry_on_fenced_json(cfg, store):
    """围栏不是错误，不该浪费一次重试。"""
    w = _window(store)
    enc, calls = _encoder(
        cfg, lambda b, n: "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```")
    out = await enc.encode(w)
    assert out.ok and out.attempts == 1 and len(calls) == 1


async def test_degrade_after_two_failures(cfg, store):
    w = _window(store)
    enc, calls = _encoder(cfg, lambda b, n: "抱歉，我无法完成。")
    out = await enc.encode(w)
    assert out.status == "degraded"
    assert out.attempts == 2 and len(calls) == 2
    assert out.error and "JSON" in out.error.upper()
    # 两次调用都烧了 token，成本要累加，不能只算最后一次
    assert out.prompt_tokens == 1800


async def test_no_retry_on_timeout(cfg, store):
    """超时不重试：上游已经过载，再等 30s 只会让积压更严重。"""
    w = _window(store)
    enc, calls = _encoder(cfg, lambda b, n: httpx.ReadTimeout("boom"))
    out = await enc.encode(w)
    assert out.status == "degraded"
    assert out.attempts == 1 and len(calls) == 1
    assert "timeout" in out.error


async def test_no_retry_on_http_error(cfg, store):
    w = _window(store)

    def handler(request):
        return httpx.Response(429, json={"error": {"message": "rate limit"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    enc = Encoder(cfg, client, "sk-test")
    out = await enc.encode(w)
    assert out.status == "degraded" and out.attempts == 1
    assert "429" in out.error


async def test_bad_shape_is_degraded(cfg, store):
    """200 但没有 choices —— 上游偶发会吐这种东西。"""
    w = _window(store)

    def handler(request):
        return httpx.Response(200, json={"id": "x", "choices": []})

    enc = Encoder(cfg, httpx.AsyncClient(transport=httpx.MockTransport(handler)), "sk")
    out = await enc.encode(w)
    assert out.status == "degraded"
    assert "choices" in out.error


# ---------------------------------------------------------------- 降级落库
def test_degrade_writes_raw_event(store):
    w = _window(store)
    eid = persist_degraded(store, w.session, w, tokens=["搬家", "老王"])
    ev = store.get_event(eid)
    assert ev.kind == "raw" and ev.tier == 3
    assert ev.summary == "", "降级事件不能有摘要——摘要一定是模型编的"
    assert "我昨天下午搬家了" in ev.raw
    assert ev.keywords == "搬家 老王"


def test_degrade_text_is_verbatim(store):
    w = _window(store)
    d = degrade_event_text(w, ["a"])
    assert d["raw"] == w.text()
    assert d["tier"] == 3 and d["kind"] == "raw"


def test_degrade_empty_window(store):
    w = Window(session="s", idxs=[0, 1], token_est=1, has_pending=False, turns=[])
    assert persist_degraded(store, "s", w) is None


# ---------------------------------------------------------------- 幂等
def test_extracted_turns_not_rescanned(store):
    w = _window(store)
    assert len(w.idxs) > 0
    store.mark_turns_extracted(w.session, w.end_idx)
    assert store.pending_turns(w.session) == []
    assert store.pending_turn_count(w.session) == 0


def test_pending_count_grows(store):
    assert store.pending_turn_count("s") == 0
    _window(store)
    assert store.pending_turn_count("s") == 10


# ---------------------------------------------------------------- 组装细节
def test_conversation_truncates_long_assistant(store):
    store.add_turns("s", [("user", "讲讲历史", 0), ("assistant", "字" * 2000, 0)], ts=1)
    w = slice_windows(store.pending_turns("s"), session="s", per_window=5)[0]
    text = build_conversation(w)
    assert "后略" in text
    assert len(text) < 600


def test_known_entities_format():
    class E:
        def __init__(self, c, a=()):
            self.canonical, self.aliases = c, list(a)

    out = build_known_entities([E("老王", ["王哥"]), E("小李")])
    assert out == "- 老王（别名：王哥）\n- 小李"


def test_known_entities_empty():
    assert build_known_entities([]) == "（暂无）"
    assert build_known_entities([], limit=0) == "（暂无）"


def test_known_entities_limit():
    class E:
        def __init__(self, c):
            self.canonical, self.aliases = c, []

    out = build_known_entities([E(f"人{i}") for i in range(100)], limit=3)
    assert len(out.splitlines()) == 3


# ---------------------------------------------------------------- 端到端（假上游）
async def test_twenty_windows_all_ok(cfg, store):
    """验收项 1：正常窗口成功率 100%（20/20）。

    这里验的是"代码路径不崩"，不是"模型抽得准"。
    后者在 R-5.2 里明确标为未验证。
    """
    ok = 0
    for i in range(20):
        s = f"s{i}"
        _window(store, session=s, turns=5)
        w = slice_windows(store.pending_turns(s), session=s, per_window=5)[0]
        enc, _ = _encoder(cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
        out = await enc.encode(w)
        ok += int(out.ok)
        store.mark_turns_extracted(s, w.end_idx)
    assert ok == 20


def test_parse_result_after_degrade_roundtrip(store):
    """降级写进去的原文，把 raw 拿出来还能被 anchor 回去——
    保证 M15 可以从 raw 事件重抽。"""
    from hippo.core.schema import anchor_raw

    w = _window(store)
    eid = persist_degraded(store, w.session, w)
    ev = store.get_event(eid)
    assert ev.raw == w.text()
    assert anchor_raw("我昨天下午搬家了，第0次说", ev.raw) is not None
