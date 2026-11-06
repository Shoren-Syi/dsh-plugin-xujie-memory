"""中文时间表述 → 绝对时间戳（M5 最小实现）。

M19 会把它换成完整版（重复规则、跨时区、模糊时段"下午晚些时候"）。
现在只需要支撑一件事：**抽取器给出的 `due_hint` 能落成 plans.due_at**。
`plans.due_at` 是 NOT NULL，所以解析不出来就只能丢——这是这一层必须存在的理由。

设计原则：
  - 宁可标 `guessed=1`，不要猜得理直气壮。调用方据此决定要不要在记忆里
    加"约""大概"。
  - 只认**明确写在文本里**的成分。"明天"就是明天，不推断几点；
    没写时刻就默认 09:00 并标记 guessed。
  - 不做"下个月""年底"这种开放式推断。解析不了返回 None，让调用方跳过。

时区固定用传入的 tz（默认 Asia/Shanghai）。用 UTC 天数做加减会错一天，
这是所有中文时间解析器的第一个坑。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

__all__ = ["parse_when", "parse_due", "parse_span", "parse_time", "ParsedTime", "TimeSpan",
           "cn_int", "LIFE_STAGE_AGES", "EVENT_ANCHORS"]

_CN_DIGIT = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

_DAY_WORDS = {
    "今天": 0, "今日": 0, "今晚": 0, "今早": 0, "今儿": 0,
    "明天": 1, "明日": 1, "明早": 1, "明晚": 1, "明儿": 1,
    "后天": 2, "大后天": 3, "昨天": -1, "前天": -2, "大前天": -3,
}

# "今晚"这类词自带时段含义。不处理的话会被默认成早上 9 点——
# 用户说"今晚提醒我"，21 点才提醒，等于没提醒。
_DAY_DEFAULT_HOUR = {"今晚": 20, "明晚": 20, "今早": 8, "明早": 8,
                     "今儿": 9, "明儿": 9}

_WEEKDAY = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
            "日": 7, "天": 7, "七": 7}

_PART_HOUR = {
    "凌晨": 0, "早上": 0, "早晨": 0, "上午": 0, "中午": 12,
    "下午": 12, "傍晚": 12, "晚上": 12, "夜里": 12, "深夜": 12, "半夜": 12,
}

_RE_DAY = re.compile("|".join(sorted(_DAY_WORDS, key=len, reverse=True)))
# 前缀里必须有"上/上上"。漏了它们，"上周三"会被当成"最近的周三"（除非今天是周三，
# 否则就是未来），事件时间往前错一周。这个 bug 不会报错，只会让"上周"的事出现在下周。
_RE_WEEK = re.compile(r"(上上|下下|上|下|这|本)?(?:周|星期|礼拜)([一二三四五六日天七])")
_RE_YMD = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?")
_RE_MD = re.compile(r"(\d{1,2})月(\d{1,2})[日号]")
_RE_HM = re.compile(r"(\d{1,2})[:：](\d{2})")
_RE_HOUR = re.compile(r"(\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*(?:点|时)")
_RE_PART = re.compile("|".join(sorted(_PART_HOUR, key=len, reverse=True)))
_RE_REL_UNIT = re.compile(r"(\d+|[一二两三四五六七八九十]+)\s*(分钟|小时|钟头|天|周|星期|月)后")


class ParsedTime:
    __slots__ = ("ts", "precision", "guessed", "matched")

    def __init__(self, ts: int, precision: str, guessed: int, matched: str):
        self.ts = ts
        self.precision = precision      # minute / hour / day
        self.guessed = guessed          # 1 = 有猜测成分
        self.matched = matched

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<ParsedTime {datetime.fromtimestamp(self.ts)} "
                f"{self.precision} guessed={self.guessed} {self.matched!r}>")


def cn_int(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _CN_DIGIT:
        return _CN_DIGIT[s]
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = _CN_DIGIT.get(head, 1) if head else 1
        ones = _CN_DIGIT.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return None


# 旧名保留：M5 的代码里还有引用，改名不该顺手制造一处 ImportError。
_cn_int = cn_int


def _now(base: datetime | None, tz: str) -> datetime:
    if base is not None:
        return base
    try:
        return datetime.now(ZoneInfo(tz or "Asia/Shanghai"))
    except Exception:  # noqa: BLE001
        return datetime.now()


def parse_when(hint: str, *, base: datetime | None = None,
               tz: str = "Asia/Shanghai",
               default_hour: int = 9) -> ParsedTime | None:
    """解析中文时间表述。解析不了返回 None。

    `base` 只给测试用（固定"现在"），生产传 None。
    """
    text = (hint or "").strip()
    if not text:
        return None
    now = _now(base, tz)
    guessed = 0
    day: datetime | None = None
    precision = "day"
    hit: list[str] = []

    # ---- 1. 绝对日期：2026-11-05 / 11月5日
    m = _RE_YMD.search(text)
    if m:
        try:
            day = now.replace(year=int(m.group(1)), month=int(m.group(2)),
                              day=int(m.group(3)), hour=0, minute=0,
                              second=0, microsecond=0)
            hit.append(m.group(0))
        except ValueError:
            return None
    if day is None:
        m = _RE_MD.search(text)
        if m:
            try:
                cand = now.replace(month=int(m.group(1)), day=int(m.group(2)),
                                   hour=0, minute=0, second=0, microsecond=0)
            except ValueError:
                return None
            # 已经过去的月日 → 认为说的是明年
            if cand.date() < now.date():
                cand = cand.replace(year=cand.year + 1)
            day = cand
            hit.append(m.group(0))

    # ---- 2. 周几：下周三 / 周五
    if day is None:
        m = _RE_WEEK.search(text)
        if m:
            prefix, wd_ch = m.group(1) or "", m.group(2)
            target = _WEEKDAY.get(wd_ch)
            if target is None:
                return None
            today_wd = now.isoweekday()
            delta = target - today_wd
            if prefix in ("下", "下下"):
                delta += 7 * (2 if prefix == "下下" else 1)
            elif prefix == "上上":
                delta -= 14
            elif prefix == "上":
                # "上周三"永远在过去，减 7 天不用判断方向。
                # 原来没有这个分支，"上周三"落到下面的 else，被当成"未来最近的那个周三"——
                # 于是用户说上周三，事件被记到下周。差了整整 13 天。
                delta -= 7
            elif prefix in ("这", "本"):
                if delta < 0:
                    delta += 7
            else:
                # 只说"周三"：本周还没到就是本周，过了就是下周
                if delta < 0:
                    delta += 7
            day = (now + timedelta(days=delta)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            hit.append(m.group(0))

    # ---- 3. 相对日：明天 / 后天
    if day is None:
        m = _RE_DAY.search(text)
        if m:
            delta = _DAY_WORDS[m.group(0)]
            day = (now + timedelta(days=delta)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            hit.append(m.group(0))

    # ---- 4. 相对量：3小时后 / 两天后
    if day is None:
        m = _RE_REL_UNIT.search(text)
        if m:
            n = cn_int(m.group(1))
            unit = m.group(2)
            if n is not None:
                if unit in ("分钟",):
                    return ParsedTime(int((now + timedelta(minutes=n)).timestamp()),
                                      "minute", 0, m.group(0))
                if unit in ("小时", "钟头"):
                    return ParsedTime(int((now + timedelta(hours=n)).timestamp()),
                                      "hour", 0, m.group(0))
                days = n * (1 if unit == "天" else 7 if unit in ("周", "星期") else 30)
                day = (now + timedelta(days=days)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                hit.append(m.group(0))

    # ---- 5. 时刻
    hour: int | None = None
    minute = 0
    m = _RE_HM.search(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        precision = "minute"
        hit.append(m.group(0))
    else:
        m = _RE_HOUR.search(text)
        if m:
            hour = cn_int(m.group(1))
            if hour is None or hour > 24:
                return None
            precision = "hour"
            hit.append(m.group(0))
            if "半" in text[m.end():m.end() + 2]:
                minute = 30
                precision = "minute"

    # ---- 6. 时段词修正 12 小时制
    part = _RE_PART.search(text)
    if part:
        hit.append(part.group(0))
        if hour is not None:
            base_h = _PART_HOUR[part.group(0)]
            if base_h == 12 and hour < 12:
                hour += 12
            elif base_h == 0 and hour == 12:
                hour = 0

    # ---- 7. 拼装
    if day is None and hour is None:
        # 只有"下个月"这种，解析不了
        return None

    if day is None:
        # 只有时刻：今天；已经过了就顺延到明天
        cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
            guessed = 1
        return ParsedTime(int(cand.timestamp()), precision, guessed, "".join(hit))

    if hour is None:
        hour = next((h for w, h in _DAY_DEFAULT_HOUR.items() if w in text), default_hour)
        guessed = 1
        precision = "day"

    try:
        final = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    return ParsedTime(int(final.timestamp()), precision, guessed, "".join(hit))


# ============================================================
# M8.3 区间解析
# ============================================================
#
# `parse_when` 回答"什么时候"，返回一个**点**。事件需要的是"哪一段时间"：
#   - 衰减要知道"这件事多久以前"（点够了）
#   - 检索要知道"这段时间里发生了什么"（需要区间）
#   - "去年冬天"根本无法表达成一个点，硬塞就会变成 12 月 1 日 9 点整
#
# 所以这一层只做一件事：把粗粒度表述变成 [start, end)。
# 细粒度（几点几分）仍走 parse_when，这里只负责补一个 end。

# `春节` 不是春天。没有这个负向断言，"春节回家"会被记成 3~5 月，
# 一条时间错了 3 个月的记忆，比没有时间更糟——它会被当成真的。
_RE_SEASON = re.compile("春天|夏季|夏天|秋天|秋季|冬天|冬季|春(?!节)|夏(?!天)|秋|冬(?!至)")
_RE_YEAR_REL = re.compile("前年|去年|今年|明年")
_RE_MONTH_REL = re.compile("上上个月|上个月|上月|这个月|本月|下下个月|下个月|下月")
_RE_WEEK_REL = re.compile("上上周|上周|上礼拜|这周|本周|这礼拜|下周|下礼拜")
_RE_MONTH_ABS = re.compile(r"(\d{4})年(\d{1,2})月|(\d{1,2})月")
_RE_FUZZY = re.compile("小时候|童年|很久以前|很早以前|以前|从前|当年")
# "那年夏天"的"那年"不指任何年份。带指代的那几个（去年/前年/今年/明年）
# 由 `_RE_YEAR_REL` 另行处理，所以要在这里排除掉。
_RE_ANON_YEAR = re.compile("那一年|那年")
_RE_RECENT = re.compile("前几天|这两天|这几天|最近")
# "X 前"：用户最常用的相对表述之一。M5 只做了"X 后"（因为计划要向后），
# 事件全在过去，恰恰只有"前"有用。缺了这条，"三天前"整句话没有时间。
_RE_BEFORE = re.compile(
    r"(\d+|[一二两三四五六七八九十]+|半)\s*"
    r"(分钟|小时|钟头|天|周|星期|个月|月|年)(?:以)?前")
# "两个半月前" 必须先于 `_RE_BEFORE` 匹配。后者会在"半月前"处命中，
# 把"两个"当没看见——2.5 个月被算成 0.5 个月，差 60 天。
_RE_BEFORE_HALF = re.compile(
    r"(\d+|[一二两三四五六七八九十]+)\s*个?\s*半\s*(个月|月|年)(?:以)?前")
_RE_NEW_YEAR = re.compile("元旦")

_SEASON_START = {"春": 3, "夏": 6, "秋": 9, "冬": 12}


class TimeSpan:
    __slots__ = ("start", "end", "precision", "guessed", "matched")

    def __init__(self, start: int, end: int | None, precision: str,
                 guessed: int, matched: str):
        self.start = start
        self.end = end
        self.precision = precision
        self.guessed = guessed
        self.matched = matched

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        f = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "-"
        return (f"<TimeSpan {f(self.start)} ~ {f(self.end)} "
                f"{self.precision} guessed={self.guessed} {self.matched!r}>")


def _month_start(y: int, m: int, tz: str) -> datetime:
    m2, y2 = (m, y)
    while m2 > 12:
        m2 -= 12
        y2 += 1
    while m2 < 1:
        m2 += 12
        y2 -= 1
    return datetime(y2, m2, 1, tzinfo=ZoneInfo(tz))


def _span_month(y: int, m: int, tz: str) -> tuple[int, int]:
    return (int(_month_start(y, m, tz).timestamp()),
            int(_month_start(y, m + 1, tz).timestamp()))


def _span_season(y: int, start_month: int, tz: str) -> tuple[int, int]:
    return (int(_month_start(y, start_month, tz).timestamp()),
            int(_month_start(y, start_month + 3, tz).timestamp()))


def parse_span(hint: str, *, base: datetime | None = None,
               tz: str = "Asia/Shanghai") -> TimeSpan | None:
    """把时间表述解析成 [start, end)。

    返回 None 表示"完全没线索"（"小时候""以前"），调用方应落
    `time_guessed=1` 并把 event_time 设成 ingest_time —— 不猜比猜错好。

    返回 `precision='fuzzy'` 表示"有大致的范围但不精确"（"前几天"），
    区间给了，标记也给了，下游可以按需降权。
    """
    text = (hint or "").strip()
    if not text:
        return None

    now = _now(base, tz)
    year = now.year

    # ---- 0. 完全模糊：连范围都给不出
    if _RE_FUZZY.search(text):
        return None

    # ---- 0.1 "那年 X"：有季节没有年份，等于什么都没说
    #
    # "那年夏天"里的"那年"不指向任何年份。再往下走，季节分支会给它安上
    # **今年**——一句话把过去的某件事记成了今年，误差可能十年，且不报错。
    # 负向断言排除"去年/前年/今年/明年"，那四个是有指代的。
    if _RE_ANON_YEAR.search(text) and not _RE_YEAR_REL.search(text):
        return None

    # ---- 0.5 "X 个半 单位前"：两个半月前 / 一年半前
    m = _RE_BEFORE_HALF.search(text)
    if m:
        n = cn_int(m.group(1))
        if n is not None:
            unit = m.group(2)
            days = (n + 0.5) * (365 if unit == "年" else 30)
            d0 = (now - timedelta(days=days)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            # 一天宽、标 day：2.5 个月这种表述本来就不精确到天，
            # precision 给 day 是为了让下游有个能比较的区间边界。
            return TimeSpan(int(d0.timestamp()),
                            int((d0 + timedelta(days=1)).timestamp()),
                            "day", 1, m.group(0))

    # ---- 0.5 "X 前"：三天前 / 半个月前 / 两年前
    m = _RE_BEFORE.search(text)
    if m:
        raw, unit = m.group(1), m.group(2)
        n = 0.5 if raw == "半" else cn_int(raw)
        if n is not None:
            if unit in ("分钟",):
                st = now - timedelta(minutes=n)
                return TimeSpan(int(st.timestamp()), int(st.timestamp()) + 60,
                                "minute", 1, m.group(0))
            if unit in ("小时", "钟头"):
                st = now - timedelta(hours=n)
                return TimeSpan(int(st.timestamp()), int(st.timestamp()) + 3600,
                                "hour", 1, m.group(0))
            if unit in ("天", "周", "星期"):
                days = int(n * (7 if unit in ("周", "星期") else 1))
                d0 = (now - timedelta(days=days)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                return TimeSpan(int(d0.timestamp()),
                                int((d0 + timedelta(days=1)).timestamp()),
                                "day", 1, m.group(0))
            if unit in ("个月", "月"):
                if raw == "半":
                    # "半个月前"不是"上个月"。按月粒度归会把 10 月 21 日
                    # 说成 11 月 1 日——方向都反了。按 15 天算，标 day。
                    d0 = (now - timedelta(days=15)).replace(
                        hour=0, minute=0, second=0, microsecond=0)
                    return TimeSpan(int(d0.timestamp()),
                                    int((d0 + timedelta(days=1)).timestamp()),
                                    "day", 1, m.group(0))
                sm = _month_start(now.year, now.month - int(round(n)), tz)
                return TimeSpan(int(sm.timestamp()),
                                int(_month_start(sm.year, sm.month + 1, tz).timestamp()),
                                "month", 1, m.group(0))
            if unit == "年":
                if raw == "半":
                    # "半年前"不是"去年"。按年粒度会把 5 月说成 1 月 1 日，
                    # 误差半年——正好是这句话本身的信息量。
                    sm = _month_start(now.year, now.month - 6, tz)
                    return TimeSpan(int(sm.timestamp()),
                                    int(_month_start(sm.year, sm.month + 1, tz).timestamp()),
                                    "month", 1, m.group(0))
                y = now.year - int(round(n))
                return TimeSpan(int(_month_start(y, 1, tz).timestamp()),
                                int(_month_start(y + 1, 1, tz).timestamp()),
                                "year", 1, m.group(0))

    # ---- 0.6 元旦：中文里唯一一个不用查农历就能定死日期的节日
    if _RE_NEW_YEAR.search(text):
        y = now.year if now.month > 1 or now.day > 1 else now.year
        if now.month > 1:
            y = now.year + 1
        d0 = datetime(y, 1, 1, tzinfo=ZoneInfo(tz))
        return TimeSpan(int(d0.timestamp()),
                        int(datetime(y, 1, 2, tzinfo=ZoneInfo(tz)).timestamp()),
                        "day", 0, "元旦")

    # ---- 1. 最近几天：给一个范围，但明确标 fuzzy
    if _RE_RECENT.search(text):
        end = int(now.timestamp())
        start = int((now - timedelta(days=6)).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())
        return TimeSpan(start, end, "fuzzy", 1, _RE_RECENT.search(text).group(0))

    # ---- 2. 年偏移：去年 / 前年 / 今年 / 明年
    m = _RE_YEAR_REL.search(text)
    if m:
        shift = {"前年": -2, "去年": -1, "今年": 0, "明年": 1}[m.group(0)]
        y = year + shift
        # "去年冬天"：年偏移只用来定基准年，季节/月份在后面接着处理
        rest = text.replace(m.group(0), "")
        season = _RE_SEASON.search(rest)
        if season:
            sm = _SEASON_START[season.group(0)[0]]
            s, e = _span_season(y, sm, tz)
            return TimeSpan(s, e, "season", 0, f"{m.group(0)}{season.group(0)}")
        mm = _RE_MONTH_ABS.search(rest)
        if mm:
            mo = int(mm.group(2) or mm.group(3))
            s, e = _span_month(y, mo, tz)
            return TimeSpan(s, e, "month", 0, f"{m.group(0)}{mm.group(0)}")
        s = int(_month_start(y, 1, tz).timestamp())
        e = int(_month_start(y + 1, 1, tz).timestamp())
        return TimeSpan(s, e, "year", 0, m.group(0))

    # ---- 3. 季节（无年份）：取"最近一次已经开始的"
    season = _RE_SEASON.search(text)
    if season and not _RE_MONTH_ABS.search(text):
        sm = _SEASON_START[season.group(0)[0]]
        sy = year if now.month >= sm else year - 1
        # 冬天跨年：12 月到次年 2 月。11 月说"冬天"指的是 12 月开始的这个冬天，
        # 但 11 月还没到 12 月，所以取"最近一次开始的"（去年 12 月）。
        s, e = _span_season(sy, sm, tz)
        return TimeSpan(s, e, "season", 1 if sy != year else 0, season.group(0))

    # ---- 4. 月份偏移：上个月 / 下个月
    m = _RE_MONTH_REL.search(text)
    if m:
        shift = {"上上个月": -2, "上个月": -1, "上月": -1,
                 "这个月": 0, "本月": 0, "下个月": 1, "下月": 1,
                 "下下个月": 2}[m.group(0)]
        s, e = _span_month(year, now.month + shift, tz)
        return TimeSpan(s, e, "month", 0, m.group(0))

    # ---- 5. 周偏移：上周 / 这周 / 下周
    #     `上周三` 不是"上周"，是上周的周三。周偏移只能在没有具体星期几时用，
    #     否则"上周三"会被整周区间吃掉，事件时间落在一周前而不是三天前。
    m = _RE_WEEK_REL.search(text)
    if m and not re.search(r"(?:周|星期|礼拜)[一二三四五六日天七]", text):
        shift = {"上上周": -2, "上周": -1, "上礼拜": -1,
                 "这周": 0, "本周": 0, "这礼拜": 0, "下周": 1, "下礼拜": 1}[m.group(0)]
        monday = (now - timedelta(days=now.isoweekday() - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = monday + timedelta(days=7 * shift)
        return TimeSpan(int(start.timestamp()),
                        int((start + timedelta(days=7)).timestamp()),
                        "week", 0, m.group(0))

    # ---- 6. 绝对年月：2025年3月 / 3月（不带日）
    m = _RE_MONTH_ABS.search(text)
    if m and not re.search(r"\d{1,2}[日号]", text):
        y = int(m.group(1)) if m.group(1) else year
        mo = int(m.group(2) or m.group(3))
        s, e = _span_month(y, mo, tz)
        return TimeSpan(s, e, "month", 0, m.group(0))

    # ---- 7. 交给细粒度解析器，补一个 end
    p = parse_when(hint, base=base, tz=tz)
    if p is None:
        return None
    start = p.ts
    if p.precision == "minute":
        end = start + 60
    elif p.precision == "hour":
        end = start + 3600
    else:
        # day 精度时**起点也要归零**。parse_when 对"明天"给的是 09:00
        # （点事件的默认时刻），直接拿来当区间起点会丢掉当天 0~9 点：
        # 用户说"明天几点开会"，会议 8:00，区间却是 [明天09:00, 后天)，
        # 那条事件被时间过滤砍掉——M10 评测里就是这样漏的（事故 59）。
        day = datetime.fromtimestamp(start, ZoneInfo(tz)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = int(day.timestamp())
        end = int((day + timedelta(days=1)).timestamp())
    return TimeSpan(start, end, p.precision, p.guessed, p.matched)


# ============================================================
# M17.1 人生阶段 / 事件锚
# ============================================================
#
# `parse_span` 只管"文本里写明了的时间"。有两类表述它必然解析不出来：
#
#   人生阶段   "小时候""刚毕业那会儿"   —— 缺一个"这个人多大"的锚
#   事件锚     "疫情那会儿""非典那年"   —— 缺一张"世界发生过什么"的表
#
# 两者都**不该猜成一个点**。手册的原话是"不硬猜"，落到实现上是：
# 给一个**宽到不会错**的区间，并标 `precision='fuzzy'`。
# 下游看到 fuzzy 就知道"这条别按点用，只当'大概那时候'"。

# 年龄段（按周岁近似）。区间是半开 [起, 止)：小学 7~12 岁，13 岁生日之后不算。
LIFE_STAGE_AGES: dict[str, tuple[int, int]] = {
    "小时候": (3, 13),
    "童年": (3, 13),
    "小学": (7, 13),
    "初中": (12, 16),
    "高中": (15, 19),
    "大学": (18, 23),
    "刚毕业": (22, 26),
    "毕业那会儿": (22, 25),
    "刚工作": (22, 26),
    "工作后": (22, 35),
    "上班后": (22, 35),
}

# 长词在前："刚毕业"必须先于"毕业"被匹配，否则标签会落到错的那一格。
_RE_LIFE = re.compile(
    "|".join(sorted(LIFE_STAGE_AGES, key=len, reverse=True))
    + r"(?:的时候|那会儿|那阵子|期间|时代|阶段|时)?")

# 事件锚：(起始年, 起月, 结束年, 结束月)
#
# 选日期的标准不是"精确"，是**不会有第二种理解**：
#   疫情  2020-01 起（武汉封城），2023-01 止（乙类乙管）。三年，所有人认这个范围
#   非典  2003-03 起，2003-08 止（世卫解除旅行警告）
#   汶川  2008-05 到 6 月（主震与堰塞湖处置期）
#   奥运  2008-08 北京夏季奥运会
#   金融危机 2008-09 雷曼倒闭起，2009-06 触底止
#
# 故意**不收录**"地震那年"这种无指代说法：在四川人嘴里是 2008，
# 在唐山人嘴里是 1976。用户没说哪个，我们就不该替他选。
EVENT_ANCHORS: dict[str, tuple[int, int, int, int]] = {
    "疫情": (2020, 1, 2023, 1),
    "新冠": (2020, 1, 2023, 1),
    "非典": (2003, 3, 2003, 8),
    "汶川": (2008, 5, 2008, 6),
    "奥运": (2008, 8, 2008, 9),
    "金融危机": (2008, 9, 2009, 6),
}

_RE_ANCHOR = re.compile("|".join(sorted(EVENT_ANCHORS, key=len, reverse=True)))

# 没有年龄锚时假设的年龄。**这个数只影响"往哪边放宽"，不影响精度**：
# 拿它算出来的区间会再向两端各扩几年，把 22~40 岁的用户都盖住。
ASSUME_AGE = 30


def _span_from_months(y0: int, m0: int, y1: int, m1: int, tz: str) -> tuple[int, int]:
    return (int(datetime(y0, m0, 1, tzinfo=ZoneInfo(tz)).timestamp()),
            int(datetime(y1, m1, 1, tzinfo=ZoneInfo(tz)).timestamp()))


def _parse_anchor(text: str, tz: str) -> TimeSpan | None:
    m = _RE_ANCHOR.search(text)
    if not m:
        return None
    y0, m0, y1, m1 = EVENT_ANCHORS[m.group(0)]
    start, end = _span_from_months(y0, m0, y1, m1, tz)
    # guessed=0：区间是确定的。**宽**是这件事的性质，不是猜测。
    return TimeSpan(start, end, "fuzzy", 0, m.group(0))


def _parse_life_stage(text: str, now: datetime, tz: str, *,
                      age: int | None, birth_year: int | None,
                      assume_age: int) -> TimeSpan | None:
    m = _RE_LIFE.search(text)
    if not m:
        return None
    word = m.group(0)
    key = next((k for k in LIFE_STAGE_AGES if word.startswith(k)), None)
    if key is None:                     # pragma: no cover - 正则与词表同源
        return None
    lo, hi = LIFE_STAGE_AGES[key]

    anchored = True
    if birth_year:
        by = int(birth_year)
    elif age:
        # 年龄是"今年多大"，出生年 = 今年 - 年龄。这里不区分生日过没过，
        # 因为下游要的是**十年量级的区间**，差一年没有意义。
        by = now.year - int(age)
    else:
        by = now.year - assume_age
        anchored = False

    y0, y1 = by + lo, by + hi
    if not anchored:
        # 无锚时往两端各放宽：把 22~40 岁的人全盖住。
        # 宁可给一个"宽到不会错"的区间——时间过滤是包含关系，
        # 宽区间只是多捞，窄区间会漏。
        y0 -= 5
        y1 += 4
    # 人生阶段全在过去（"上小学"是现在进行时，但起点也在过去）。
    # 不截的话，"工作后"在 30 岁用户身上会算出 2035 年——一个未来的区间，
    # 会让"最近半年发生的事"这类查询把它当成未来事件排除。
    y1 = min(y1, now.year + 1)
    start, end = _span_from_months(max(1900, y0), 1, max(1901, y1), 1, tz)
    now_ts = int(now.timestamp())
    if end > now_ts:
        # 截到今天，而不是截到"今年 1 月 1 日"或"明年 1 月 1 日"：
        # "工作后"的意思是"从那时到现在"，终点就是现在。
        end = now_ts
    return TimeSpan(start, end, "fuzzy", 0 if anchored else 1, key)


def parse_time(hint: str, *, ref: datetime | None = None,
               tz: str = "Asia/Shanghai", age: int | None = None,
               birth_year: int | None = None,
               assume_age: int = ASSUME_AGE) -> TimeSpan | None:
    """M17.1 统一入口：人生阶段 → 事件锚 → 通用解析。

    顺序不是随便排的：

      1. **人生阶段必须最先。** `parse_span` 的 `_RE_FUZZY` 里有"小时候"
         "童年"，先跑它就会被挡成 None，区间永远拿不到。
      2. **事件锚第二。** "疫情那会儿"不含任何日期词，通用解析给不出区间。
      3. 最后兜底给 `parse_span`。

    两者同时出现时（"疫情期间我还在上大学"）取**交集**：疫情是 2020~2023，
    大学是 2014~2023，交集才是用户真正说的时间。交集为空则以事件锚为准——
    它比年龄段更特定。
    """
    text = (hint or "").strip()
    if not text:
        return None
    now = ref or _now(None, tz)

    life = _parse_life_stage(text, now, tz, age=age, birth_year=birth_year,
                             assume_age=assume_age)
    anchor = _parse_anchor(text, tz)

    if life and anchor:
        s, e = max(life.start, anchor.start), min(life.end, anchor.end)
        if s < e:
            return TimeSpan(s, e, "fuzzy", max(life.guessed, anchor.guessed),
                            f"{anchor.matched}+{life.matched}")
        return anchor
    if life:
        return life
    if anchor:
        return anchor
    return parse_span(hint, base=ref, tz=tz)


# ============================================================
# M19.1 计划时刻解析
# ============================================================
#
# parse_when 回答"这件事发生在什么时候"（事件，过去或现在都合法）；
# parse_due 回答"用户想让我什么时候做事"（计划，**必须是未来**）。
# 两者的差异落在三处：
#
#   1. 重复词（"每天""每周三"）不算日期，算周期——parse_when 会把
#      "每天早上7点半"解析成"明天7点半"，日期对，但 recur 丢了，
#      触发一次就完了。recur 必须在这里单独检测。
#   2. 计划不允许落在过去。"今天下午3点"在 16 点说出来，不是
#      用户说错了就是复述历史——两种都不该落库硬触发，反问一次。
#   3. 事件解析失败可以降级成"无时间的事件"，计划解析失败没法降级
#      （due_at NOT NULL）——必须反问，见 persist 的 doubts 落库。

_RE_RECUR_DAILY = re.compile("每天|天天|每日")
_RE_RECUR_WEEKLY = re.compile("每周|每星期|每礼拜")

# ---- 计划专用的粗粒度日期（M26 评测暴露）
#
# 为什么不在 parse_when 里加：parse_when 是**事件**时间解析器，它服务
# 的是"这句话发生在什么时候"。`下个月十号` 当事件时间说得通，但当计划
# 时间时必须额外检查"是不是已经过去了"——两套语义混在一个函数里，
# 迟早有人拿事件分支去解析计划。
#
# 更直接的原因：`下个月十号早上八点` 在 parse_when 里会走"只有时刻"
# 分支，得到**明天早上八点**。它匹配到了"八点"，把"下个月十号"当成了
# 无关的修饰语。这是 M26 语料实测出来的：搬家计划本该落在 40 天后，
# 实际落在第二天，会提前一个月提醒用户搬家。
_RE_PLAN_MONTH_DAY = re.compile(
    r"(上上|下下|上|下|这|本)?个?月(初|[一二三四五六七八九十\d]{1,3})[日号]")
_RE_PLAN_NEXT_YEAR_MD = re.compile(
    r"明年\s*([一二三四五六七八九十\d]{1,2})\s*月")
_RE_PLAN_MD = re.compile(r"([一二三四五六七八九十\d]{1,2})\s*月")
_RE_PLAN_YEAR_END = re.compile("年底|年末|今年底|今年年底")
_RE_PLAN_YEAR_AFTER = re.compile(r"([一二两三四五六七八九十\d]+)\s*年后")
# "这句话里有个远期日期词"的粗判据，只用来防止 parse_when 只抓到时刻
# 必须是原生字符串：非原生字符串里的 \d 在 3.12 会报 SyntaxWarning，
# 未来版本直接变 SyntaxError（实测打包运行时日志里就刷了这条警告）。
_RE_PLAN_FAR = re.compile(r"下个?月|下下个?月|明年|年底|年后|[一二三四五六七八九十\d]{1,2}\s*月")

def _add_months(dt: datetime, n: int) -> datetime:
    """加月并夹到月末。1/31 + 1 月 = 2/28，不是 3/3。"""
    total = dt.year * 12 + (dt.month - 1) + n
    y, m = divmod(total, 12)
    m += 1
    for day in (dt.day, 30, 29, 28):
        try:
            return dt.replace(year=y, month=m, day=min(day, dt.day))
        except ValueError:
            continue
    return dt


def parse_plan_time(hint: str, *, base: datetime | None = None,
                    tz: str = "Asia/Shanghai",
                    default_hour: int = 9) -> ParsedTime | None:
    """计划专用：`下个月十号` / `十月` / `年底` / `两年后`。

    只在 parse_when 失败后兜底，所以不需要覆盖 parse_when 已有的能力——
    它要做的是那些"模型爱说、但事件解析器不认识"的粗粒度表达。
    """
    text = (hint or "").strip()
    if not text:
        return None
    now = _now(base, tz)

    m = _RE_PLAN_MONTH_DAY.search(text)
    if m:
        rel, day_s = m.group(1) or "这", m.group(2)
        if day_s == "初":
            day = 1
        else:
            day = cn_int(day_s)
        if day and 1 <= day <= 31:
            shift = {"上上": -2, "上": -1, "这": 0, "本": 0,
                     "下": 1, "下下": 2}.get(rel, 0)
            d = _add_months(now.replace(day=1), shift)
            try:
                due = d.replace(day=day, hour=default_hour, minute=0,
                                second=0, microsecond=0)
            except ValueError:      # 2月30日这种
                return None
            return ParsedTime(int(due.timestamp()), "day", 1, m.group(0))
        return None

    m = _RE_PLAN_YEAR_END.search(text)
    if m:
        due = now.replace(month=12, day=31, hour=default_hour,
                          minute=0, second=0, microsecond=0)
        return ParsedTime(int(due.timestamp()), "day", 0, m.group(0))

    m = _RE_PLAN_YEAR_AFTER.search(text)
    if m:
        n = cn_int(m.group(1))
        if n and 1 <= n <= 20:
            try:
                due = now.replace(year=now.year + n)
            except ValueError:      # 2/29
                due = now.replace(year=now.year + n, day=28)
            return ParsedTime(int(due.timestamp()), "day", 1, m.group(0))

    m = _RE_PLAN_NEXT_YEAR_MD.search(text)
    if m:
        mm = cn_int(m.group(1))
        if mm and 1 <= mm <= 12:
            due = now.replace(year=now.year + 1, month=mm, day=1,
                              hour=default_hour, minute=0, second=0, microsecond=0)
            return ParsedTime(int(due.timestamp()), "month", 1, m.group(0))

    m = _RE_PLAN_MD.search(text)
    if m:
        mm = cn_int(m.group(1))
        if mm and 1 <= mm <= 12:
            # "十月" 在 1 月说 = 今年 10 月；在 11 月说 = 明年 10 月。
            # 计划不能落过去，所以跨年要顺延。
            year = now.year + (1 if mm < now.month else 0)
            due = now.replace(year=year, month=mm, day=1, hour=default_hour,
                              minute=0, second=0, microsecond=0)
            return ParsedTime(int(due.timestamp()), "month", 1, m.group(0))
    return None


def parse_due(hint: str, *, base: datetime | None = None,
              tz: str = "Asia/Shanghai",
              default_hour: int = 9) -> tuple[ParsedTime | None, str | None]:
    """计划表述 → (ParsedTime, recur)。

    recur 由文本检测（"每天"→daily / "每周X"→weekly），与模型给的
    PlanOut.recur 互相独立——调用方取两者中更明确的那个（模型优先）。
    解析不出来或解析出过去时刻 → (None, recur)，recur 照常返回：
    就算时间没说清，"这是个每天的事"这个信息也不该丢。
    """
    text = (hint or "").strip()
    if not text:
        return None, None

    if _RE_RECUR_DAILY.search(text):
        recur = "daily"
    elif _RE_RECUR_WEEKLY.search(text):
        recur = "weekly"
    else:
        recur = None

    parsed = parse_when(text, base=base, tz=tz, default_hour=default_hour)

    # parse_when 能解析，但它可能只抓住了句子里的**时刻**而丢掉了日期：
    # "下个月十号早上八点" → 明天八点。判据是"解析结果离今天不到两天，
    # 而原句里明明有月/年底/年后这类远期词"——这时候宁可信计划解析器。
    if parsed is not None and _RE_PLAN_FAR.search(text):
        loose = parse_plan_time(text, base=base, tz=tz, default_hour=default_hour)
        if loose is not None and loose.ts > parsed.ts + 86400:
            parsed = loose
    if parsed is None:
        parsed = parse_plan_time(text, base=base, tz=tz, default_hour=default_hour)
    if parsed is None:
        return None, recur

    if recur is not None:
        # 周期计划的解析点如果已经过了（"每天早上7点半"在 9 点说），
        # parse_when 的"只有时刻"分支已经顺延到明天——但 day+hour 组合
        # 不会顺延（"每周三下午3点"在今天周三 16 点说会落今天 15:00）。
        # 周期计划落过去没有意义，按周期推到下一个未来点。
        now = _now(base, tz)
        step = 86400 if recur == "daily" else 7 * 86400
        while parsed.ts <= int(now.timestamp()):
            parsed = ParsedTime(parsed.ts + step, parsed.precision,
                                parsed.guessed, parsed.matched)
        return parsed, recur

    if parsed.ts <= int(_now(base, tz).timestamp()):
        # 非周期计划落过去：用户说漏了（"今天下午3点"在 16 点说），
        # 或在复述历史。两种都不能硬触发，反问。
        return None, recur
    return parsed, recur
