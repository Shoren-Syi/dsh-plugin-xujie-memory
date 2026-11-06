"""M19.2 调度器：到点触发，误差 ≤60s，且不靠 LLM 判断时间。

设计立场：
- **自写 asyncio 循环，不引 APScheduler**（约束⑤少依赖）。调度器的
  全部工作是"每 60 秒跑一次 SQL + 几行 UPDATE"，为一个 if 引一个
  调度框架，依赖账算不过来。
- **tick 是唯一触发端**。pack 的 [计划] 段只负责渲染（注入≠触发），
  mark_plan_fired 的 `UPDATE ... WHERE fired=0` 保证并发下只触发
  一次——谁先抢到 rowcount=1 谁触发。
- **不调模型**。手册说"到点调一次小模型生成问候语"，但我们的架构
  是 pack 装配上下文：到点事件写进注入段，模型响应用户消息时自然
  带出。零 token 的 tick 比"小模型生成问候"便宜两个数量级，
  而且不会在用户没开口时凭空弹消息（打扰控制交给 M24 心跳）。

时间语义（容易踩的坑，注释钉死）：
- due_at 是**写入时一次换算好的绝对 epoch**（M19.1.3），tick 只做
  比较，永不重算——否则"明天8点"会在每天 8 点重新变成"明天8点"，
  永远差一天。
- sweep_missed 在启动时跑一次，之后每轮 tick 前也跑（进程死过
  重启的场景由它兜底）：due_at 超过 grace 还没触发的标 missed=1，
  **但不标 fired**——晚到的提醒还是要发，注入时说明"这条晚了"。
"""

from __future__ import annotations

import asyncio
import time as _time
from typing import Any, Callable

from ..log import get_logger

log = get_logger("scheduler")

DEFAULT_TICK_SECONDS = 60
DEFAULT_GRACE_SECONDS = 300          # 到点 5 分钟内触发算正常延迟


class Scheduler:
    """到点触发的最小调度器。触发即 mark fired，回调由宿主决定。"""

    def __init__(self, store: Any, *,
                 tick_seconds: int = DEFAULT_TICK_SECONDS,
                 grace_seconds: int = DEFAULT_GRACE_SECONDS,
                 on_fire: Callable[[Any], Any] | None = None):
        self.store = store
        self.tick_seconds = tick_seconds
        self.grace_seconds = grace_seconds
        # on_fire(plan) 在触发成功后被调用（同步或异步均可）。
        # 默认只写日志——"收到主动消息"的最后一步是宿主的事。
        self.on_fire = on_fire

    # ------------------------------------------------------------ 核心
    def tick(self, *, now: int | None = None) -> list[Any]:
        """跑一轮：到点的全部触发。返回这次真正触发的计划。

        只做一次 SQL 查询（手册 M19.2.2）。到点的逐条
        mark_plan_fired——`WHERE fired=0` 是并发安全的全部秘密：
        两个 tick 同时跑，只有一个能拿到 rowcount=1。
        """
        now_ts = now if now is not None else int(_time.time())
        fired: list[Any] = []
        for p in self.store.due_plans(now_ts):
            # sweep 可能刚把超期的标了 missed=1；触发时必须原样保留，
            # 不能用 mark_plan_fired 的默认 missed=False 把它洗掉。
            if not self.store.mark_plan_fired(p.id, missed=bool(p.missed),
                                              now=now_ts):
                continue                      # 别的 tick 抢先了，让给它
            fired.append(p)
            if p.recur:
                step = 86400 if p.recur == "daily" else 7 * 86400
                self.store.advance_plan(p.id, p.due_at + step)
            log.info("plan.fired", extra={"fields": {
                "id": p.id, "due": p.due_at, "now": now_ts,
                "late": now_ts - p.due_at, "missed": bool(p.missed)}})
            if self.on_fire is not None:
                r = self.on_fire(p)
                if asyncio.iscoroutine(r):
                    # tick 本身是同步的；异步回调丢给事件循环时，
                    # 调用方要自己保证 loop 在跑（run_forever 里已保证）。
                    asyncio.get_event_loop().create_task(r)
        return fired

    def sweep(self, *, now: int | None = None) -> int:
        """扫超期未触发的，标 missed。启动时和每轮 tick 前各跑一次。"""
        n = self.store.sweep_missed_plans(grace_seconds=self.grace_seconds,
                                          now=now)
        if n:
            log.info("plan.sweep_missed", extra={"fields": {"n": n}})
        return n

    # ------------------------------------------------------------ 循环
    async def run_forever(self, *, stop: asyncio.Event | None = None,
                          first_delay: float = 0.0) -> None:
        """asyncio 主循环：sweep → tick → 睡到下一轮。

        `stop` 置位后退出。first_delay 给测试用（0 = 立即跑第一轮）。
        循环里所有异常都吞掉记日志：调度器死掉的表现是"提醒永远
        不响"——比崩溃难发现得多，所以宁可吵也不静默退出。
        """
        if stop is None:
            stop = asyncio.Event()
        if first_delay:
            await asyncio.sleep(first_delay)
        while not stop.is_set():
            try:
                self.sweep()
                self.tick()
            except Exception as exc:          # 见 docstring：宁可吵不静默
                log.warning("scheduler.tick_failed", extra={"fields": {
                    "err": str(exc)}})
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.tick_seconds)
            except asyncio.TimeoutError:
                pass
