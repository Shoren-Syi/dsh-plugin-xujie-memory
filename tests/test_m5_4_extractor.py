"""M5.4 验收：编排、预筛拦截、代理接线。

三条主线：
  1. 攒够才抽（省钱），但 force 能补尾巴（不漏）
  2. 不完整窗口不抽且不标记（下次还能抽）
  3. 代理里抽取是后台任务，不阻塞响应
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from hippo.adapters.proxy import create_app
from hippo.core.encode import Encoder
from hippo.core.extractor import Extractor
from tests.conftest import serve

GOOD = {
    "events": [{
        "raw": "我昨天下午搬家了",
        "summary": "用户昨天下午搬家",
        "keywords": ["搬家", "下午"],
        "entities": [{"name": "老王"}],
        "event_time_hint": "昨天下午",
        "arousal": 0.3,
    }],
    "facts": [{"entity": "用户", "key": "city", "value": "深圳", "evidence": [0]}],
    "traits": [{"entity": "老王", "trait": "帮过忙", "evidence": [0]}],
    "plans": [], "doubts": [],
    "affect_delta": {"joy": 3},
}


def completion(text: str) -> dict[str, Any]:
    return {
        "id": "x", "object": "chat.completion", "created": 1, "model": "deepseek-chat",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 120},
    }


def make_encoder(cfg, respond):
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body)
        return httpx.Response(200, json=completion(respond(body, len(calls))))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Encoder(cfg, client, "sk-test"), calls


def add_rounds(store, texts, *, session="s", ts=1_760_000_000, last_user_only=False):
    items = []
    for t in texts:
        items.append(("user", t, 0))
        if not last_user_only:
            items.append(("assistant", "嗯。", 0))
    store.add_turns(session, items, ts=ts)
    return items


def make_extractor(store, cfg, respond):
    enc, calls = make_encoder(cfg, respond)
    return Extractor(store, cfg, enc), calls


# ---------------------------------------------------------------- 攒够才抽
async def test_not_enough_turns_no_call(store, cfg):
    add_rounds(store, ["我昨天下午搬家了从朝阳搬到海淀", "老王说他下周要去深圳出差"])
    ex, calls = make_extractor(store, cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    rep = await ex.run("s")
    assert rep.results == []
    assert calls == [], "不足 5 轮就调模型 = 每轮都在花钱"


async def test_force_extracts_tail(store, cfg):
    add_rounds(store, ["我昨天下午搬家了从朝阳搬到海淀", "老王说他下周要去深圳出差"])
    ex, calls = make_extractor(store, cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    rep = await ex.run("s", force=True)
    assert len(rep.results) == 1 and rep.results[0].status == "ok"
    assert len(calls) == 1


async def test_five_rounds_triggers(store, cfg):
    add_rounds(store, [f"我第{i}次说我昨天下午搬家了" for i in range(5)])
    ex, calls = make_extractor(store, cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    rep = await ex.run("s")
    assert len(rep.results) == 1 and len(calls) == 1
    assert rep.results[0].events == 1


async def test_end_to_end_persists(store, cfg):
    add_rounds(store, [f"我第{i}次说我昨天下午搬家了" for i in range(5)])
    ex, _ = make_extractor(store, cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    await ex.run("s")
    c = store.counts()
    assert c["events"] == 1 and c["facts"] == 1 and c["card_traits"] == 1
    assert c["entities"] == 2, "用户 + 老王"
    assert c["extract_runs"] == 1
    assert store.pending_turn_count("s") == 0


# ---------------------------------------------------------------- 不完整窗口
async def test_incomplete_window_not_extracted(store, cfg):
    """最后一条是 user（AI 还没回）→ 不抽、不标记。"""
    add_rounds(store, [f"我第{i}次说我昨天下午搬家了，从朝阳搬到海淀" for i in range(6)],
                last_user_only=True)
    ex, calls = make_extractor(store, cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    rep = await ex.run("s", force=True)
    # 前 5 轮完整（10 条），尾巴 1 条 user 不完整
    statuses = [(r.start_idx, r.end_idx, r.status, r.reason) for r in rep.results]
    assert any(r.status == "skipped" and r.reason == "incomplete" for r in rep.results), statuses
    # 尾巴不能被标记
    assert store.pending_turn_count("s") > 0


async def test_marked_after_success(store, cfg):
    add_rounds(store, [f"我第{i}次说我昨天下午搬家了" for i in range(5)])
    ex, _ = make_extractor(store, cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    await ex.run("s")
    assert store.pending_turn_count("s") == 0
    assert all(t.extracted_at for t in store.get_turns("s", 100))


# ---------------------------------------------------------------- 预筛拦截
async def test_prescreen_blocks_chitchat(store, cfg):
    """5 轮寒暄 → 0 次调用、0 成本，但**要标记已抽**，否则永远卡着。"""
    add_rounds(store, ["早", "嗯", "哈哈", "好的", "行"])
    ex, calls = make_extractor(store, cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    rep = await ex.run("s")
    assert calls == []
    assert rep.results[0].status == "skipped"
    assert rep.results[0].reason == "prescreen"
    assert store.pending_turn_count("s") == 0


async def test_prescreen_passes_on_one_good_turn(store, cfg):
    """5 轮里只有 1 轮有信息 → 抽整窗（不是只抽那一句）。"""
    add_rounds(store, ["早", "嗯", "我在字节做后端，天天加班", "哈哈", "行"])
    ex, calls = make_extractor(store, cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    await ex.run("s")
    assert len(calls) == 1
    # 整窗 5 轮都在 prompt 里
    prompt = calls[0]["messages"][0]["content"]
    assert "我在字节做后端" in prompt and "早" in prompt


async def test_prescreen_disabled(store, cfg):
    cfg.extract.prescreen = False
    add_rounds(store, ["早", "嗯", "哈哈", "好的", "行"])
    ex, calls = make_extractor(store, cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    await ex.run("s")
    assert len(calls) == 1


# ---------------------------------------------------------------- 降级
async def test_degrade_writes_raw_and_marks(store, cfg):
    add_rounds(store, [f"我第{i}次说我昨天下午搬家了" for i in range(5)])
    ex, calls = make_extractor(store, cfg, lambda b, n: "抱歉，我做不到。")
    rep = await ex.run("s")
    assert rep.results[0].status == "degraded"
    assert len(calls) == 2
    ev = store.get_events([1])[0]
    assert ev.kind == "raw" and ev.tier == 3
    assert "搬家" in ev.raw
    # 降级也要标记：再抽只会再失败一次，白烧钱
    assert store.pending_turn_count("s") == 0


async def test_degrade_off(store, cfg):
    cfg.extract.degrade_to_raw = False
    add_rounds(store, [f"我第{i}次说我昨天下午搬家了" for i in range(5)])
    ex, _ = make_extractor(store, cfg, lambda b, n: "垃圾输出")
    await ex.run("s")
    assert store.counts()["events"] == 0


async def test_persist_failure_not_marked(store, cfg, monkeypatch):
    """入库失败 → 不标记，下次重来。这是"不漏"的最后一道保险。"""
    add_rounds(store, [f"我第{i}次说我昨天下午搬家了" for i in range(5)])

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr("hippo.core.extractor.persist_result", boom)
    ex, _ = make_extractor(store, cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    rep = await ex.run("s")
    assert rep.results[0].status == "error"
    assert store.pending_turn_count("s") > 0, "写库失败却标记已抽 = 这段对话永远丢了"


# ---------------------------------------------------------------- 幂等
async def test_second_run_no_work(store, cfg):
    add_rounds(store, [f"我第{i}次说我昨天下午搬家了" for i in range(5)])
    ex, calls = make_extractor(store, cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    await ex.run("s")
    rep2 = await ex.run("s")
    assert rep2.results == [] and len(calls) == 1


async def test_two_windows_in_one_run(store, cfg):
    add_rounds(store, [f"我第{i}次说我昨天下午搬家了" for i in range(10)])
    ex, calls = make_extractor(store, cfg, lambda b, n: json.dumps(GOOD, ensure_ascii=False))
    rep = await ex.run("s")
    assert len(rep.results) == 2 and len(calls) == 2


# ---------------------------------------------------------------- 代理接线
def _extract_aware_upstream():
    """一个同时应付"聊天"和"抽取"的假上游。"""
    seen_extract = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("response_format", {}).get("type") == "json_object":
            seen_extract["n"] += 1
            return httpx.Response(200, json=completion(json.dumps(GOOD, ensure_ascii=False)))
        return httpx.Response(200, json=completion("好的，记住了。"))

    return httpx.MockTransport(handler), seen_extract


async def test_proxy_extracts_after_five_turns(cfg, db_path, conn):
    transport, seen = _extract_aware_upstream()
    app = create_app(cfg, store_path=db_path, upstream=transport)
    async with serve(app) as c:
        for i in range(5):
            r = await c.post("/v1/chat/completions", json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": f"我第{i}次说我昨天下午搬家了"}],
            }, headers={"authorization": "Bearer sk-x",
                        "X-Hippo-Session": "main"})
            assert r.status_code == 200
    # serve 退出时 lifespan 会 drain 未完成的抽取任务
    from hippo.core.store import Store

    with Store.open(db_path) as s:
        assert seen["n"] >= 1, "5 轮之后应该触发抽取"
        assert s.counts()["events"] >= 1
        assert s.pending_turn_count("main") == 0


async def test_proxy_extract_disabled(cfg, db_path, conn):
    cfg.extract.enabled = False
    transport, seen = _extract_aware_upstream()
    app = create_app(cfg, store_path=db_path, upstream=transport)
    async with serve(app) as c:
        for i in range(5):
            await c.post("/v1/chat/completions", json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": f"我第{i}次说我昨天下午搬家了"}],
            }, headers={"authorization": "Bearer sk-x",
                        "X-Hippo-Session": "main"})
    assert seen["n"] == 0


async def test_response_not_blocked_by_extract(cfg, db_path, conn):
    """抽取失败不能影响聊天响应码。"""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("response_format"):
            raise httpx.ConnectError("抽取专用上游挂了")
        return httpx.Response(200, json=completion("好的"))

    app = create_app(cfg, store_path=db_path, upstream=httpx.MockTransport(handler))
    async with serve(app) as c:
        for i in range(5):
            r = await c.post("/v1/chat/completions", json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": f"我第{i}次说我昨天下午搬家了"}],
            }, headers={"authorization": "Bearer sk-x",
                        "X-Hippo-Session": "main"})
            assert r.status_code == 200, "抽取挂了不能把聊天拖下水"
