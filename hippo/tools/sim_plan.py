"""M19.2 调度器模拟：计划到点 → 触发，误差多少？

    python -m hippo.tools.sim_plan --delay 90

真实节奏模拟：设一条 due_at=now+delay 的计划，调度器每 15 秒 tick
一次（模拟脚本里压缩了真实 60s 周期），记录 mark_plan_fired 成功
的时刻。输出对齐手册示例：

    planned 00:31:00 fired 00:31:18 delay=18s

delay 恒 < tick_seconds 且 > 0：触发永远发生在计划之后的第一轮
tick，误差上界就是 tick 周期（生产 60s ≤ 手册要求 60s）。
"""

from __future__ import annotations

import argparse
import tempfile
import time as _t
from datetime import datetime

from hippo.core.db import connect
from hippo.core.migrate import migrate
from hippo.core.scheduler import Scheduler
from hippo.core.store import Store


def main() -> None:
    ap = argparse.ArgumentParser(description="M19.2 调度器到点模拟")
    ap.add_argument("--delay", type=int, default=90,
                    help="计划多少秒后到点（默认 90）")
    ap.add_argument("--tick", type=int, default=15,
                    help="模拟的 tick 周期秒数（生产 60）")
    args = ap.parse_args()

    conn = connect(tempfile.mktemp(suffix=".db"))
    migrate(conn)
    store = Store(conn)

    planned = int(_t.time()) + args.delay
    store.add_plan(session="s", due_at=planned, content="模拟：到点叫我",
                   raw_hint="90秒后叫我")

    sched = Scheduler(store, tick_seconds=args.tick)
    sched.sweep()
    fired_at: list[int] = []
    sched.on_fire = lambda p: fired_at.append(int(_t.time()))

    # 直接以 tick 周期步进跑真实时钟，不用 sleep 累计误差
    import asyncio

    async def drive() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(sched.run_forever(stop=stop))
        while not fired_at:
            await asyncio.sleep(0.5)
        stop.set()
        await task

    asyncio.run(drive())

    t = fired_at[0]
    print(f"planned {datetime.fromtimestamp(planned):%H:%M:%S} "
          f"fired {datetime.fromtimestamp(t):%H:%M:%S} "
          f"delay={t - planned}s")


if __name__ == "__main__":
    main()
