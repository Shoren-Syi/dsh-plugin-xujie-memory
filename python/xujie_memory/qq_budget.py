"""QQ 模型调用的预算预留与结算；金额单位为百万分之一元。"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass


class BudgetDenied(ValueError):
    pass


@dataclass(frozen=True)
class BudgetPolicy:
    per_request: int
    daily: int
    monthly: int
    input_price: int
    output_price: int
    daily_calls: int
    cache_price: int | None = None

    def __post_init__(self):
        if any(type(value) is not int or not 0 < value <= 2**53 - 1 for key, value in vars(self).items() if key != 'cache_price'):
            raise ValueError('预算、价格和次数上限必须是正整数')
        if self.cache_price is not None and (type(self.cache_price) is not int or not 0 <= self.cache_price <= self.input_price):
            raise ValueError('缓存价格必须位于零和输入价格之间')


def token_cost(tokens: int, price_per_million: int) -> int:
    if (type(tokens) is not int or not 0 <= tokens <= 2**31 - 1
            or type(price_per_million) is not int or not 0 <= price_per_million <= 2**53 - 1):
        raise ValueError('token 数和价格不合法')
    cost = (tokens * price_per_million + 999_999) // 1_000_000
    if cost > 2**63 - 1:
        raise ValueError('预计费用超出可记录范围')
    return cost


class QQBudget:
    def __init__(self, engine):
        self.engine = engine

    def configure(self, scope: str, policy: BudgetPolicy) -> None:
        if not scope or not isinstance(policy, BudgetPolicy):
            raise ValueError('预算对象或配置不合法')
        with self.engine._tx() as conn:
            conn.execute(
                'INSERT INTO qq_budget_policies VALUES (?,?,?,?,?,?,?,?)'
                ' ON CONFLICT(scope_id) DO UPDATE SET per_request=excluded.per_request, daily=excluded.daily,'
                ' monthly=excluded.monthly, input_price=excluded.input_price, output_price=excluded.output_price,'
                ' daily_calls=excluded.daily_calls, cache_price=excluded.cache_price',
                (scope, policy.per_request, policy.daily, policy.monthly, policy.input_price,
                 policy.output_price, policy.daily_calls, policy.cache_price),
            )

    def configure_activity(self, scope: str, activity: str, daily_calls: int) -> None:
        if not scope or activity not in ('chat', 'proactive', 'qzone') or type(daily_calls) is not int or daily_calls <= 0:
            raise ValueError('活动类型或每日次数上限不合法')
        with self.engine._tx() as conn:
            conn.execute(
                'INSERT INTO qq_budget_activity_limits VALUES (?,?,?)'
                ' ON CONFLICT(scope_id,activity) DO UPDATE SET daily_calls=excluded.daily_calls',
                (scope, activity, daily_calls),
            )

    def status(self, scope: str, *, now: dt.datetime | None = None) -> dict:
        now = now or dt.datetime.now().astimezone()
        if not scope or now.tzinfo is None:
            raise ValueError('预算对象或时间不合法')
        conn = self.engine._conn()
        setting = conn.execute('SELECT * FROM qq_budget_policies WHERE scope_id=?', (scope,)).fetchone()
        limits = conn.execute('SELECT activity,daily_calls FROM qq_budget_activity_limits WHERE scope_id=?', (scope,)).fetchall()
        rows = conn.execute("SELECT day,activity,amount,status FROM qq_budget_calls WHERE scope_id=? AND month=? AND status IN ('reserved','settled')", (scope, now.strftime('%Y-%m'))).fetchall()
        day = now.date().isoformat()
        return {
            'configured': setting is not None,
            'policy': {key: setting[key] for key in BudgetPolicy.__dataclass_fields__} if setting else None,
            'activity_limits': {row['activity']: row['daily_calls'] for row in limits},
            'today': sum(row['amount'] for row in rows if row['day'] == day),
            'month': sum(row['amount'] for row in rows),
            'pending': sum(row['amount'] for row in rows if row['status'] == 'reserved'),
        }

    def reserve(self, scope: str, activity: str,
                input_limit: int, output_limit: int, *, now: dt.datetime | None = None) -> str:
        if activity not in ('chat', 'proactive', 'qzone') or not scope:
            raise ValueError('预算对象或活动类型不合法')
        now = now or dt.datetime.now().astimezone()
        if now.tzinfo is None:
            raise ValueError('预算时间必须带时区')
        day, month = now.date().isoformat(), now.strftime('%Y-%m')
        with self.engine._tx() as conn:
            setting = conn.execute('SELECT * FROM qq_budget_policies WHERE scope_id=?', (scope,)).fetchone()
            if setting is None:
                raise BudgetDenied('尚未设置预算及模型价格')
            policy = BudgetPolicy(**{key: setting[key] for key in BudgetPolicy.__dataclass_fields__})
            estimated = token_cost(input_limit, policy.input_price) + token_cost(output_limit, policy.output_price)
            if estimated <= 0 or estimated > policy.per_request:
                raise BudgetDenied('单次预算不足')
            rows = conn.execute(
                "SELECT day,month,amount,activity FROM qq_budget_calls WHERE scope_id=? AND status IN ('reserved','settled') AND month=?",
                (scope, month),
            ).fetchall()
            day_total = sum(row['amount'] for row in rows if row['day'] == day)
            month_total = sum(row['amount'] for row in rows)
            if day_total + estimated > policy.daily or month_total + estimated > policy.monthly:
                raise BudgetDenied('已达到每日或每月预算上限')
            if sum(1 for row in rows if row['day'] == day) >= policy.daily_calls:
                raise BudgetDenied('已达到每日调用次数上限')
            activity_limit = conn.execute('SELECT daily_calls FROM qq_budget_activity_limits WHERE scope_id=? AND activity=?', (scope, activity)).fetchone()
            if activity_limit and sum(1 for row in rows if row['day'] == day and row['activity'] == activity) >= activity_limit['daily_calls']:
                raise BudgetDenied('已达到该活动的每日次数上限')
            call_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO qq_budget_calls(call_id,scope_id,activity,day,month,amount,status,input_price,output_price,cache_price)"
                " VALUES (?,?,?,?,?,?,'reserved',?,?,?)",
                (call_id, scope, activity, day, month, estimated, policy.input_price, policy.output_price, policy.cache_price),
            )
            return call_id

    def settle(self, call_id: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> int:
        with self.engine._tx() as conn:
            row = conn.execute('SELECT * FROM qq_budget_calls WHERE call_id=?', (call_id,)).fetchone()
            if row is None or row['status'] != 'reserved':
                raise ValueError('预算预留不存在或已经结算')
            if cached_tokens and row['cache_price'] is None:
                raise ValueError('缓存价格未知，保留预算预留')
            amount = (token_cost(input_tokens, row['input_price']) + token_cost(cached_tokens, row['cache_price'] or 0)
                      + token_cost(output_tokens, row['output_price']))
            if amount > 2**63 - 1:
                raise ValueError('实际费用超出可记录范围')
            conn.execute("UPDATE qq_budget_calls SET status='settled',amount=? WHERE call_id=?", (amount, call_id))
            return amount

    def cancel_unstarted(self, call_id: str) -> None:
        with self.engine._tx() as conn:
            result = conn.execute("UPDATE qq_budget_calls SET status='cancelled',amount=0 WHERE call_id=? AND status='reserved'", (call_id,))
            if result.rowcount != 1:
                raise ValueError('预算预留不存在或已经结算')
