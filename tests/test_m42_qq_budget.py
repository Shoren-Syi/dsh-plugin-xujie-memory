# -*- coding: utf-8 -*-
"""M42 验收：QQ 桥接的服务端两件事——设置校验与预算账本。

这些数字都涉及钱，所以测的是"拒绝"而不是"尽力修"：
非法 QQ 号必须拒、超预算必须拒、用量不完整必须保留预留。
"""
from __future__ import annotations

import datetime as dt

import pytest

from hippo.core import qq

NOW = dt.datetime(2026, 11, 7, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def _settings(**over):
    base = {
        "selfId": "10001", "wsUrl": "ws://127.0.0.1:3001", "httpUrl": "http://127.0.0.1:3000",
        "accessToken": "", "privateAllow": ["20002"], "groupAllow": ["30003"],
        "requireMention": True, "provider": "deepseek-official", "model": "deepseek-flash",
        "maxTokens": 800, "budgetScope": "10001", "schedule": {},
    }
    base.update(over)
    return base


def _policy(**over):
    base = dict(per_request=10_000, daily=100_000, monthly=1_000_000,
                input_price=1_000_000, output_price=2_000_000, daily_calls=50, cache_price=None)
    base.update(over)
    return qq.BudgetPolicy(**base)


def test_settings_roundtrip_and_revision(conn):
    first = qq.set_settings(conn, _settings(), now=1)
    assert first.revision and first.selfId == "10001"
    assert qq.active_settings(conn).revision == first.revision
    second = qq.set_settings(conn, _settings(maxTokens=400), now=2)
    assert second.revision != first.revision
    assert qq.active_settings(conn).maxTokens == 400


@pytest.mark.parametrize("over", [
    {"selfId": "abc"},
    {"wsUrl": "http://127.0.0.1:3001"},
    {"httpUrl": "http://user:pw@127.0.0.1:3000"},
    {"maxTokens": 0},
    {"provider": ""},
    {"privateAllow": ["20a"]},
    {"schedule": {"proactive": {"enabled": True, "intervalMinutes": 0, "prompt": "x", "target": "1"}}},
])
def test_settings_reject_bad_input(conn, over):
    with pytest.raises(ValueError):
        qq.set_settings(conn, _settings(**over), now=1)


def test_budget_reserve_and_settle(conn):
    qq.configure_budget(conn, "10001", _policy())
    call_id = qq.reserve(conn, "10001", "chat", 1000, 200, now=NOW)
    assert call_id
    status = qq.budget_status(conn, "10001", now=NOW)
    assert status["pending"] > 0 and status["calls_today"] == 1
    amount = qq.settle(conn, call_id, 900, 150, 0)
    assert amount > 0
    after = qq.budget_status(conn, "10001", now=NOW)
    assert after["pending"] == 0 and after["today"] == amount


def test_budget_denied_without_policy(conn):
    with pytest.raises(qq.BudgetDenied):
        qq.reserve(conn, "10001", "chat", 1000, 200, now=NOW)


def test_budget_denies_over_per_request(conn):
    qq.configure_budget(conn, "10001", _policy(per_request=1))
    with pytest.raises(qq.BudgetDenied):
        qq.reserve(conn, "10001", "chat", 1000, 200, now=NOW)


def test_budget_daily_calls_limit(conn):
    qq.configure_budget(conn, "10001", _policy(daily_calls=1))
    qq.reserve(conn, "10001", "chat", 100, 100, now=NOW)
    with pytest.raises(qq.BudgetDenied):
        qq.reserve(conn, "10001", "chat", 100, 100, now=NOW)


def test_activity_limit(conn):
    qq.configure_budget(conn, "10001", _policy(), {"proactive": 1})
    qq.reserve(conn, "10001", "proactive", 100, 100, now=NOW)
    with pytest.raises(qq.BudgetDenied):
        qq.reserve(conn, "10001", "proactive", 100, 100, now=NOW)


def test_cache_tokens_need_cache_price(conn):
    qq.configure_budget(conn, "10001", _policy())
    call_id = qq.reserve(conn, "10001", "chat", 100, 100, now=NOW)
    with pytest.raises(ValueError):
        qq.settle(conn, call_id, 10, 10, 5)


def test_settle_twice_rejected(conn):
    qq.configure_budget(conn, "10001", _policy())
    call_id = qq.reserve(conn, "10001", "chat", 100, 100, now=NOW)
    qq.settle(conn, call_id, 10, 10, 0)
    with pytest.raises(ValueError):
        qq.settle(conn, call_id, 10, 10, 0)


def test_token_cost_rounds_up():
    assert qq.token_cost(1, 1_000_000) == 1
    assert qq.token_cost(0, 1_000_000) == 0
    assert qq.token_cost(1_000_000, 1_000_000) == 1_000_000
