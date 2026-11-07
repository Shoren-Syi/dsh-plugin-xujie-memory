# -*- coding: utf-8 -*-
"""QQ 桥接的服务端两件事：**设置**与**预算账本**。

从 0.8 的实验分支（`xujie_memory/qq_budget.py` / `qq_settings.py`）抽出来适配：
- 金额统一用「百万分之一元」整数，避免浮点误差；
- 预算必须在**每次模型请求前预留**、拿到真实用量后结算；
- 价格/用量不可知时保留预留，不假装能限住费用；
- 设置带 revision，Node 侧靠它判断要不要重建桥接。

安全边界：QQ 号、群号必须是纯数字；预算上界必须是正整数；所有写入走参数化 SQL。
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, asdict
from typing import Any

__all__ = [
    "BudgetDenied", "BudgetPolicy", "QQSettings", "token_cost", "ensure_tables",
    "set_settings", "active_settings", "configure_budget", "budget_status",
    "reserve", "settle", "cancel_unstarted",
]

ACTIVITIES = ("chat", "proactive", "qzone")


class BudgetDenied(ValueError):
    """预算 / 次数 / 价格没配好，不能发起这次调用。"""


@dataclass(frozen=True)
class BudgetPolicy:
    per_request: int
    daily: int
    monthly: int
    input_price: int
    output_price: int
    daily_calls: int
    cache_price: int | None = None


@dataclass(frozen=True)
class QQSettings:
    """启用的桥接配置。字段与 Node 侧 QQRuntime 的 config 对齐。"""

    revision: str
    selfId: str
    wsUrl: str
    httpUrl: str
    accessToken: str
    privateAllow: list[str]
    groupAllow: list[str]
    requireMention: bool
    provider: str
    model: str
    maxTokens: int
    budgetScope: str
    schedule: dict[str, Any]


def token_cost(tokens: int, price_per_million: int) -> int:
    if (type(tokens) is not int or not 0 <= tokens <= 2**31 - 1
            or type(price_per_million) is not int or not 0 <= price_per_million <= 2**53 - 1):
        raise ValueError("token 数和价格不合法")
    return (tokens * price_per_million + 999_999) // 1_000_000


def ensure_tables(conn: Any) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS qq_settings (
        slot TEXT PRIMARY KEY,
        revision TEXT NOT NULL,
        payload TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS qq_budget_policies (
        scope_id TEXT PRIMARY KEY,
        per_request INTEGER NOT NULL,
        daily INTEGER NOT NULL,
        monthly INTEGER NOT NULL,
        input_price INTEGER NOT NULL,
        output_price INTEGER NOT NULL,
        daily_calls INTEGER NOT NULL,
        cache_price INTEGER
    );
    CREATE TABLE IF NOT EXISTS qq_budget_activity_limits (
        scope_id TEXT NOT NULL,
        activity TEXT NOT NULL,
        daily_calls INTEGER NOT NULL,
        PRIMARY KEY (scope_id, activity)
    );
    CREATE TABLE IF NOT EXISTS qq_budget_calls (
        call_id TEXT PRIMARY KEY,
        scope_id TEXT NOT NULL,
        activity TEXT NOT NULL,
        day TEXT NOT NULL,
        month TEXT NOT NULL,
        amount INTEGER NOT NULL,
        status TEXT NOT NULL,
        input_price INTEGER NOT NULL,
        output_price INTEGER NOT NULL,
        cache_price INTEGER,
        input_tokens INTEGER,
        output_tokens INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_qq_calls_scope ON qq_budget_calls(scope_id, month, status);
    """)


# ------------------------------------------------------------------ 设置
def _digits(value: Any, name: str, *, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    if not text.isdigit() or len(text) > 20:
        raise ValueError(f"{name} 必须是纯数字")
    return text


def _http_url(value: Any, name: str) -> str:
    from urllib.parse import urlparse
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{name} 必须是无凭据的 HTTP/HTTPS 地址")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{name} 不能带查询串或片段")
    return text.rstrip("/")


def _ws_url(value: Any) -> str:
    from urllib.parse import urlparse
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in ("ws", "wss") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("wsUrl 必须是 ws:// 或 wss://")
    if parsed.fragment:
        raise ValueError("wsUrl 不能带片段")
    return text


def parse_settings(payload: dict[str, Any]) -> QQSettings:
    """把外部 JSON 规范成 QQSettings；任何越界都直接拒绝，不做"尽量修"。"""
    if not isinstance(payload, dict):
        raise ValueError("QQ 设置必须是对象")
    schedule = payload.get("schedule") or {}
    if not isinstance(schedule, dict):
        raise ValueError("schedule 必须是对象")
    box: dict[str, Any] = {}
    for activity in ("proactive", "qzone"):
        item = schedule.get(activity) or {}
        if not isinstance(item, dict) or not item.get("enabled"):
            continue
        minutes = int(item.get("intervalMinutes") or 0)
        if not 1 <= minutes <= 24 * 60:
            raise ValueError("定时任务的间隔必须在 1~1440 分钟")
        prompt = str(item.get("prompt") or "").strip()
        if not prompt or len(prompt) > 2000:
            raise ValueError("定时任务的提示词不能为空且不超过 2000 字")
        entry: dict[str, Any] = {"enabled": True, "intervalMinutes": minutes, "prompt": prompt}
        if activity == "proactive":
            channel = str(item.get("channel") or "private")
            if channel not in ("private", "group"):
                raise ValueError("主动发言只支持私聊或群聊")
            entry["channel"] = channel
            entry["target"] = _digits(item.get("target"), "主动发言对象")
        else:
            visibility = int(item.get("visibility") or 64)
            if visibility not in (1, 4, 16, 64, 128):
                raise ValueError("空间可见范围不合法")
            entry["visibility"] = visibility
        box[activity] = entry
    ts = QQSettings(
        revision="",
        selfId=_digits(payload.get("selfId"), "机器人 QQ 号"),
        wsUrl=_ws_url(payload.get("wsUrl")),
        httpUrl=_http_url(payload.get("httpUrl"), "httpUrl"),
        accessToken=str(payload.get("accessToken") or "")[:200],
        privateAllow=[_digits(x, "私聊白名单") for x in (payload.get("privateAllow") or [])][:2000],
        groupAllow=[_digits(x, "群白名单") for x in (payload.get("groupAllow") or [])][:2000],
        requireMention=bool(payload.get("requireMention", True)),
        provider=str(payload.get("provider") or "")[:80],
        model=str(payload.get("model") or "")[:120],
        maxTokens=int(payload.get("maxTokens") or 0),
        budgetScope=_digits(payload.get("budgetScope"), "预算范围"),
        schedule=box,
    )
    if not ts.provider or not ts.model:
        raise ValueError("必须指定 provider 与 model")
    if not 1 <= ts.maxTokens <= 200_000:
        raise ValueError("maxTokens 必须在 1~200000")
    return ts


def set_settings(conn: Any, payload: dict[str, Any], *, now: int) -> QQSettings:
    ensure_tables(conn)
    ts = parse_settings(payload)
    revision = uuid.uuid4().hex[:12]
    body = json.dumps({**asdict(ts), "revision": revision}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO qq_settings(slot, revision, payload, updated_at) VALUES ('active', ?, ?, ?)"
        " ON CONFLICT(slot) DO UPDATE SET revision=excluded.revision,"
        " payload=excluded.payload, updated_at=excluded.updated_at",
        (revision, body, now),
    )
    conn.commit()
    return QQSettings(**{**asdict(ts), "revision": revision})


def active_settings(conn: Any) -> QQSettings | None:
    ensure_tables(conn)
    row = conn.execute("SELECT payload FROM qq_settings WHERE slot='active'").fetchone()
    if not row:
        return None
    data = json.loads(row[0])
    return QQSettings(**data)


def clear_settings(conn: Any) -> None:
    ensure_tables(conn)
    conn.execute("DELETE FROM qq_settings WHERE slot='active'")
    conn.commit()


# ------------------------------------------------------------------ 预算
def configure_budget(conn: Any, scope: str, policy: BudgetPolicy,
                     activity_limits: dict[str, int] | None = None) -> None:
    ensure_tables(conn)
    if not scope or not scope.isdigit():
        raise ValueError("预算范围必须是纯数字")
    values = [policy.per_request, policy.daily, policy.monthly, policy.input_price,
              policy.output_price, policy.daily_calls]
    if any(type(v) is not int or not 0 < v <= 2**53 - 1 for v in values):
        raise ValueError("预算、价格和次数上限必须是正整数")
    if policy.cache_price is not None and (type(policy.cache_price) is not int
                                          or not 0 <= policy.cache_price <= policy.input_price):
        raise ValueError("缓存价格必须在 0~输入价格之间")
    conn.execute(
        "INSERT INTO qq_budget_policies VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(scope_id) DO UPDATE SET per_request=excluded.per_request,"
        " daily=excluded.daily, monthly=excluded.monthly, input_price=excluded.input_price,"
        " output_price=excluded.output_price, daily_calls=excluded.daily_calls,"
        " cache_price=excluded.cache_price",
        (scope, *values, policy.cache_price),
    )
    for activity, limit in (activity_limits or {}).items():
        if activity not in ACTIVITIES or type(limit) is not int or limit <= 0:
            raise ValueError("活动类型或每日次数上限不合法")
        conn.execute(
            "INSERT INTO qq_budget_activity_limits VALUES (?,?,?)"
            " ON CONFLICT(scope_id,activity) DO UPDATE SET daily_calls=excluded.daily_calls",
            (scope, activity, limit),
        )
    conn.commit()


def _policy(conn: Any, scope: str) -> BudgetPolicy:
    row = conn.execute("SELECT * FROM qq_budget_policies WHERE scope_id=?", (scope,)).fetchone()
    if row is None:
        raise BudgetDenied("尚未设置预算及模型价格")
    return BudgetPolicy(per_request=row[1], daily=row[2], monthly=row[3], input_price=row[4],
                        output_price=row[5], daily_calls=row[6], cache_price=row[7])


def budget_status(conn: Any, scope: str, *, now: dt.datetime | None = None) -> dict:
    ensure_tables(conn)
    now = now or dt.datetime.now().astimezone()
    policy = _policy(conn, scope)
    limits = {r[1]: r[2] for r in conn.execute(
        "SELECT scope_id,activity,daily_calls FROM qq_budget_activity_limits WHERE scope_id=?", (scope,))}
    rows = conn.execute(
        "SELECT day,activity,amount,status FROM qq_budget_calls"
        " WHERE scope_id=? AND month=? AND status IN ('reserved','settled')",
        (scope, now.strftime("%Y-%m"))).fetchall()
    day = now.date().isoformat()
    return {
        "configured": True,
        "policy": asdict(policy),
        "activity_limits": limits,
        "today": sum(r[2] for r in rows if r[0] == day),
        "month": sum(r[2] for r in rows),
        "pending": sum(r[2] for r in rows if r[3] == "reserved"),
        "calls_today": sum(1 for r in rows if r[0] == day),
    }


def reserve(conn: Any, scope: str, activity: str, input_limit: int, output_limit: int,
            *, now: dt.datetime | None = None) -> str:
    ensure_tables(conn)
    if activity not in ACTIVITIES or not scope:
        raise ValueError("预算对象或活动类型不合法")
    now = now or dt.datetime.now().astimezone()
    day, month = now.date().isoformat(), now.strftime("%Y-%m")
    policy = _policy(conn, scope)
    estimated = token_cost(input_limit, policy.input_price) + token_cost(output_limit, policy.output_price)
    if estimated <= 0 or estimated > policy.per_request:
        raise BudgetDenied("单次预算不足")
    rows = conn.execute(
        "SELECT day,amount,activity FROM qq_budget_calls"
        " WHERE scope_id=? AND status IN ('reserved','settled') AND month=?", (scope, month)).fetchall()
    if sum(r[1] for r in rows if r[0] == day) + estimated > policy.daily:
        raise BudgetDenied("已达到每日预算上限")
    if sum(r[1] for r in rows) + estimated > policy.monthly:
        raise BudgetDenied("已达到每月预算上限")
    if sum(1 for r in rows if r[0] == day) >= policy.daily_calls:
        raise BudgetDenied("已达到每日调用次数上限")
    limit = conn.execute(
        "SELECT daily_calls FROM qq_budget_activity_limits WHERE scope_id=? AND activity=?",
        (scope, activity)).fetchone()
    if limit and sum(1 for r in rows if r[0] == day and r[2] == activity) >= limit[0]:
        raise BudgetDenied("已达到该活动的每日次数上限")
    call_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO qq_budget_calls(call_id,scope_id,activity,day,month,amount,status,"
        "input_price,output_price,cache_price) VALUES (?,?,?,?,?,?,'reserved',?,?,?)",
        (call_id, scope, activity, day, month, estimated,
         policy.input_price, policy.output_price, policy.cache_price))
    conn.commit()
    return call_id


def settle(conn: Any, call_id: str, input_tokens: int, output_tokens: int,
           cached_tokens: int = 0) -> int:
    ensure_tables(conn)
    row = conn.execute("SELECT * FROM qq_budget_calls WHERE call_id=?", (call_id,)).fetchone()
    if row is None or row[6] != "reserved":
        raise ValueError("预算预留不存在或已经结算")
    cache_price = row[9]
    if cached_tokens and cache_price is None:
        raise ValueError("缓存价格未知，保留预算预留")
    amount = (token_cost(input_tokens, row[7]) + token_cost(cached_tokens, cache_price or 0)
              + token_cost(output_tokens, row[8]))
    conn.execute("UPDATE qq_budget_calls SET status='settled', amount=?, input_tokens=?,"
                 " output_tokens=? WHERE call_id=?",
                 (amount, input_tokens, output_tokens, call_id))
    conn.commit()
    return amount


def cancel_unstarted(conn: Any, call_id: str) -> None:
    ensure_tables(conn)
    cur = conn.execute("UPDATE qq_budget_calls SET status='cancelled', amount=0"
                       " WHERE call_id=? AND status='reserved'", (call_id,))
    if cur.rowcount != 1:
        raise ValueError("预算预留不存在或已经结算")
    conn.commit()
