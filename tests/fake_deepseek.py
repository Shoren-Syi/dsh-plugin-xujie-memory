"""测试用的假 DeepSeek。

比抓真包更有价值的地方：它可以精确构造那些真接口不常出现的边界
（零 usage、空 delta、非法 JSON 行、上游 429），这些才是代理最容易崩的地方。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

import httpx

USAGE = {"prompt_tokens": 1024, "completion_tokens": 231, "total_tokens": 1255,
         "prompt_cache_hit_tokens": 768, "prompt_cache_miss_tokens": 256}


def sse(*chunks: dict[str, Any], usage: dict[str, Any] | None = None,
        done: bool = True, ending: str = "") -> str:
    """把若干 delta 拼成标准 SSE 文本。"""
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps(c, ensure_ascii=False))
    if usage is not None:
        lines.append("data: " + json.dumps(
            {"id": "chatcmpl-x", "object": "chat.completion.chunk", "model": "deepseek-chat",
             "choices": [], "usage": usage}, ensure_ascii=False))
    if done:
        lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n" + ending


def delta(content: str, *, finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-x", "object": "chat.completion.chunk",
        "created": 1, "model": "deepseek-chat",
        "choices": [{"index": 0, "delta": {"content": content},
                     "finish_reason": finish}],
    }


def completion(text: str, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-x", "object": "chat.completion", "created": 1,
        "model": "deepseek-chat",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": usage or USAGE,
    }


class FakeDeepSeek:
    """记录每次收到的请求，方便断言"透传到底改没改"。"""

    def __init__(self, *, status: int = 200, body: str | None = None,
                 json_body: dict[str, Any] | None = None):
        self.status = status
        self.body = body
        self.json_body = json_body
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.json_body is not None:
                return httpx.Response(self.status, json=self.json_body,
                                      headers={"content-type": "application/json"})
            if self.body is not None:
                return httpx.Response(self.status, content=self.body.encode("utf-8"),
                                      headers={"content-type": "text/event-stream"})
            return httpx.Response(self.status, json={"error": {"message": "no body"}})

        return httpx.MockTransport(handler)

    def last_json(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content.decode("utf-8"))


def streaming_fake(n: int = 12) -> FakeDeepSeek:
    chunks = "".join(f"字{i}" for i in range(n))
    return FakeDeepSeek(body=sse(*[delta(c) for c in chunks], usage=USAGE))


def unary_fake(text: str = "你好，我记得你。") -> FakeDeepSeek:
    return FakeDeepSeek(json_body=completion(text))
