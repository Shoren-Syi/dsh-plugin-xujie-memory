"""M27.3 日预算熔断。

一句话：**花超了自动变笨，而不是失控。**

## 为什么要熔断，而不是"提醒一下"

记忆系统的成本和用户聊得多不多是乘出来的：每轮 600 token 的上下文，
聊 20 轮是 ¥0.6/月，聊 500 轮就是 ¥15/月。用户感觉不到"每轮多了一点"，
只会在月底看到账单。所以必须有一条硬线：到线就降级，第二天自动回来。

## 降级到底降什么（D81）

手册给的是三条：停止抽取、检索只取 top-1、pack 只用 A + C 块。

第一版我照抄了三条，结果发现**后两条在实现上互相取消**：pack 只装
A（人设）和 C（钉住）的话，D 段不存在，top-1 检索出来也没有落点。
要么保留 D 段装 1 条，要么承认"不检索"比"检索 1 条"更省。

选了后者，理由是可测的：D 段 600 token 是**每轮最大的一笔**，
砍掉它省 55%；只砍到 1 条（80 token）只省 47%，却还要付检索的
SQL 时间。降级的目的是活着等到明天，不是"记得少一点"。

但 A + C 也不完整——时间锚必须留。没有它，降级期间 AI 不知道今天是几号，
"明天几点提醒你"这类话会直接说错，而这不是"变笨"，是"说错"。
时间锚只有 20 token，留它不影响任何成本结论。

所以 `DEGRADED_BLOCKS = (persona, pinned, anchor)`，比手册多一个锚。
这条差异写进 R-27.3，不藏着。

## 为什么计数要从库里读，而不是内存计数器

内存计数器快，但重启即清零——用户重启一下服务就能无限刷额度。
`metrics` 和 `extract_runs` 已经是持久账本，按天求和即可，两条查询
走索引都是亚毫秒。**预算是钱，钱的账必须落盘。**

## 为什么抽取也要算进来

`metrics` 只记对话请求，抽取是后台任务，token 只落在 `extract_runs`。
只读 metrics 的话，重度用户每天 20 次抽取的成本完全看不见。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..log import get_logger

log = get_logger("budget")

__all__ = [
    "BudgetState", "check", "reset", "day_bounds", "format_status",
    "DEGRADED_BLOCKS", "SYS", "RESET_KEY",
]

SYS = "_sys"
RESET_KEY = "budget:reset_at"

# 降级时保留的段。顺序与 pack.BLOCKS 一致，方便对照。
DEGRADED_BLOCKS = ("persona", "pinned", "anchor")


def day_bounds(now: float | None = None,
               tz: str = "Asia/Shanghai") -> tuple[int, int]:
    """本地时区的当日 [00:00, 次日 00:00)，返回 epoch 秒。

    用本地午夜而不是"最近 24 小时"：用户理解的"每天额度"是日历天，
    而且次日 00:00 自动恢复靠的就是这条边界——滚动窗口永远恢复不了。
    """
    t = time.time() if now is None else float(now)
    d = datetime.fromtimestamp(t, ZoneInfo(tz or "Asia/Shanghai"))
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


@dataclass(frozen=True)
class BudgetState:
    day: str                  # 2026-11-06
    limit: int                # 0 = 不限
    chat_tokens: int
    extract_tokens: int
    reset_at: int             # 0 = 没手动重置过
    on_exceed: str            # degrade / off
    start: int = 0
    end: int = 0

    @property
    def used(self) -> int:
        return self.chat_tokens + self.extract_tokens

    @property
    def over(self) -> bool:
        # 0 是"关掉熔断"而不是"零额度"。把 0 当零额度的话，任何没配
        # budget 段的旧配置文件一升级就全体降级——这是最坏的一种默认值。
        return self.limit > 0 and self.used >= self.limit

    @property
    def degraded(self) -> bool:
        return self.over and self.on_exceed != "off"

    @property
    def pct(self) -> float:
        return self.used / self.limit * 100 if self.limit > 0 else 0.0

    @property
    def left(self) -> int:
        return max(0, self.limit - self.used) if self.limit > 0 else 0


def _cfg_of(store: Any, cfg: Any) -> Any:
    return cfg if cfg is not None else getattr(store, "cfg", None)


def _reset_at(store: Any) -> int:
    try:
        return int(store.get_state(SYS, RESET_KEY, "0") or 0)
    except (TypeError, ValueError):
        return 0


def check(store: Any, cfg: Any = None, now: float | None = None) -> BudgetState:
    """算当前预算状态。**只读，不写库**，可以每轮调。"""
    cfg = _cfg_of(store, cfg)
    b = getattr(cfg, "budget", None)
    limit = int(getattr(b, "daily_tokens", 500_000))
    mode = str(getattr(b, "on_exceed", "degrade") or "degrade")
    tz = getattr(cfg, "timezone", "Asia/Shanghai") or "Asia/Shanghai"
    start, end = day_bounds(now, tz)

    reset_at = _reset_at(store)
    since = max(start, reset_at)      # 手动重置后，之前的用量不再计入
    chat = store.token_sum(since, end)
    ext = store.extract_token_sum(since, end)
    day = datetime.fromtimestamp(start, ZoneInfo(tz)).date().isoformat()
    return BudgetState(
        day=day, limit=limit,
        chat_tokens=chat["prompt_tokens"] + chat["completion_tokens"],
        extract_tokens=ext["prompt_tokens"] + ext["completion_tokens"],
        reset_at=reset_at, on_exceed=mode, start=start, end=end,
    )


def reset(store: Any, now: float | None = None) -> int:
    """手动恢复：把"从此刻起重新计数"写进状态。

    不删 metrics 行——账是账，预算额度是额度。删行会让"这个月花了多少"
    永久失真，而重置只需要一个时间戳。用**当天 00:00 之后**的一个点
    作为新起点，跨天后这个点自动失效（since = max(day_start, reset_at)）。
    """
    ts = int(time.time() if now is None else now)
    store.set_state(SYS, RESET_KEY, str(ts))
    log.info("budget.reset", extra={"fields": {"at": ts}})
    return ts


def format_status(st: BudgetState) -> str:
    """`hippo budget` 与 `hippo stats` 共用的一段文字。"""
    lines: list[str] = []
    head = f"日预算  {st.day}"
    if st.limit > 0:
        head += f"   {st.used:,} / {st.limit:,} token（{st.pct:.1f}%）"
    else:
        head += f"   已用 {st.used:,} token（未设上限）"
    lines.append(head)
    lines.append(f"  对话 {st.chat_tokens:,}  抽取 {st.extract_tokens:,}"
                 + (f"  剩余 {st.left:,}" if st.limit > 0 else ""))
    if st.degraded:
        lines.append("  状态 **降级中**：停止抽取、只用 A/C/锚装配"
                     "（回复照常，记忆暂停）")
        lines.append("  恢复 次日 00:00 自动 / `hippo budget reset` 立即")
    elif st.over:
        lines.append("  状态 已超限，但 on_exceed=off（不降级，仅告警）")
    else:
        lines.append("  状态 正常")
    if st.reset_at and st.reset_at >= st.start:
        lines.append(f"  今日已手动重置 "
                     f"{datetime.fromtimestamp(st.reset_at):%H:%M:%S}")
    return "\n".join(lines)
