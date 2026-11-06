# -*- coding: utf-8 -*-
"""M39 验收：首次抽取门槛。

真实使用里"连发 5 条用户消息"很罕见——用户聊了半天，库里一条记忆都没有，
界面上看着就像插件没工作。所以会话的第一次抽取只要求 2 条用户消息。
"""
from __future__ import annotations

import json

import httpx

from hippo.config import Config
from hippo.core.encode import Encoder
from hippo.core.extractor import Extractor, extract_threshold

GOOD = {
    "events": [{"raw": "我住在深圳", "summary": "用户住在深圳", "keywords": ["深圳"],
                "entities": [], "time_hint": "", "arousal": 0.1}],
    "facts": [{"entity": "用户", "key": "city", "value": "深圳"}],
    "traits": [], "plans": [], "doubts": [],
    "affect_delta": {},
}


def _completion(text: str) -> dict:
    return {
        "id": "x", "object": "chat.completion", "created": 1, "model": "deepseek-chat",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 500, "completion_tokens": 80},
    }


def _extractor(store, cfg):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(json.dumps(GOOD, ensure_ascii=False)))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Extractor(store, cfg, Encoder(cfg, client, "sk-test")), client


def _add(store, n_user: int, *, session="s"):
    items = []
    for i in range(n_user):
        items.append(("user", f"我住在深圳第{i}次说", 0))
        items.append(("assistant", "嗯。", 0))
    store.add_turns(session, items, ts=1_760_000_000)
    return items


def test_threshold_helper():
    cfg = Config()
    cfg.extract.every_n_turns = 5
    cfg.extract.first_extract_user_turns = 2
    assert extract_threshold(cfg, first=True) == 2
    assert extract_threshold(cfg, first=False) == 5
    # 首抽门槛配得比常规门槛还大时，不能反而更难
    cfg.extract.first_extract_user_turns = 9
    assert extract_threshold(cfg, first=True) == 5


def test_first_extraction_needs_only_two_user_turns(store, cfg):
    cfg.extract.every_n_turns = 5
    cfg.extract.first_extract_user_turns = 2
    _add(store, 2)
    ex, client = _extractor(store, cfg)
    try:
        rep = __import__("asyncio").run(ex.run("s"))
    finally:
        __import__("asyncio").run(client.aclose())
    assert rep.results, "两条用户消息就该触发首次抽取"
    assert rep.events >= 1


def test_one_user_turn_does_not_extract(store, cfg):
    cfg.extract.every_n_turns = 5
    cfg.extract.first_extract_user_turns = 2
    _add(store, 1)
    ex, client = _extractor(store, cfg)
    try:
        rep = __import__("asyncio").run(ex.run("s"))
    finally:
        __import__("asyncio").run(client.aclose())
    assert not rep.results, "只有一条用户消息时窗口不完整/不够门槛，不该抽"


def test_after_first_extraction_uses_regular_threshold(store, cfg):
    cfg.extract.every_n_turns = 5
    cfg.extract.first_extract_user_turns = 2
    _add(store, 2)
    ex, client = _extractor(store, cfg)
    try:
        __import__("asyncio").run(ex.run("s"))
        assert store.extracted_turn_count("s") > 0
        # 再补 2 条：已经抽过一次，应该回到 5 条的门槛，所以不抽
        _add(store, 2)
        rep = __import__("asyncio").run(ex.run("s"))
    finally:
        __import__("asyncio").run(client.aclose())
    assert not rep.results
