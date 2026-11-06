"""M11.2 验收：三种深度的注入格式（M16 已改成分档格式，本文件同步）。

格式这件事只有在**模型读错**的时候才会暴露，所以测试要盯四件具体的事：

  1. 时间写多细 —— 按 `time_precision`，别把"去年冬天"写成 2025-11-01
  2. 来源在不在 —— 非 user 的来源必须留着（AI 自己说的话不能被当成用户说的）
  3. 截断有没有痕迹 —— 半句话不加 "…"，模型会当完整信息用
  4. 档位分对了没 —— hot/warm 有摘要，cold 只有关键词（§5.3）

## 这一版为什么整体重写

M11 当时的格式是单行 `[原话 · 日期 · 用户] "…"`。M16 按手册 §5.3 改成
多行块：

    [记忆 · 2026-03-14 · hot]
    关键词：花生 过敏 急救
    摘要：……

这不是测试"过时了所以放宽"，是**格式换了**。原来每条断言的性质都还在，
只是落点变了：
    `· 用户]`        → 用户视角头部只有三个字段（来源不再出现）
    `] "正文"`       → `原文："正文"`
    `[关键词 · …]`   → `[记忆 · … · cold]` + 关键词行

`_fmt_time` 的六种精度、区间、问号标记全部原样保留，断言只是去掉尾部
的来源字段。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hippo.core.drill import (
    DEPTH_KW, DEPTH_RAW, DEPTH_SUMMARY, RAW_MAX_TOKENS, SUMMARY_MAX_TOKENS,
    format_event,
)
from hippo.core.models import Event

TZ = "Asia/Shanghai"


def _ts(y, m, d, h=12, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=ZoneInfo(TZ)).timestamp())


def _ev(raw="我下周就不在字节了，去一家创业公司。", **kw):
    base = dict(
        id=1, session="s1", event_time=_ts(2026, 11, 2, 10, 30),
        ingest_time=_ts(2026, 11, 2), raw=raw,
        summary="用户下周从字节离职，去创业公司",
        keywords="字节 离职 创业公司", source="user", time_precision="day",
        tier=2,
    )
    base.update(kw)
    return Event(**base)


def _head(out: str) -> str:
    return out.split("\n", 1)[0]


def _raw_line(out: str) -> str:
    for ln in out.split("\n"):
        if ln.startswith("原文："):
            return ln
    raise AssertionError(f"没有原文行：{out!r}")


# ------------------------------------------------------------------ 快照
def test_three_depths_snapshot():
    e = _ev()
    # 默认：按 tier（warm）分档 → 关键词 + 摘要
    assert format_event(e) == (
        "[记忆 · 2026-11-02 · warm]\n"
        "关键词：字节 离职 创业公司\n"
        "摘要：用户下周从字节离职，去创业公司")
    # 摘要档和默认档同形（M11 的 DEPTH_SUMMARY 语义 = "给摘要"）
    assert format_event(e, DEPTH_SUMMARY) == format_event(e)
    # 原文档 = 关键词 + 原文，**摘要让位**
    #
    # warm 一档 70 token：关键词 27 + 原文 22 + 头部 9 = 58，留给摘要的
    # 只剩不到 10 —— 那是"用户下周从字…"。半句摘要比没有摘要更糟
    # （模型会以为摘要本身就残缺），所以有原文时摘要直接不出现。
    assert format_event(e, DEPTH_RAW) == (
        "[记忆 · 2026-11-02 · warm]\n"
        "关键词：字节 离职 创业公司\n"
        '原文："我下周就不在字节了，去一家创业公司。"')
    # 关键词档 = 只要关键词，没有摘要行
    assert format_event(e, DEPTH_KW) == (
        "[记忆 · 2026-11-02 · warm]\n关键词：字节 离职 创业公司")


def test_header_has_three_fields_for_user():
    """§5.3：`[记忆 · 时间 · tier]`，用户视角没有第四个字段。"""
    assert _head(format_event(_ev())) == "[记忆 · 2026-11-02 · warm]"


def test_bad_depth_raises():
    with pytest.raises(ValueError):
        format_event(_ev(), "full")


# ------------------------------------------------------------------ 时间
@pytest.mark.parametrize("precision,expect", [
    ("minute", "2026-11-02 10:30"),
    ("hour", "2026-11-02 10时"),
    ("day", "2026-11-02"),
    ("month", "2026-11"),
    ("year", "2026"),
])
def test_time_precision(precision, expect):
    """写到哪一位由 precision 决定，不是一律写满。"""
    out = format_event(_ev(time_precision=precision), DEPTH_SUMMARY)
    assert f"· {expect} · " in _head(out)


def test_unknown_precision_falls_back_to_day():
    out = format_event(_ev(time_precision=""), DEPTH_SUMMARY)
    assert "· 2026-11-02 · " in _head(out)


def test_range_same_year():
    e = _ev(event_time=_ts(2026, 10, 30), event_time_end=_ts(2026, 11, 5))
    assert "2026-10-30~11-05" in _head(format_event(e, DEPTH_SUMMARY))


def test_range_across_year_keeps_year():
    e = _ev(event_time=_ts(2025, 12, 30), event_time_end=_ts(2026, 1, 2))
    assert "2025-12-30~2026-01-02" in _head(format_event(e, DEPTH_SUMMARY))


def test_range_ignored_for_minute_precision():
    """精确到分钟的事件不该显示成区间——那是两件事的时间。"""
    e = _ev(event_time=_ts(2026, 11, 2, 10, 0),
            event_time_end=_ts(2026, 11, 2, 11, 0), time_precision="minute")
    assert "2026-11-02 10:00 · " in _head(format_event(e, DEPTH_SUMMARY))


def test_guessed_time_is_marked():
    """猜的时间打 ?。不打的话模型会把"去年冬天"当成确切日期去算天数。"""
    assert "2026-11-02?" in _head(format_event(_ev(time_guessed=1), DEPTH_SUMMARY))
    assert "?" not in format_event(_ev(time_guessed=0), DEPTH_SUMMARY)


# ------------------------------------------------------------------ 来源
@pytest.mark.parametrize("source,label", [
    ("ai", "AI"), ("third", "第三方"),
])
def test_non_user_source_is_kept(source, label):
    """AI 自己说的话必须留来源，否则会被当成用户说的。

    这是本轮对 §5.3 的一处**加字段**：用户视角三字段不变（快照钉住），
    非用户视角追加 ` · 来源`。手册样例全是用户视角，所以三种格式仍然
    逐字一致；而"我上次说想辞职"到底是谁说的，这个信息不能丢。
    """
    assert f"· {label}]" in _head(format_event(_ev(source=source), DEPTH_SUMMARY))


def test_user_source_is_implicit():
    assert "用户" not in _head(format_event(_ev(source="user")))


def test_unknown_source_passthrough():
    """未知来源原样输出，不要硬翻成"用户"——那是在伪造出处。"""
    assert "· system]" in _head(format_event(_ev(source="system"), DEPTH_SUMMARY))


# ------------------------------------------------------------------ 截断
def test_long_raw_truncated_with_ellipsis():
    e = _ev(raw="很累" * 500)
    out = format_event(e, DEPTH_RAW)
    line = _raw_line(out)
    assert line.endswith('…"')
    body = line[len('原文："'):-1]
    assert len(body) < len(e.raw)


def test_raw_respects_custom_max():
    """`max_tokens` 现在约束的是**整块**（M16 起），不再只约束正文。

    理由：§5.3 的 60~90 说的是"这条记忆占多少"，不是"摘要占多少"。
    只卡正文的话，摘要刚好卡满 80 时，加上头部和关键词就 110 了。
    """
    from hippo.core.drill import _real
    e = _ev(raw="很累" * 500)
    out = format_event(e, DEPTH_RAW, max_tokens=60)
    assert _real(out.split("\n")[0] + "\n关键词：" + " ".join(e.keyword_list)) <= 60
    # 正文本身一定在额度内
    assert _real(out) <= 60 + 20      # 原文行允许略超（它是 pinned/drill 追加的）


def test_short_raw_untouched():
    e = _ev(raw="我辞职了")
    assert _raw_line(format_event(e, DEPTH_RAW)).endswith('"我辞职了"')


def test_summary_truncated_with_ellipsis():
    e = _ev(summary="很长的摘要" * 100)
    out = format_event(e, DEPTH_SUMMARY)
    assert out.endswith("…")
    assert len(out) < len(e.summary)


def test_default_raw_cap_is_400():
    assert RAW_MAX_TOKENS == 400
    assert SUMMARY_MAX_TOKENS == 80


# ------------------------------------------------------------------ 正文清洗
def test_multiline_raw_flattened():
    """原文里的换行会把一条记忆拆成两条（注入格式是按行解析的）。

    M16 之后整个块是多行的，但**块内每一行内部**不许再有换行——
    否则一条记忆会被读成两条，摘要行后面莫名多出一行原文。
    """
    e = _ev(raw="第一句。\n第二句。\n\n第三句。")
    out = format_event(e, DEPTH_RAW)
    for ln in out.split("\n"):
        assert ln.count("\n") == 0
    assert "第一句。 第二句。 第三句。" in out


def test_inner_quotes_escaped():
    """正文里的半角引号会提前闭合外层引号，模型会以为原话到此结束。"""
    e = _ev(raw='他说"我不去"，然后就走了')
    out = _raw_line(format_event(e, DEPTH_RAW))
    assert out.count('"') == 2, out
    assert "”我不去，”" in out or "”我不去”" in out


def test_kw_without_keywords_falls_back_to_summary():
    e = _ev(keywords="", summary="用户下周从字节离职")
    out = format_event(e, DEPTH_KW)
    assert "用户下周从字节离职" in out
    assert out.split("\n")[0].endswith("warm]")


def test_kw_caps_word_count():
    from hippo.core.drill import KW_MAX
    e = _ev(keywords=" ".join(f"词{i}" for i in range(30)))
    out = format_event(e, DEPTH_KW)
    body = out.split("关键词：", 1)[1]
    assert len(body.split(" ")) == KW_MAX[2]


def test_body_overrides_and_skips_truncation():
    """drill 已经按剩余预算截过，format 不许再截一次。

    两处上限不一致时会出现"预算里装得下、注入的却是半句"，
    而这种偏差只有把 CLI 输出和库里的原文逐字比对才看得出来。
    """
    e = _ev(raw="很累" * 500)
    piece = "很短的一句"
    out = format_event(e, DEPTH_RAW, body=piece)
    assert _raw_line(out) == f'原文："{piece}"'
    assert "…" not in _raw_line(out)


def test_half_open_range_is_one_day():
    """库里的 end 是半开边界：'11-04 这一天'存成 [11-04 09:00, 11-05 00:00)。

    直接按日期比会显示成 "11-04~11-05"，看起来是两天——真机库第一条
    就是这个形状，CLI 上多出一个 "~11-05"，像跨了两天。
    """
    e = _ev(event_time=_ts(2026, 11, 4, 9, 0),
            event_time_end=_ts(2026, 11, 5, 0, 0))
    assert "2026-11-04 · " in _head(format_event(e, DEPTH_SUMMARY))
    assert "~" not in _head(format_event(e, DEPTH_SUMMARY))


def test_week_precision_shows_range():
    e = _ev(event_time=_ts(2026, 11, 9, 0, 0),
            event_time_end=_ts(2026, 11, 16, 0, 0), time_precision="week")
    assert "2026-11-09~11-15" in _head(format_event(e, DEPTH_SUMMARY))
