"""M19.1 计划时间解析与落库（手册验收 4 条）。

20 条表述夹具（手册要求 ≥95%，实际 20/20）：相对日+时刻 / 周几 /
周期计划（每天 X 点、每周三）/ 相对量 / 绝对日期。每条的期望值都
是拿日历手算的（base 固定 2026-11-05 周四 14:30），不是照实现抄。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hippo.core.timeparse import parse_due

TZ = ZoneInfo("Asia/Shanghai")
# 2026-11-05 是周四（isoweekday=4，测试里也断言一次防止换 base 后全错）
BASE = datetime(2026, 11, 5, 14, 30, tzinfo=TZ)


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, TZ)


def test_base_is_thursday():
    assert BASE.isoweekday() == 4


# ---------------------------------------------------------- 20 条夹具
CASES = [
    # (hint, 期望 datetime, recur)
    ("明天8点叫我起来干活", "11-06 08:00", None),
    ("明天早上七点半", "11-06 07:30", None),
    ("后天下午3点开会", "11-07 15:00", None),
    ("下周三下午2点", "11-11 14:00", None),
    ("每天早上7点半叫我起床", "11-06 07:30", "daily"),
    ("每天晚上10点提醒我吃药", "11-05 22:00", "daily"),
    ("每周三下午3点开会", "11-11 15:00", "weekly"),
    ("30分钟后叫我", "11-05 15:00", None),
    ("两小时后提醒我", "11-05 16:30", None),
    ("12月24号晚上7点半", "12-24 19:30", None),
    ("明天上午9点面试", "11-06 09:00", None),
    ("15分钟后", "11-05 14:45", None),
    ("每天中午12点", "11-06 12:00", "daily"),
    ("礼拜五晚上八点", "11-06 20:00", None),
    ("三天后早上9点", "11-08 09:00", None),
    ("每天早上7点", "11-06 07:00", "daily"),
    ("下周一下午4点", "11-09 16:00", None),
    ("明天中午十二点", "11-06 12:00", None),
    ("每天早上6点半晨跑", "11-06 06:30", "daily"),
    ("周五下午5点半下班提醒", "11-06 17:30", None),
]


@pytest.mark.parametrize("hint,want,recur", CASES,
                         ids=[c[0] for c in CASES])
def test_parse_due_cases(hint, want, recur):
    parsed, r = parse_due(hint, base=BASE)
    assert parsed is not None, hint
    assert _dt(parsed.ts).strftime("%m-%d %H:%M") == want, hint
    assert r == recur, hint


def test_20_cases_at_least_95_percent():
    """验收 #1：20 种表述解析 ≥95%。全对是 100%。"""
    ok = sum(1 for hint, want, _ in CASES
             if parse_due(hint, base=BASE) is not None
             and _dt(parse_due(hint, base=BASE)[0].ts)
             .strftime("%m-%d %H:%M") == want)
    assert ok / len(CASES) >= 0.95


# ---------------------------------------------------------- 语义守护
def test_past_time_for_one_shot_plan_returns_none():
    """非周期计划落过去 → None（反问）。16:30 说"今天下午3点"不是计划。"""
    base = datetime(2026, 11, 5, 16, 30, tzinfo=TZ)
    parsed, recur = parse_due("今天下午3点开会", base=base)
    assert parsed is None and recur is None


def test_daily_plan_past_today_rolls_to_tomorrow():
    """周期计划过了今天的点 → 顺延到明天（"每天早上7点半"在 9 点说）。"""
    base = datetime(2026, 11, 5, 9, 0, tzinfo=TZ)
    parsed, recur = parse_due("每天早上7点半", base=base)
    assert recur == "daily"
    assert _dt(parsed.ts).strftime("%m-%d %H:%M") == "11-06 07:30"


def test_weekly_plan_today_past_rolls_next_week():
    """周期计划落在今天的点已过 → 推下周（"每周三下午3点"在周三 16 点说）。"""
    base = datetime(2026, 11, 4, 16, 0, tzinfo=TZ)      # 周三
    parsed, recur = parse_due("每周三下午3点", base=base)
    assert recur == "weekly"
    assert _dt(parsed.ts).strftime("%m-%d %H:%M") == "11-11 15:00"


def test_write_time_conversion_is_one_shot(store):
    """验收 #2：相对时间只在写入时算——due_at 是写死的绝对时间戳，
    plan 行里没有存"明天"这种相对词（raw_hint 只是留档）。"""
    from hippo.core.persist import persist_result
    from hippo.core.models import Turn
    from hippo.core.schema import ExtractResult
    from hippo.core.turns import Window

    turns = [Turn(session="s", idx=0, role="user",
                  content="明天8点叫我起来干活", ts=1_700_000_000)]
    w = Window(session="s", idxs=[0], token_est=100,
               has_pending=False, turns=turns)
    r = ExtractResult(
        events=[{"raw": "明天8点叫我起来干活", "keywords": ["干活"]}],
        plans=[{"content": "叫用户起来干活", "due_hint": "明天8点"}],
    )
    st = persist_result(store, "s", w, r)
    assert st.plans == 1
    plan = store.due_plans(4_000_000_000)[0]     # 远期 now 也能查到
    # 换算发生在写入时：due_at 已是 epoch，不是"now+1天"的运行时表达式
    assert plan.raw_hint == "明天8点"
    assert plan.due_at > 1_700_000_000


def test_undated_plan_becomes_doubt(store):
    """验收 #4：模糊表述反问一次——落 doubt，不落 plan。"""
    from hippo.core.persist import persist_result
    from hippo.core.models import Turn
    from hippo.core.schema import ExtractResult
    from hippo.core.turns import Window

    turns = [Turn(session="s", idx=0, role="user",
                  content="记得提醒我那个事", ts=1_700_000_000)]
    w = Window(session="s", idxs=[0], token_est=100,
               has_pending=False, turns=turns)
    r = ExtractResult(
        events=[{"raw": "记得提醒我那个事", "keywords": ["提醒"]}],
        plans=[{"content": "提醒那个事", "due_hint": "回头"}],
    )
    st = persist_result(store, "s", w, r)
    assert st.plans == 0 and st.plans_dropped == 1
    doubts = store.active_doubts("s")
    assert len(doubts) == 1
    assert "回头" in doubts[0].question and "提醒那个事" in doubts[0].question


def test_undated_without_hint_no_doubt(store):
    """连 due_hint 都没有 → 只降级，不反问（没得问）。"""
    from hippo.core.persist import persist_result
    from hippo.core.models import Turn
    from hippo.core.schema import ExtractResult
    from hippo.core.turns import Window

    turns = [Turn(session="s", idx=0, role="user",
                  content="记得提醒我那个事", ts=1_700_000_000)]
    w = Window(session="s", idxs=[0], token_est=100,
               has_pending=False, turns=turns)
    r = ExtractResult(
        events=[{"raw": "记得提醒我那个事", "keywords": ["提醒"]}],
        plans=[{"content": "提醒那个事"}],
    )
    st = persist_result(store, "s", w, r)
    assert st.plans == 0 and st.doubts == 0


def test_model_recur_field_wins(store):
    """模型标的 recur 优先于文本检测（模型知道"每周复查"其实是 weekly）。"""
    from hippo.core.persist import persist_result
    from hippo.core.models import Turn
    from hippo.core.schema import ExtractResult
    from hippo.core.turns import Window

    turns = [Turn(session="s", idx=0, role="user",
                  content="下周一开始每周交一次周报", ts=1_700_000_000)]
    w = Window(session="s", idxs=[0], token_est=100,
               has_pending=False, turns=turns)
    r = ExtractResult(
        events=[{"raw": "下周一开始每周交一次周报", "keywords": ["周报"]}],
        plans=[{"content": "交周报", "due_hint": "下周一开始每周交",
                "recur": "weekly"}],
    )
    st = persist_result(store, "s", w, r)
    assert st.plans == 1
    assert store.due_plans(4_000_000_000)[0].recur == "weekly"
