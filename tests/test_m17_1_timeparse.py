"""M17.1 验收：人生阶段 / 事件锚 —— 给区间，但不硬猜。

这一节的 DoD 是"60 条覆盖 ≥95%"，但覆盖率高不代表对：把夹具期望值
照着实现抄一遍也能 100%（M8.3 的文档里就写过这个陷阱）。

所以这里钉的是**能独立推出答案的性质**：

  - 30 岁的人说"小时候"，区间必须落在 3~12 岁那几年（不看实现也该这样）
  - "疫情那会儿"的起点必须是 2020 年（这是世界事实，不是我们的选择）
  - 没有年龄锚时区间**必须更宽**，否则"假设年龄"这个机制根本没生效
  - fuzzy 只能标给真的说不准的，不能拿它当"什么都解析不了"的垃圾桶
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hippo.core.timeparse import LIFE_STAGE_AGES, parse_time

TZ = "Asia/Shanghai"
BASE = datetime(2026, 11, 5, 10, 0, tzinfo=ZoneInfo(TZ))   # 2026-11-05


def sp(hint: str, **kw):
    return parse_time(hint, ref=BASE, tz=TZ, **kw)


def year(ts: int) -> int:
    return datetime.fromtimestamp(ts, ZoneInfo(TZ)).year


def width_years(span) -> int:
    return year(span.end) - year(span.start)


# ---------------------------------------------------------------- 人生阶段
@pytest.mark.parametrize("hint,key", [
    ("小时候", "小时候"), ("童年", "童年"), ("小学的时候", "小学"),
    ("初中那会儿", "初中"), ("高中时住校", "高中"),
    ("大学时", "大学"), ("刚毕业那会儿", "刚毕业"),
])
def test_life_stage_lands_in_the_right_age_band(hint, key):
    """区间必须覆盖"那个年龄段"，而不是覆盖"随便一段时间"。

    这条断言不引用任何实现常量以外的期望数值：给定年龄 30（出生 1996），
    `LIFE_STAGE_AGES[key]` 就是唯一的答案。如果谁把词表改了，
    这里会立刻红——这正是我们要的。
    """
    s = sp(hint, age=30)
    assert s is not None
    lo, hi = LIFE_STAGE_AGES[key]
    assert year(s.start) - 1996 == lo
    assert year(s.end) - 1996 == hi


def test_life_stage_uses_age_not_assumption():
    """给了年龄就用年龄。28 岁的人"小时候"比 30 岁的**晚**两年。

    方向容易记反：年龄越小，出生的年份越靠后，同一人生阶段发生得越晚。
    """
    a = sp("小时候", age=30)
    b = sp("小时候", age=28)
    assert year(b.start) > year(a.start)
    assert year(b.end) > year(a.end)


def test_birth_year_wins_over_age():
    s = sp("大学时", age=20, birth_year=1990)
    assert year(s.start) - 1990 == LIFE_STAGE_AGES["大学"][0]


def test_no_anchor_widens_instead_of_guessing_point():
    """没有年龄锚时必须**放宽**，而且必须标 guessed。

    这是 M17.1 最容易做错的地方：拿一个假设年龄（比如 30）算完就当真的用，
    `guessed=0` 会让下游以为"这个区间有依据"。实际它盖的是 22~40 岁的人。
    """
    tight = sp("小时候", age=30)
    loose = sp("小时候")
    assert width_years(loose) > width_years(tight)
    assert loose.guessed == 1
    assert tight.guessed == 0


def test_life_stage_never_ends_in_future():
    """"工作后"的终点是**现在**，不是 35 岁那年。

    不截断的话，30 岁用户会拿到 [2018, 2035]——一个延伸到未来的区间。
    搜索"最近半年发生的事"时，这类区间会被当成未来事件排除。
    """
    s = sp("工作后", age=30)
    assert s.end <= int(BASE.timestamp())
    assert year(s.end) == BASE.year


def test_life_stage_precision_is_fuzzy():
    """人生阶段的精度是 fuzzy，不是 year。

    给成 year 会诱导下游按年份精确比较；而"小时候"本身横跨十年。
    """
    for hint in ("小时候", "大学时", "刚毕业"):
        assert sp(hint, age=30).precision == "fuzzy"


# ---------------------------------------------------------------- 事件锚
@pytest.mark.parametrize("hint,start_year", [
    ("疫情那会儿", 2020), ("新冠那年", 2020),
    ("非典那年", 2003), ("汶川地震那年", 2008),
    ("奥运会那年", 2008), ("金融危机那阵子", 2008),
])
def test_event_anchor_uses_world_facts(hint, start_year):
    """锚的年份是**世界事实**，与用户是谁无关。

    所以断言里直接写年份字面量——如果有人把"非典"改成 2004，
    这条测试会红，而不是跟着实现一起漂。
    """
    s = sp(hint)
    assert s is not None
    assert year(s.start) == start_year


def test_event_anchor_is_not_guessed():
    """区间宽是这件事的性质，不是猜测。标 guessed=1 会让下游把它降级。"""
    assert sp("疫情那会儿").guessed == 0


def test_unreferenced_anchor_is_not_guessed_at_all():
    """"地震那年"不收录。

    在四川人嘴里是 2008，在唐山人嘴里是 1976。两个都"合理"，所以都错。
    这条钉住的是**不做什么**：宁可返回 None，让摘要里的自然语言去表达。
    """
    assert sp("地震那年") is None
    assert sp("那年夏天") is None


# ---------------------------------------------------------------- 交集
def test_life_stage_and_anchor_intersect():
    """"疫情期间我还在上高中"：两个锚同时出现，取交集。

    24 岁的人高中是 2017~2021，疫情是 2020~2023，交集 2020~2021。
    只取其一都会丢掉用户真正想说的信息：
      - 只取高中 → 2017~2021，把疫情前那三年也算进去了
      - 只取疫情 → 2020~2023，把毕业后的两年也算进去了
    """
    s = sp("疫情期间我还在上高中", age=24)
    assert s is not None
    assert year(s.start) == 2020
    assert year(s.end) == 2021


def test_anchor_wins_when_intersection_empty():
    """交集为空时以事件锚为准。

    30 岁的人 2014~2019 上大学，疫情 2020~2023，两者不相交——
    用户说错了，或者我们对年龄的推断错了。此时给一个空区间毫无意义，
    给事件锚至少是"他提到了疫情"这个确定信息。
    """
    s = sp("疫情期间我在上大学", age=30)
    assert year(s.start) == 2020


# ---------------------------------------------------------------- 回归
def test_general_parsing_still_works_through_unified_entry():
    """统一入口不能把 M8.3 已有的能力弄丢。

    这一条防的是"加了新分支，旧分支被提前 return 挡掉"——
    人生阶段的正则里如果有裸的"时"，"明天下午3点"就会被它吃掉。
    """
    assert sp("昨天").precision == "day"
    assert sp("下周三").precision == "day"
    assert sp("去年冬天").precision == "season"
    assert sp("三小时前").precision == "hour"
    assert sp("明天下午3点").precision == "hour"


def test_two_and_half_months_before():
    """"两个半月前"不是"半个月前"。

    旧的 `_RE_BEFORE` 会在"半月前"处命中，把"两个"当没看见，
    2.5 个月被算成 0.5 个月——差 60 天，且不报错。
    """
    s = sp("两个半月前")
    assert s is not None
    days = (BASE - datetime.fromtimestamp(s.start, ZoneInfo(TZ))).days
    assert 70 <= days <= 80
