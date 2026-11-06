"""M8.3 验收：时间回推 —— 区间、跨年、季节、模糊不乱猜。

这一节的验收最容易作弊：把 30 条用例的期望值照着实现的输出抄一遍，
就得到 100%。所以测试里钉的是**能独立推出答案**的性质：

  - 11 月 5 日说"昨天"，一定是 11 月 4 日（跟实现无关）
  - 1 月 15 日说"去年冬天"，一定回推到 2025 年 12 月（跨年）
  - "上周三"一定在过去，"下周三"一定在未来
  - "小时候"必须返回 None，猜一个年份是最坏的结局
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hippo.core.timeparse import parse_span

TZ = "Asia/Shanghai"
BASE = datetime(2026, 11, 5, 10, 0, tzinfo=ZoneInfo(TZ))   # 周四


def span(hint: str, base: datetime = BASE):
    return parse_span(hint, base=base, tz=TZ)


def d(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, ZoneInfo(TZ)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 基本方向
@pytest.mark.parametrize("hint,days", [("昨天", -1), ("前天", -2), ("大前天", -3),
                                       ("今天", 0), ("明天", 1)])
def test_day_offsets(hint, days):
    sp = span(hint)
    assert d(sp.start) == (BASE + timedelta(days=days)).strftime("%Y-%m-%d")
    assert sp.precision == "day"


def test_weekday_last_week_is_in_the_past():
    """`上周三` 必须落在过去。

    这条测试是修出来的：`_RE_WEEK` 的前缀里根本没有"上"，
    于是"上周三"被当成"最近的周三"——除非今天正好是周三，否则在未来。
    用户说上周三，事件被记到下周，差 13 天，而且不报错。
    """
    sp = span("上周三")
    assert d(sp.start) == "2026-10-28"
    assert sp.start < BASE.timestamp()


def test_weekday_next_week_is_in_the_future():
    sp = span("下周三")
    assert d(sp.start) == "2026-11-11"
    assert sp.start > BASE.timestamp()


def test_weekday_does_not_swallow_whole_week():
    """`上周三` 不能被"上周"整周区间吃掉。"""
    assert span("上周三").precision == "day"
    assert span("上周").precision == "week"


# ---------------------------------------------------------------- 跨年
def test_season_crosses_year():
    sp = span("去年冬天")
    assert (d(sp.start), d(sp.end)) == ("2025-12-01", "2026-03-01")
    assert sp.precision == "season"


def test_january_looking_back():
    """1 月 15 日说"去年冬天"，指的是**上一年** 12 月。

    按自然年算会得到 2026-12，那是未来——一个"去年"的表述落到了年底，
    而这种错只有跨年那两个月才会暴露。
    """
    base = datetime(2026, 1, 15, 10, 0, tzinfo=ZoneInfo(TZ))
    sp = span("去年冬天", base)
    assert (d(sp.start), d(sp.end)) == ("2025-12-01", "2026-03-01")


def test_january_last_month():
    base = datetime(2026, 1, 15, 10, 0, tzinfo=ZoneInfo(TZ))
    sp = span("上个月", base)
    assert (d(sp.start), d(sp.end)) == ("2025-12-01", "2026-01-01")


def test_season_without_year_is_most_recent():
    """11 月说"夏天"，指今年 6 月；11 月说"冬天"，指去年 12 月（还没到）。"""
    assert d(span("夏天").start) == "2026-06-01"
    assert d(span("冬天").start) == "2025-12-01"


# ---------------------------------------------------------------- 模糊
@pytest.mark.parametrize("hint", ["小时候", "以前", "很久以前", "春节"])
def test_fuzzy_returns_none(hint):
    """猜不出来就返回 None。给一个假年份是最坏的结果。"""
    assert span(hint) is None


def test_recent_gets_range_but_flagged():
    sp = span("前几天")
    assert sp is not None and sp.precision == "fuzzy"
    assert sp.guessed == 1
    assert sp.end - sp.start <= 8 * 86400


# ---------------------------------------------------------------- 向前回推
@pytest.mark.parametrize("hint,want", [
    ("两天前", "2026-11-03"),
    ("半个月前", "2026-10-21"),
    ("三个月前", "2026-08-01"),
    ("半年前", "2026-05-01"),
])
def test_before_offsets(hint, want):
    """"X 前"是事件最常用的表述，M5 只实现了"X 后"（那是给计划用的）。"""
    assert d(span(hint).start) == want


def test_before_half_month_is_not_last_month():
    """半个月前 ≠ 上个月。按月归一会把方向都说反。"""
    assert d(span("半个月前").start) != d(span("上个月").start)


# ---------------------------------------------------------------- 区间合法
@pytest.mark.parametrize("hint", ["昨天", "上周", "上个月", "去年", "去年冬天",
                                  "2026-03-15", "三天前", "下个月"])
def test_span_is_half_open_and_ordered(hint):
    sp = span(hint)
    assert sp.end is not None and sp.start < sp.end
    # 区间长度必须和精度量级相称：一天的区间不该有 30 天
    length = sp.end - sp.start
    cap = {"minute": 120, "hour": 7200, "day": 2 * 86400, "week": 8 * 86400,
           "month": 32 * 86400, "season": 95 * 86400, "year": 367 * 86400,
           "fuzzy": 8 * 86400}
    assert length <= cap[sp.precision], (hint, sp.precision, length)


def test_absolute_date_idempotent():
    """绝对日期不随"现在"漂移——同一天说，永远解析成同一天。"""
    a = span("2026-03-15")
    b = span("2026-03-15", datetime(2027, 8, 1, tzinfo=ZoneInfo(TZ)))
    assert d(a.start) == d(b.start) == "2026-03-15"
