"""M17.3 验收：时区与夏令时。

规则只有一条：**内部全部是 UTC epoch，只在展示时转本地**。
听起来是常识，但中文项目里最常见的错法是"用 UTC 天数做加减"——
`datetime.utcfromtimestamp(ts) + timedelta(days=1)` 在东八区会算错一天，
因为 UTC 的 00:00 是北京的 08:00，"明天"和"今天"在这里重叠了 8 小时。

所以这里钉的不是"能不能解析对"，是**在哪些时刻会算错**。
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from hippo.core.timeparse import parse_time

CN = "Asia/Shanghai"
NY = "America/New_York"


def at(y, m, d, h=10, tz=CN) -> datetime:
    return datetime(y, m, d, h, tzinfo=ZoneInfo(tz))


def utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def local(ts: int, tz: str) -> datetime:
    return datetime.fromtimestamp(ts, ZoneInfo(tz))


# ---------------------------------------------------------------- 东八区
def test_china_eight_am_is_utc_midnight():
    """手册验收 #1：8:00 → 00:00 UTC。"""
    s = parse_time("明天早上8点", ref=at(2026, 11, 5), tz=CN)
    assert utc(s.start).strftime("%Y-%m-%d %H:%M") == "2026-11-06 00:00"


def test_china_day_boundary_is_not_utc_day_boundary():
    """北京 11 月 6 日 00:30 = UTC 11 月 5 日 16:30。

    如果哪一层把 epoch 转成 UTC 日期再用，"11 月 6 日凌晨"会被记成 11 月 5 日。
    这条断言直接把这个换算写死。
    """
    s = parse_time("明天凌晨0点半", ref=at(2026, 11, 5), tz=CN)
    assert local(s.start, CN).strftime("%Y-%m-%d %H:%M") == "2026-11-06 00:30"
    assert utc(s.start).strftime("%Y-%m-%d %H:%M") == "2026-11-05 16:30"


# ---------------------------------------------------------------- 跨时区
def test_new_york_winter_is_utc_minus_five():
    """手册验收 #2：NY 用户 8:00 解析正确（冬令时 EST）。"""
    s = parse_time("明天早上8点", ref=at(2026, 11, 5, tz=NY), tz=NY)
    assert local(s.start, NY).strftime("%H:%M") == "08:00"
    assert utc(s.start).strftime("%H:%M") == "13:00"


def test_new_york_summer_is_utc_minus_four():
    """夏令时 EDT 差 4 小时。**同一个函数、同一句话**，offset 必须自己变。"""
    s = parse_time("明天早上8点", ref=at(2026, 7, 5, tz=NY), tz=NY)
    assert local(s.start, NY).strftime("%H:%M") == "08:00"
    assert utc(s.start).strftime("%H:%M") == "12:00"


def test_same_phrase_different_epoch_per_timezone():
    """同一句话在两个时区解析出的 epoch 必须不同。

    如果实现里把时区硬编码成 +08:00，这条会红——而"8 点"这个本地时刻
    在两边看起来仍然都是 8:00，光看展示是发现不了的。
    """
    ref = at(2026, 11, 5)
    a = parse_time("明天早上8点", ref=ref, tz=CN)
    b = parse_time("明天早上8点", ref=at(2026, 11, 5, tz=NY), tz=NY)
    assert a.start != b.start
    # 差 13 小时：都是本地 8:00，北京 = UTC+8，纽约冬令时 = UTC-5，
    # 两个 UTC 时刻相距 8 + 5 = 13 小时。
    assert abs(a.start - b.start) == 13 * 3600


# ---------------------------------------------------------------- 夏令时切换
def test_dst_spring_forward_day():
    """手册验收 #3：3 月第二个周日。

    2026-03-08 02:00 EST 直接跳到 03:00 EDT。3 月 7 日说的"明天"，落在 8 日，
    那天的 8:00 已经是 EDT（UTC-4）——不能沿用前一天的 -5。
    """
    s = parse_time("明天早上8点", ref=at(2026, 3, 7, tz=NY), tz=NY)
    assert local(s.start, NY).strftime("%Y-%m-%d %H:%M") == "2026-03-08 08:00"
    assert utc(s.start).strftime("%H:%M") == "12:00"


def test_dst_fall_back_day():
    """2026-11-01 02:00 EDT 回拨到 01:00 EST。那天的 8:00 是 EST（UTC-5）。"""
    s = parse_time("明天早上8点", ref=at(2026, 10, 31, tz=NY), tz=NY)
    assert local(s.start, NY).strftime("%Y-%m-%d %H:%M") == "2026-11-01 08:00"
    assert utc(s.start).strftime("%H:%M") == "13:00"


def test_nonexistent_local_time_does_not_crash():
    """凌晨 2:30 在 3 月 8 日**不存在**。不能崩，也不能悄悄回退到前一天。

    `zoneinfo` 的处理是把它映射到跳变之后的时刻（03:30 EDT）。
    我们不修正、只接受——因为用户说"凌晨两点半"时指的就是那个被跳过的时间点，
    挪半小时比报错或挪一天都更接近他的意思。
    """
    s = parse_time("明天凌晨2点半", ref=at(2026, 3, 7, tz=NY), tz=NY)
    assert s is not None
    got = local(s.start, NY)
    assert got.strftime("%Y-%m-%d") == "2026-03-08"
    assert got.strftime("%H:%M") in ("02:30", "03:30")


# ---------------------------------------------------------------- 存储
def test_stored_value_is_plain_epoch():
    """手册验收 #4：库内全 epoch。

    返回的必须是 int，不是带时区的 datetime、不是字符串、不是毫秒。
    毫秒和时间戳差 1000 倍，混进来时不会报错——只会让所有时间变成 1970 年。
    """
    s = parse_time("明天早上8点", ref=at(2026, 11, 5), tz=CN)
    assert isinstance(s.start, int)
    assert isinstance(s.end, int)
    # 秒级时间戳在 2026 年约为 1.7e9；毫秒级会是 1.7e12
    assert 1_700_000_000 < s.start < 2_000_000_000


def test_invalid_timezone_falls_back_instead_of_crashing():
    """配置里写了个不存在的时区，不能让整条写入链路挂掉。

    降级到系统本地时间并不完美，但"记忆没存进去"比"时间差一小时"严重得多。
    """
    s = parse_time("明天早上8点", ref=datetime(2026, 11, 5, 10), tz="Mars/Olympus")
    assert s is not None
