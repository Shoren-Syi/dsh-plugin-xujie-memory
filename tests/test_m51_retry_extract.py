# -*- coding: utf-8 -*-
"""M51 验收：二次抽取只在"抽空了 + 像在定义"时才触发。

成本纪律（主人定的顺序：成本 > 无感 > 效果）：
- 判断是纯本地的（长度 + 数字/定义词），零调用、零延迟；
- 只有确实抽空且像定义，才多花一次模型调用；
- 用户侧无感：仍然在后台抽取任务里，不增加对话延迟。
"""
from __future__ import annotations

import asyncio
import json

import httpx

from hippo.config import Config
from hippo.core.encode import Encoder
from hippo.core.extractor import Extractor, definitional_window
from hippo.core.turns import Window


def _w(text: str) -> Window:
    turn = type("T", (), {"role": "user", "content": text, "idx": 0, "partial": 0})()
    return Window(session="s", idxs=[0], token_est=0, has_pending=False, turns=[turn])


def test_definitional_detects_numbers_and_specs():
    assert definitional_window(_w("封面比例是16:9，配色用莫兰迪，背景灰豆绿色，人物半身像"))
    assert definitional_window(_w("游戏指令的格式要求是姓名、提示词、性别、性格、经历，每人不超过50字"))
    assert not definitional_window(_w("你好呀今天过得怎么样"))


def test_definitional_needs_min_length():
    cfg = Config()
    assert not definitional_window(_w("16:9"), min_chars=cfg.extract.retry_min_chars)


def _completion(payload: dict) -> dict:
    return {"id": "x", "object": "chat.completion", "created": 1, "model": "deepseek-chat",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 60}}


EMPTY = {"events": [], "facts": [], "traits": [], "plans": [], "doubts": [], "affect_delta": {}}
ONE = {"events": [{"raw": "封面比例是16:9", "summary": "用户定封面比例16:9", "keywords": ["封面"],
                   "entities": [], "time_hint": "", "arousal": 0.1, "importance": 3}],
       "facts": [], "traits": [], "plans": [], "doubts": [], "affect_delta": {}}


def _extractor(store, cfg, replies):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(json.dumps(body, ensure_ascii=False))
        return httpx.Response(200, json=_completion(replies[min(len(calls) - 1, len(replies) - 1)]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Extractor(store, cfg, Encoder(cfg, client, "sk-test")), client, calls


def test_second_pass_fires_only_for_definitional_zero_event_window(store, cfg):
    cfg.extract.every_n_turns = 1
    cfg.extract.retry_empty = True
    cfg.extract.prescreen_mode = "off"
    store.add_turns("s", [("user", "封面比例是16:9，配色用莫兰迪，背景灰豆绿色，人物半身像", 0),
                          ("assistant", "好的", 0)], ts=1_760_000_000)
    ex, client, calls = _extractor(store, cfg, [EMPTY, ONE])
    try:
        rep = asyncio.run(ex.run("s", force=True))
    finally:
        asyncio.run(client.aclose())
    assert len(calls) == 2, "第一次抽空 + 像定义 → 应该补一次"
    assert rep.events >= 1, "二次抽取应该拿到事件"
    assert "敲定的东西" in calls[1] or "只找" in calls[1]


def test_no_second_pass_for_casual_window(store, cfg):
    cfg.extract.every_n_turns = 1
    cfg.extract.retry_empty = True
    cfg.extract.prescreen_mode = "off"
    store.add_turns("s", [("user", "早上好呀", 0), ("assistant", "早", 0)], ts=1_760_000_000)
    ex, client, calls = _extractor(store, cfg, [EMPTY, ONE])
    try:
        asyncio.run(ex.run("s", force=True))
    finally:
        asyncio.run(client.aclose())
    assert len(calls) == 1, "寒暄窗口不该多花一次调用"
