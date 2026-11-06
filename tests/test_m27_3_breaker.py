"""M27.3 预算熔断验收。

九条用例对应手册的四项 DoD：
  超限即降级（下一轮生效） / 降级后仍可用 / 次日 00:00 自动恢复 / 状态可见

另外四条是设计边界的护栏，不是手册要求：
  0 = 关掉熔断 / on_exceed=off 只告警 / 重置不清账 / 抽取也计入
"""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from hippo.adapters.proxy import create_app
from hippo.core import budget
from hippo.core.pack import pack
from hippo.core.store import Store
from tests.conftest import serve
from tests.fake_deepseek import FakeDeepSeek, completion

TZ = "Asia/Shanghai"


def _ts(y, m, d, hh=12, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(TZ)).timestamp())


@pytest.fixture()
def wired(store, cfg):
    store.attach_config(cfg)
    return store


# ------------------------------------------------------------------ 计数
def test_day_bounds_use_local_midnight():
    # 23:30 的"今天"仍从当天 00:00 开始，不是"最近 24 小时"
    t = _ts(2026, 11, 6, 23, 30)
    start, end = budget.day_bounds(t, TZ)
    assert start == _ts(2026, 11, 6, 0, 0)
    assert end - start == 86400
    assert start <= t < end


def test_usage_counts_chat_and_extract(wired, cfg):
    """对话和抽取是两笔账，漏一笔重度用户能漏掉一半成本。"""
    now = time.time()
    wired.add_metric(ts=int(now), prompt_tokens=100, completion_tokens=20)
    wired.add_metric(ts=int(now), prompt_tokens=50, completion_tokens=10)
    wired.record_run(session="s", start_idx=0, end_idx=9, status="ok",
                     prompt_tokens=300, completion_tokens=40)
    st = budget.check(wired, cfg, now=now)
    assert st.chat_tokens == 180
    assert st.extract_tokens == 340
    assert st.used == 520


def test_yesterday_and_tomorrow_are_excluded(wired, cfg):
    """`until` 不是摆设：没有它，明天的行会把今天判成超限。"""
    now = _ts(2026, 11, 6, 12, 0)
    wired.add_metric(ts=now - 86400, prompt_tokens=999)      # 昨天
    wired.add_metric(ts=now + 86400, prompt_tokens=888)      # 明天
    wired.add_metric(ts=now - 60, prompt_tokens=12, completion_tokens=3)
    st = budget.check(wired, cfg, now=now)
    assert st.used == 15


def test_over_limit_degrades(wired, cfg):
    cfg.budget.daily_tokens = 100
    now = time.time()
    wired.add_metric(ts=int(now), prompt_tokens=90, completion_tokens=10)
    st = budget.check(wired, cfg, now=now)
    assert st.limit == 100
    assert st.used == 100
    assert st.over and st.degraded
    assert st.pct == 100.0
    assert "降级中" in budget.format_status(st)


def test_zero_limit_means_off_not_blocked(wired, cfg):
    """0 是"别管我"，不是"零额度"。后者会让所有旧配置升级即降级。"""
    cfg.budget.daily_tokens = 0
    wired.add_metric(ts=int(time.time()), prompt_tokens=10 ** 6)
    st = budget.check(wired, cfg)
    assert st.used > 0
    assert not st.over and not st.degraded
    assert "未设上限" in budget.format_status(st)


def test_on_exceed_off_only_warns(wired, cfg):
    cfg.budget.daily_tokens = 10
    cfg.budget.on_exceed = "off"
    wired.add_metric(ts=int(time.time()), prompt_tokens=99)
    st = budget.check(wired, cfg)
    assert st.over
    assert not st.degraded
    assert "off" in budget.format_status(st)


def test_reset_restarts_count_without_deleting_ledger(wired, cfg):
    """重置写时间戳，不删 metrics——账和额度是两件事。"""
    cfg.budget.daily_tokens = 100
    now = _ts(2026, 11, 6, 12, 0)
    wired.add_metric(ts=now - 600, prompt_tokens=200)
    assert budget.check(wired, cfg, now=now).degraded

    budget.reset(wired, now=now - 60)
    st = budget.check(wired, cfg, now=now)
    assert st.used == 0, "重置前的用量不该还压在头上"
    assert not st.degraded
    # 账还在
    assert wired.token_sum(0)["prompt_tokens"] == 200

    wired.add_metric(ts=now - 30, prompt_tokens=40)
    assert budget.check(wired, cfg, now=now).used == 40


def test_next_day_recovers_automatically(wired, cfg):
    """00:00 之后自然恢复，不需要任何人执行命令。"""
    cfg.budget.daily_tokens = 100
    wired.add_metric(ts=_ts(2026, 11, 6, 23, 50), prompt_tokens=500)
    assert budget.check(wired, cfg, now=_ts(2026, 11, 6, 23, 59)).degraded
    assert not budget.check(wired, cfg, now=_ts(2026, 11, 7, 0, 1)).degraded


# ------------------------------------------------------------------ 装配
def _seed_memory(store: Store) -> int:
    eid = store.add_event(
        session="s", raw="我下个月十号搬家，搬到城西。",
        summary="用户下个月十号搬家到城西",
        keywords="搬家 城西 下个月", event_time=_ts(2026, 11, 1), w0=0.8,
        pinned=1,
    )
    store.add_event(
        session="s", raw="我养了一只金毛叫大福。",
        summary="用户养了金毛大福", keywords="金毛 大福 狗",
        event_time=_ts(2026, 10, 1), w0=0.6,
    )
    return eid


def test_degraded_pack_is_a_c_anchor_only(wired, cfg):
    _seed_memory(wired)
    full = pack(wired, "s", "我什么时候搬家？", cfg=cfg, now=datetime(2026, 11, 6, 12, 0))
    low = pack(wired, "s", "我什么时候搬家？", cfg=cfg, degrade=True,
               now=datetime(2026, 11, 6, 12, 0))

    assert [b.name for b in low.blocks] == ["persona", "pinned", "anchor"]
    assert low.degraded is True
    assert low.trigger is not None and low.trigger.reason == "budget_degrade"
    assert low.total_tokens < full.total_tokens
    assert "[记忆 · " not in low.text, "降级还带记忆，等于没降"
    assert "大福" not in low.text
    # 钉住的必须在：降级是"少记新事"，不是"忘掉你让我记的"
    assert "搬家" in low.text
    # 时间锚必须在：没有日期，"明天提醒你"会直接说错
    assert "[现在 2026-11-06 12:00]" in low.text


def test_degraded_pack_does_not_poison_reuse_state(wired, cfg):
    """降级那几轮不能把 last_pack_d 写成空，否则恢复后第一轮静默失忆。"""
    _seed_memory(wired)
    now = datetime(2026, 11, 6, 12, 0)
    pack(wired, "s", "我什么时候搬家？", cfg=cfg, now=now)
    before = wired.get_state("s", "last_pack_d", "")
    assert before
    pack(wired, "s", "我什么时候搬家？", cfg=cfg, now=now, degrade=True)
    assert wired.get_state("s", "last_pack_d", "") == before


# ------------------------------------------------------------------ 代理
def test_extract_is_skipped_while_degraded(monkeypatch):
    """预算见底时抽取必须停：它是后台的第二笔钱。"""
    from hippo.adapters import proxy as px

    app = SimpleNamespace(state=SimpleNamespace(
        extract_enabled=True, degraded=True,
        extract_tasks=set(), extract_running=set(),
        cfg=SimpleNamespace(extract=SimpleNamespace(every_n_turns=5)),
        writer=None,
    ))
    monkeypatch.setattr(px.asyncio, "create_task",
                        lambda *a, **k: pytest.fail("降级中不该排抽取任务"))
    px._schedule_extract(app, "s")


async def test_over_budget_response_still_works(cfg, db_path, wired):
    """降级后对话照常：200、有回复、请求里没有记忆段。"""
    cfg.budget.daily_tokens = 50
    wired.add_metric(ts=int(time.time()), prompt_tokens=60, completion_tokens=5)
    fake = FakeDeepSeek(json_body=completion("我在，只是今天少记一点。"))
    app = create_app(cfg, store_path=db_path, upstream=fake.transport())

    async with serve(app) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-chat",
                  "messages": [{"role": "user", "content": "我什么时候搬家？"}]},
            headers={"authorization": "Bearer sk-x"},
        )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"]
    assert app.state.degraded is True

    sent = fake.last_json()
    system = sent["messages"][0]["content"]
    assert "[记忆 · " not in system
    assert "[ctx-v" in system
