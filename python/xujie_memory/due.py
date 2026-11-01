"""把「下个月」「月底」落到一个确切的日子。

承诺如果只有一个字符串 `due_hint`，它能回答"他答应过什么"，
但回答不了"**现在该催谁**"。而仿人记忆里，"该提醒却没说"和"忘了"是一回事。

所以这里做一件很小、很笨、但必须确定的事：把中文相对时间换成绝对时间。

    明天 / 后天 / 大后天        → +1 / +2 / +3 天
    下周 / 下星期 / 下礼拜      → +7 天（不是"下周一"，中文没那么精确）
    下个月 / 下月               → 同一日，月末则夹到月末（1/31 + 1月 = 2/28）
    月底 / 月末                 → 本月最后一天
    年底 / 年前                 → 12-31
    过两天 / 过几天             → +2 / +3
    回头 / 改天 / 以后 / 待会   → **不算**（没有可催的日子，只留 due_hint）

三条设计原则：

1. **算不出来就说算不出来。** 「回头」「改天」「发工资」没有一个可执行的日期，
   硬给一个（比如 +3 天）会在不该催的时候催，比不催更伤信任。
2. **宁可晚一天，不要早一天。** 「月底」取本月最后一天，「下个月」取同日的
   23:59:59——催早了是打扰，催晚了顶多少一次。
3. **不引第三方库。** 只用 `datetime` 和 `calendar`。这一层越薄越好，
   因为它要能被单测钉死，而不是被库的版本行为左右。
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone

# 明确到"天"的词。值是偏移天数。
_DAY_OFFSETS = {
    "大后天": 3,
    "后天": 2,
    "明天": 1,
    "明日": 1,
    "今天": 0,
    "今日": 0,
    "过两天": 2,
    "过几天": 3,
}

# 只在"下个月/月底"这类月级短语里才成立的词，单独处理，不放进偏移表。
_MONTH_PHRASES = ("下个月", "下月", "下一个月")
_MONTH_END = ("月底", "月末")
_YEAR_END = ("年底", "年前", "年末")
_WEEK_PHRASES = ("下周", "下星期", "下礼拜", "下个星期", "下个礼拜")

# 算不出确切日子的。**不是失败**，是"这件事没有可执行的时间"。
_VAGUE = (
    "回头", "改天", "以后", "将来", "下次", "下回", "待会", "一会儿",
    "等下", "晚点", "等会", "发工资", "发了工资", "有空", "看情况",
)


def _parse(now: str) -> datetime:
    dt = datetime.fromisoformat(now)
    if dt.tzinfo is None:
        # 内核时钟可能给 naive 时间（测试里常见）。统一按 UTC 处理，
        # 不在这一层猜"本地时区"——猜错会让所有截止日整体偏一天。
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _end_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=23, minute=59, second=59, microsecond=0)


def _add_months(dt: datetime, months: int) -> datetime:
    """加月，日按月末夹住。1/31 + 1 月 = 2/28（不是 3/3）。"""
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def resolve_due(text: str, now: str) -> str | None:
    """从一句话里算出绝对截止时间。算不出返回 None。

    返回的是**那天 23:59:59**——承诺说的是"月底前"，不是"月底那天的零点"。
    """
    base = _parse(now)

    # 顺序有讲究：先长词后短词。「大后天」必须先于「后天」，
    # 「下个月」必须先于「下个星期」被判断（两者共享「下个」前缀）。
    for phrase in _YEAR_END:
        if phrase in text:
            return _end_of_day(base.replace(month=12, day=31)).isoformat()

    for phrase in _MONTH_END:
        if phrase in text:
            last = calendar.monthrange(base.year, base.month)[1]
            return _end_of_day(base.replace(day=last)).isoformat()

    for phrase in _MONTH_PHRASES:
        if phrase in text:
            return _end_of_day(_add_months(base, 1)).isoformat()

    for phrase in _WEEK_PHRASES:
        if phrase in text:
            return _end_of_day(base + timedelta(days=7)).isoformat()

    for phrase, days in sorted(_DAY_OFFSETS.items(), key=lambda kv: -len(kv[0])):
        if phrase in text:
            return _end_of_day(base + timedelta(days=days)).isoformat()

    return None


def is_vague(text: str) -> bool:
    """说的是"以后"，但没有可执行的日期。"""
    return any(w in text for w in _VAGUE)


def describe(due_at: str | None) -> str:
    """给人看的一句话。用于提醒。"""
    if not due_at:
        return "没说什么时候"
    dt = datetime.fromisoformat(due_at)
    return f"{dt.month} 月 {dt.day} 日"
