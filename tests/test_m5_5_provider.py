"""M5.5 验收：多上游适配（thinking 开关 / 缓存计费 / 推理 token）。

这一层的价值全在"不发错字段"和"算对账"上，而这两件事**都不会报错**：
  - 多发一个上游不认的字段 → 400，立刻发现（还算好）
  - 漏记缓存命中 → 成本高估 50 倍，永远发现不了
  - 思考开关没生效 → 推理 token 白花，只有看 usage 才知道

所以每个断言都盯着"某个数字必须恰好是多少"，而不是"没抛异常"。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from hippo.core.encode import Encoder, _cached_of, _reasoning_of
from hippo.core.telemetry import Span
from hippo.core.turns import Turn, Window


def _body(usage: dict[str, Any], content: str | None = None) -> dict[str, Any]:
    return {
        "id": "x", "object": "chat.completion", "created": 1, "model": "m",
        "choices": [{"index": 0,
                     "message": {"role": "assistant",
                                 "content": content or json.dumps({"events": []})},
                     "finish_reason": "stop"}],
        "usage": usage,
    }


def _enc(cfg, usage):
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json=_body(usage))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Encoder(cfg, client, "sk-test"), seen


def _win():
    return Window(session="s", idxs=[0], token_est=100, has_pending=False,
                  turns=[Turn(session="s", role="user", content="我昨天搬家了", idx=0, ts=1)])


# ------------------------------------------------------------------ thinking
def test_official_flash_disables_thinking_for_extraction(cfg):
    enc, seen = _enc(cfg, {"prompt_tokens": 10, "completion_tokens": 5})
    payload = enc._payload([{"role": "user", "content": "x"}])
    assert payload["thinking"] == {"type": "disabled"}


def test_thinking_field_absent_for_other_providers(cfg):
    cfg.deepseek.base_url = "https://other.example"
    enc, _ = _enc(cfg, {})
    assert "thinking" not in enc._payload([])


def test_thinking_disabled_is_sent(cfg):
    cfg.deepseek.thinking = "disabled"
    enc, _ = _enc(cfg, {"prompt_tokens": 10, "completion_tokens": 5})
    assert enc._payload([])["thinking"] == {"type": "disabled"}


def test_thinking_garbage_value_falls_back_to_absent(cfg):
    """配错（"off"/"false"/"关"）不能把请求打挂，退回不发的行为。"""
    for bad in ("off", "false", "关", "  ", "auto"):
        cfg.deepseek.thinking = bad
        enc, _ = _enc(cfg, {})
        assert "thinking" not in enc._payload([]), bad


# ------------------------------------------------------------------ usage 解析
def test_cached_tokens_from_mimo_shape():
    usage = {"prompt_tokens": 1549,
             "prompt_tokens_details": {"cached_tokens": 1536}}
    assert _cached_of(usage) == 1536


def test_cached_tokens_from_deepseek_shape():
    assert _cached_of({"prompt_tokens": 100, "prompt_cache_hit_tokens": 64}) == 64


def test_cached_tokens_null_details():
    """mimo 冷启动时 prompt_tokens_details 是 null 而不是 {}。"""
    assert _cached_of({"prompt_tokens": 1549, "prompt_tokens_details": None}) == 0


def test_reasoning_tokens():
    assert _reasoning_of(
        {"completion_tokens_details": {"reasoning_tokens": 1754}}) == 1754
    assert _reasoning_of({"completion_tokens": 5}) == 0
    assert _reasoning_of({"completion_tokens_details": None}) == 0


# ------------------------------------------------------------------ 记账
def test_cost_uses_hit_price(cfg):
    """命中缓存必须按命中价算。mimo 命中价是未命中的 1/50。"""
    cfg.pricing.input_miss_per_m = 1.0
    cfg.pricing.input_hit_per_m = 0.02
    cfg.pricing.output_per_m = 2.0

    span = Span()
    span.absorb_usage({"prompt_tokens": 1549, "completion_tokens": 800,
                       "prompt_tokens_details": {"cached_tokens": 1536}})
    # (1549-1536)*1.0 + 1536*0.02 + 800*2.0 = 13 + 30.72 + 1600 = 1643.72 /1e6
    assert abs(span.cost(cfg.pricing.as_dict()) - 0.00164372) < 1e-9


def test_cost_without_cache_is_50x_higher(cfg):
    """这条不是测算法，是测"漏记账有多贵"——把它写死成测试，
    以后谁再把 cached_tokens 漏掉，数字会立刻变形。"""
    cfg.pricing.input_miss_per_m = 1.0
    cfg.pricing.input_hit_per_m = 0.02
    cfg.pricing.output_per_m = 2.0
    usage = {"prompt_tokens": 1549, "completion_tokens": 0,
             "prompt_tokens_details": {"cached_tokens": 1536}}

    hit = Span()
    hit.absorb_usage(usage)
    miss = Span()
    miss.absorb_usage({"prompt_tokens": 1549, "completion_tokens": 0})

    ratio = miss.cost(cfg.pricing.as_dict()) / hit.cost(cfg.pricing.as_dict())
    assert 30 < ratio < 60, f"未命中/命中 的倍数异常：{ratio}"


# ------------------------------------------------------------------ 端到端记账
async def test_encode_accumulates_cache_across_retry(cfg):
    """重试时两次调用都花了钱。只记最后一次 = 重试率上升在报表里隐形。"""
    cfg.extract.retries = 1
    bodies = [
        {"prompt_tokens": 1000, "completion_tokens": 50,
         "prompt_tokens_details": {"cached_tokens": 900}},
        {"prompt_tokens": 1200, "completion_tokens": 60,
         "prompt_tokens_details": {"cached_tokens": 1000}},
    ]
    contents = ["不是 json", json.dumps({"events": []})]
    n = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = n["i"]
        n["i"] += 1
        return httpx.Response(200, json=_body(bodies[i], contents[i]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await Encoder(cfg, client, "sk").encode(_win())
    assert out.status == "ok"
    assert out.attempts == 2
    assert out.prompt_tokens == 2200      # 累加，不是覆盖
    assert out.cached_tokens == 1900
    assert out.cost > 0


async def test_reasoning_tokens_recorded(cfg):
    enc, seen = _enc(cfg, {"prompt_tokens": 10, "completion_tokens": 20,
                           "completion_tokens_details": {"reasoning_tokens": 15}})
    out = await enc.encode(_win())
    assert out.reasoning_tokens == 15
    assert len(seen) == 1


# ------------------------------------------------------------------ 记账完整性
def test_record_run_persists_all_token_columns(store):
    """extract_runs 的 token 列必须真的被写。

    这里锁的是一个真实事故：RunResult 里没有 prompt_tokens 字段，
    _record() 就也没传，于是 extract_runs.prompt_tokens 恒为 0。
    而 cost 是另一个字段算出来的，所以报表看着正常——
    直到要算缓存命中率，才发现分母是 0。
    """
    store.record_run(session="s", start_idx=0, end_idx=9, status="ok",
                     prompt_tokens=1800, completion_tokens=800,
                     cached_tokens=1792, reasoning_tokens=0, cost=0.001)

    st = store.run_stats("s")
    assert st["prompt_tokens"] == 1800
    assert st["cached_tokens"] == 1792
    assert abs(st["cache_hit_rate"] - 1792 / 1800) < 1e-4


def test_run_stats_cache_rate_is_zero_when_no_tokens(store):
    """没有调用过（全是 skipped）时，命中率是 0 而不是崩掉。"""
    store.record_run(session="s", start_idx=0, end_idx=9, status="skipped")
    st = store.run_stats("s")
    assert st["prompt_tokens"] == 0
    assert st["cache_hit_rate"] == 0.0


async def test_run_result_carries_token_counts(cfg, store):
    """从 EncodeOutcome 到 RunResult 再到库，一条链上三个字段都不能掉。"""
    from hippo.core.extractor import Extractor
    from hippo.core.turns import Turn

    usage = {"prompt_tokens": 1719, "completion_tokens": 640,
             "prompt_tokens_details": {"cached_tokens": 1664}}
    enc, _ = _enc(cfg, usage)
    ext = Extractor(store, cfg, enc, rules=None)

    w = Window(session="s", idxs=[0, 1], token_est=100, has_pending=False,
               turns=[Turn(session="s", role="user", content="我昨天搬家了", idx=0, ts=1),
                      Turn(session="s", role="assistant", content="新家好吗", idx=1, ts=2)])
    await ext._one(type("R", (), {"results": []})(), "s", w, "（暂无）")

    st = store.run_stats("s")
    assert st["prompt_tokens"] == 1719
    assert st["cached_tokens"] == 1664
    assert st["success_rate"] == 1.0


# ------------------------------------------------------------------ 自动迁移
def test_open_auto_migrates_old_db(tmp_path):
    """老库打开就升到最新。

    这条锁的是一个真实故障：用户升级包之后，库里缺新列，
    `run_stats()` 抛 `no such column`，而 CLI 里 `except: pass` 吃掉它，
    于是"抽取统计"整段从报表里消失——看着像"这个库没抽过"。
    功能静默变少，比崩溃难查得多。
    """
    from hippo.core.db import connect
    from hippo.core.migrate import current_version, migrate
    from hippo.core.store import Store

    db = tmp_path / "old.db"
    conn = connect(str(db))
    migrate(conn, target=3)          # 造一个 v3 的老库
    assert current_version(conn) == 3
    conn.close()

    with Store.open(db) as s:
        # 打开的动作本身就该把它升上去
        assert current_version(s.conn) >= 4
        # 而且新列必须可用，否则等于没升
        st = s.run_stats()
        assert st["cache_hit_rate"] == 0.0


def test_open_is_idempotent_on_fresh_db(tmp_path):
    """已经是新库时，open 不该反复迁移（也不该报错）。"""
    from hippo.core.db import connect
    from hippo.core.migrate import current_version, migrate
    from hippo.core.store import Store

    db = tmp_path / "new.db"
    conn = connect(str(db))
    migrate(conn)
    conn.close()

    for _ in range(3):
        with Store.open(db) as s:
            v = current_version(s.conn)
    assert v >= 4
