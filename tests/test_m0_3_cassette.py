"""M0.3.3 录制回放验收。

关键点不是"能存能读"，而是：
  - 回放时**零网络**（断网也能跑）
  - 未命中时的报错**能指导人补录**
  - 换 api key 不该导致重录（否则 CI 上换个 secret 就全红）
"""

from __future__ import annotations

import json

import httpx
import pytest

from hippo.testing.cassette import Cassette, CassetteMiss
from tests.fake_deepseek import FakeDeepSeek, sse, delta, completion, USAGE


def _req(api_key: str = "sk-a", text: str = "你好") -> httpx.Request:
    return httpx.Request(
        "POST", "https://api.deepseek.com/v1/chat/completions",
        headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
        content=json.dumps({"model": "deepseek-chat",
                            "messages": [{"role": "user", "content": text}]}).encode(),
    )


def test_record_then_replay_without_network(tmp_path):
    fake = FakeDeepSeek(json_body=completion("记得"))
    cassette = Cassette(tmp_path, record=True, inner=fake.transport())

    c1 = httpx.Client(transport=cassette.transport())
    r1 = c1.post("https://api.deepseek.com/v1/chat/completions",
                 json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "你好"}]},
                 headers={"authorization": "Bearer sk-a"})
    assert r1.status_code == 200
    assert len(fake.requests) == 1
    assert cassette.size() == 1

    # 再录一盒会命中已有文件，不该重复调用
    c2 = httpx.Client(transport=cassette.transport())
    r2 = c2.post("https://api.deepseek.com/v1/chat/completions",
                 json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "你好"}]})
    assert r2.json()["choices"][0]["message"]["content"] == "记得"
    assert len(fake.requests) == 1, "回放不该再打上游"


def test_replay_survives_api_key_change(tmp_path):
    fake = FakeDeepSeek(json_body=completion("ok"))
    Cassette(tmp_path, record=True, inner=fake.transport()).transport()

    c = httpx.Client(transport=Cassette(tmp_path, record=True, inner=fake.transport()).transport())
    c.post("https://api.deepseek.com/v1/chat/completions",
           json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "x"}]},
           headers={"authorization": "Bearer sk-OLD"})

    # 换 key、换 host header，仍然命中同一个文件
    replay = Cassette(tmp_path, record=False)
    c2 = httpx.Client(transport=replay.transport())
    r = c2.post("https://api.deepseek.com/v1/chat/completions",
                json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "x"}]},
                headers={"authorization": "Bearer sk-NEW"})
    assert r.status_code == 200
    assert replay.hits == 1


def test_miss_raises_actionable_error(tmp_path):
    cassette = Cassette(tmp_path, record=False)
    c = httpx.Client(transport=cassette.transport())
    with pytest.raises(CassetteMiss) as ei:
        c.post("https://api.deepseek.com/v1/chat/completions",
               json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "没录过"}]})
    msg = str(ei.value)
    assert "补录" in msg and "HIPPO_RECORD=1" in msg
    assert "model=deepseek-chat" in msg


def test_stream_body_roundtrip(tmp_path):
    body = sse(*[delta(x) for x in "你好世界"], usage=USAGE)
    fake = FakeDeepSeek(body=body)
    Cassette(tmp_path, record=True, inner=fake.transport()).transport()

    c = httpx.Client(transport=Cassette(tmp_path, record=True, inner=fake.transport()).transport())
    c.post("https://api.deepseek.com/v1/chat/completions",
           json={"model": "deepseek-chat", "stream": True,
                 "messages": [{"role": "user", "content": "讲个故事"}]})

    replay = Cassette(tmp_path, record=False)
    c2 = httpx.Client(transport=replay.transport())
    r = c2.post("https://api.deepseek.com/v1/chat/completions",
                json={"model": "deepseek-chat", "stream": True,
                      "messages": [{"role": "user", "content": "讲个故事"}]})
    assert r.text == body
    assert r.headers["content-type"].startswith("text/event-stream")


def test_different_messages_different_files(tmp_path):
    fake = FakeDeepSeek(json_body=completion("a"))
    cassette = Cassette(tmp_path, record=True, inner=fake.transport())
    c = httpx.Client(transport=cassette.transport())
    for text in ("第一句", "第二句"):
        c.post("https://api.deepseek.com/v1/chat/completions",
               json={"model": "deepseek-chat", "messages": [{"role": "user", "content": text}]})
    assert cassette.size() == 2


def test_record_file_is_human_readable(tmp_path):
    fake = FakeDeepSeek(json_body=completion("嗯"))
    Cassette(tmp_path, record=True, inner=fake.transport()).transport()
    c = httpx.Client(transport=Cassette(tmp_path, record=True, inner=fake.transport()).transport())
    c.post("https://api.deepseek.com/v1/chat/completions",
           json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "x"}]})

    f = next(tmp_path.glob("*.json"))
    rec = json.loads(f.read_text(encoding="utf-8"))
    assert rec["request"]["model"] == "deepseek-chat"
    assert rec["request"]["turns"] == 1
    assert "recorded_at" in rec
