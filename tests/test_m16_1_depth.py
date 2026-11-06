"""M16.1 验收：按 tier 分档的注入格式。

DoD 是「hot/warm/cold 三种注入格式与第一部分 §5.3 完全一致」。所以这个文件
的第一组测试**逐字**比对手册里那三个样例——不是"看起来像"，是字节相同。

## 为什么要专门盯"两处渲染"

M15 收工时，同一条记忆在两个地方各有一套拼装逻辑：

    pack._memory_block   →  [记忆 · 日期 · hot] + 关键词：… + 摘要：…
    drill.format_event   →  [原话 · 日期 · 用户] "…"

两套并存的症状是**CLI 预览看着对、真进 prompt 的不对**，而且只有把两边
逐字比对才看得出来。M16 把定义收进 `format_event` 一处，本文件最后一组
测试就是钉这个：pack 拼出来的 D 段必须逐字等于 `format_event` 的输出。

## token 区间怎么量

`estimate_tokens` 自带 1.3 安全系数（宁可高估，见 turns.py）。手册 §5.3 的
60~90 说的是**模型看到的 token**，所以这里用 `_real()`（估算值 ÷ 1.3）去比。
不换算的话 hot 档会只用掉 2/3 的额度，看起来"达标"其实一直欠着。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hippo.core.drill import (
    KW_MAX, TIER_BUDGET, _real, format_event,
)
from hippo.core.models import Event
from hippo.core.pack import _memory_block
from hippo.core.rank import MemoryItem

TZ = "Asia/Shanghai"


def _ts(y, m, d, h=12):
    return int(datetime(y, m, d, h, tzinfo=ZoneInfo(TZ)).timestamp())


def _ev(*, tier=2, pins=0, keywords="花生 过敏 急救",
        summary="用户对花生严重过敏，曾过敏性休克送医，随身带肾上腺素笔。",
        raw="我对花生过敏，上次休克送医院", **kw):
    base = dict(
        id=1, session="s1", event_time=_ts(2026, 3, 14),
        ingest_time=_ts(2026, 3, 14), raw=raw, summary=summary,
        keywords=keywords, source="user", time_precision="day",
        tier=tier, pinned=pins,
    )
    base.update(kw)
    return Event(**base)


# ------------------------------------------------------- DoD：与 §5.3 逐字一致
def test_hot_matches_manual_verbatim():
    """手册 §5.3 第一个样例，逐字比对。"""
    out = format_event(_ev(tier=1))
    assert out == (
        "[记忆 · 2026-03-14 · hot]\n"
        "关键词：花生 过敏 急救\n"
        "摘要：用户对花生严重过敏，曾过敏性休克送医，随身带肾上腺素笔。")


def test_warm_matches_manual_verbatim():
    out = format_event(_ev(
        tier=2, keywords="老王 跳槽 字节",
        summary="用户的朋友老王从字节跳到一家创业公司，用户当时很羡慕。",
        raw="老王从字节跳了", event_time=_ts(2025, 11, 2)))
    assert out == (
        "[记忆 · 2025-11-02 · warm]\n"
        "关键词：老王 跳槽 字节\n"
        "摘要：用户的朋友老王从字节跳到一家创业公司，用户当时很羡慕。")


def test_cold_matches_manual_verbatim():
    """手册写的是 `2024-06-xx`——`xx` 是"不知道哪天"的占位符。

    我们据实写成 `2024-06`（`time_precision=month`），不编造一个具体日子。
    这是唯一一处与手册字面不同，差的就是那个占位符本身。
    """
    out = format_event(_ev(
        tier=3, keywords="搬家 杭州", summary="用户从北京搬到杭州。",
        event_time=_ts(2024, 6, 1), time_precision="month"))
    assert out == "[记忆 · 2024-06 · cold]\n关键词：搬家 杭州"


# ------------------------------------------------------- 三条验收项
def test_hot_pinned_carries_quoted_raw():
    """验收 #2：hot + pinned 带引号原文。"""
    out = format_event(_ev(tier=1, pins=1))
    assert '原文："' in out
    assert out.count('"') == 2
    assert out.splitlines()[-1].startswith('原文："')


def test_cold_has_no_summary_line():
    """验收 #3：cold 无摘要。"""
    out = format_event(_ev(tier=3))
    assert "摘要" not in out
    assert out.count("\n") == 1


@pytest.mark.parametrize("tier", [1, 2, 3])
def test_timestamp_always_present(tier):
    """验收 #4：时间戳 100% 存在。"""
    out = format_event(_ev(tier=tier))
    assert "2026-03-14" in out.splitlines()[0]


# ------------------------------------------------------- token 区间
@pytest.mark.parametrize("tier,name", [(1, "hot"), (2, "warm"), (3, "cold")])
def test_token_band(tier, name):
    """§5.3 的 60~90 / 50~70 / 15~25（真实 token，见模块开头）。"""
    lo, hi = TIER_BUDGET[tier]
    used = _real(format_event(_ev(tier=tier)))
    assert lo <= used <= hi, f"{name} 用了 {used:.0f} token，不在 {lo}~{hi}"


def test_pinned_may_exceed_hot_band():
    """pinned 多一行原文，允许超 90（手册："pinned 再加原文"），但不超过 130。

    上限为什么是 90 + 40：多出来的只有那一行引号原文，而原文本身封顶
    `RAW_QUOTE_TOKENS=40`。pinned 再多也不能把一条记忆撑成小作文——
    数量上的失控由 M16.2 的 C 段 200 token 提醒兜住。
    """
    short = _real(format_event(_ev(tier=1, pins=1, raw="我对花生过敏")))
    long = _real(format_event(_ev(tier=1, pins=1, raw="我对花生过敏，" * 40)))
    assert short <= TIER_BUDGET[1][1] + 40
    assert long <= TIER_BUDGET[1][1] + 40
    # 长原文时确实用上了这 40 的额度（否则这条测试什么都没钉住）
    assert long > TIER_BUDGET[1][1]


def test_cold_keywords_capped_tighter_than_hot():
    """档位越冷，关键词给得越少。否则 cold 一条 8 个词就顶穿 25 token。"""
    assert KW_MAX[3] < KW_MAX[2] < KW_MAX[1]


# ------------------------------------------------------- 边角
def test_unknown_tier_falls_back_to_warm():
    out = format_event(_ev(tier=9))
    assert out.splitlines()[0].endswith("warm]")


def test_tier_override():
    """`tier=` 参数可以覆盖事件自己的 tier（给仿真和调试用）。"""
    assert format_event(_ev(tier=1), tier=3).splitlines()[0].endswith("cold]")


def test_cold_drilled_gives_raw_not_summary():
    """冷事件被下钻时给原文、不给摘要。

    "当时怎么说的"问的是原文；给一条现裁的20字摘要，等于答非所问，
    而且比原文贵（摘要要现裁，原文是现成的）。
    """
    out = format_event(_ev(tier=3), body="我对花生过敏")
    assert "摘要" not in out
    assert '原文："我对花生过敏"' in out


# ------------------------------------------------------- 单一渲染出口
def test_pack_block_equals_format_event():
    """D 段必须逐字等于 `format_event` 的输出。

    这是本轮最重要的一条：M15 时 pack 和 drill 各拼一套，改一处忘一处。
    现在 pack 只负责"把 items 排好、加个标题"，每条长什么样由 format_event 定。
    """
    ev = _ev(tier=1)
    it = MemoryItem(event=ev, score=1.0, kw_hit=2, text=ev.summary, tokens=50)
    block = _memory_block([it], None)
    assert block == "# 我记得的\n" + format_event(ev, summary=ev.summary)


def test_pack_cold_item_is_one_line():
    ev = _ev(tier=3, keywords="搬家 杭州")
    it = MemoryItem(event=ev, score=1.0, kw_hit=1, text=ev.summary, tokens=20)
    block = _memory_block([it], None)
    assert block.count("\n") == 2          # 标题 + 一条两行（头部 + 关键词）


def test_pack_uses_l2_clipped_summary():
    """L2 已经按预算裁过摘要，D 段要沿用裁剪版，不能退回库里全文。

    退回全文的后果：L2 的 `memory_tokens` 算的是裁剪后的总量，
    D 段突然变长，预算直接爆——而爆的那部分没人记账。
    """
    ev = _ev(tier=2, summary="用户对花生严重过敏，曾过敏性休克送医，随身带肾上腺素笔。")
    clipped = "用户对花生严重过敏"
    it = MemoryItem(event=ev, score=1.0, kw_hit=1, text=clipped, tokens=10)
    assert clipped in _memory_block([it], None)
