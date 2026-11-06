# -*- coding: utf-8 -*-
"""M24.3 升级判定。

验收：只有白名单能升级 / 单次 ≤300 token / 日上限生效 / 超额有 WARNING。
"""
from __future__ import annotations

import time

from hippo.config import Config
from hippo.core.heartbeat import ESCALATION_REASONS, Escalator

T0 = int(time.mktime((2026, 1, 2, 12, 0, 0, 0, 0, -1)))


def _esc(store, **kw) -> Escalator:
    cfg = Config()
    cfg.heartbeat.max_llm_calls_per_day = kw.pop("cap", 10)
    return Escalator(store, cfg=cfg, wall=lambda: T0, **kw)


def test_whitelist_contents():
    """白名单就三个：计划到期、好奇发言、用户新消息。"""
    assert ESCALATION_REASONS == ("plan", "curiosity", "user_message")


def test_whitelist_passes(store):
    e = _esc(store)
    for reason in ESCALATION_REASONS:
        r = e.escalate(reason, "上下文", now=T0)
        assert r.ok and r.why == "dry", reason


def test_non_whitelist_blocked(store):
    """验收 #1：非白名单一律拒绝，0 例外。

    "每轮顺手总结一下"是最容易写出来的破功代码，这道闸就是为它设的。
    """
    e = _esc(store)
    for reason in ("summary", "tick", "consolidate", "improve", ""):
        r = e.escalate(reason, "上下文", now=T0)
        assert not r.ok and r.why == "not_whitelisted", reason
    # 告警落盘标记（同一天同一种拦截只记一次）
    assert store.get_state("_sys", f"hb_warned:{e._day(T0)}:reason:summary") == "1"


def test_context_trimmed_to_300(store):
    """验收 #2：上下文恒 ≤300 token。按行砍，保留开头。"""
    e = _esc(store)
    big = "\n".join(f"第{i}行：" + "这是一段很长的上下文" * 8 for i in range(60))
    r = e.escalate("plan", big, now=T0)
    assert r.tokens <= 300
    assert r.text.startswith("第0行")
    assert r.text != big

    short = "到点了：叫用户起床"
    assert e.escalate("plan", short, now=T0).text == short


def test_single_line_trim(store):
    """单行超长也要压下来——真实上下文常常是一整段没有换行。"""
    e = _esc(store)
    r = e.escalate("curiosity", "啊" * 5000, now=T0)
    assert r.tokens <= 300 and len(r.text) < 5000


def test_daily_cap(store):
    """验收 #3：第 11 次静默。"""
    e = _esc(store, cap=10)
    noop = lambda *a, **kw: "ok"          # noqa: E731
    oks = [e.escalate("plan", "x", call=noop, now=T0).ok for _ in range(10)]
    assert all(oks)
    r = e.escalate("plan", "x", call=noop, now=T0)
    assert not r.ok and r.why == "daily_cap" and r.left == 0
    assert store.get_state("_sys", f"hb_warned:{e._day(T0)}:cap") == "1"


def test_cap_resets_next_day(store):
    """额度按自然日 reset，不按"距上次调用 24 小时"。"""
    e = _esc(store, cap=2)
    noop = lambda *a, **kw: "ok"          # noqa: E731
    assert e.escalate("plan", "x", call=noop, now=T0).ok
    assert e.escalate("plan", "x", call=noop, now=T0).ok
    assert not e.escalate("plan", "x", call=noop, now=T0).ok
    assert e.escalate("plan", "x", call=noop, now=T0 + 86400).ok


def test_call_receives_limits(store):
    """验收 #2 的另一半：真正发请求时 max_tokens=120、temperature=0.8。"""
    e = _esc(store)
    seen = {}

    def call(text, *, max_tokens, temperature):
        seen.update(text=text, max_tokens=max_tokens, temperature=temperature)
        return "好的"

    r = e.escalate("user_message", "用户问：现在几点", call=call, now=T0)
    assert r.ok and r.why == "ok"
    assert seen == {"text": "用户问：现在几点", "max_tokens": 120,
                    "temperature": 0.8}


def test_failed_call_not_counted(store):
    """调用失败不扣额度——失败了等于没花，扣了就变成"越失败越不能试"。"""
    e = _esc(store, cap=2)

    def boom(*a, **kw):
        raise RuntimeError("网络断了")

    assert not e.escalate("plan", "x", call=boom, now=T0).ok
    assert e.used(T0) == 0
    assert e.left(T0) == 2


def test_warn_once_per_day(store, monkeypatch):
    """同一天同一种拦截只 WARNING 一次——1440 轮 tick 每轮刷一条，
    等于把告警淹没掉，最后没人知道超额了。"""
    from hippo.core import heartbeat as hb_mod

    warns: list[str] = []
    monkeypatch.setattr(hb_mod.log, "warning",
                        lambda msg, **kw: warns.append(str(msg)))
    e = _esc(store, cap=1)
    noop = lambda *a, **kw: "ok"          # noqa: E731
    e.escalate("plan", "x", call=noop, now=T0)
    for _ in range(5):
        e.escalate("plan", "x", call=noop, now=T0)
    # 机制层：标记只写一次
    assert store.get_state("_sys", f"hb_warned:{e._day(T0)}:cap") == "1"
    # 表现层：6 次拦截只发 1 条 WARNING
    assert len(warns) == 1 and "escalate_capped" in warns[0]
