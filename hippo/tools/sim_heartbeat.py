"""M24.2 零成本空转验证：挂机 N 分钟，花几次钱？

    python -m hippo.tools.sim_heartbeat --minutes 10
    python -m hippo.tools.sim_heartbeat --minutes 1440      # 等效 24 小时

手册 DoD：挂机 24 小时，`metrics` 表新增 LLM 调用数 = 0。

**为什么用虚拟时钟而不是真等 24 小时**：这一条验的是"循环里有没有
会掏钱的分支"，不是"机器稳不稳"。虚拟时钟把 1440 个 tick 压进几秒，
每一轮走的是完全相同的代码路径（同一份 tick()），所以"0 次调用"
这个结论不受时钟影响。真实进程的稳定性另有一项短时实测（见 R-24.2），
两件事分开证，各自证得干净。

库里会预置：一条到期计划、一条过期疑问、一行情绪、一组重复事件——
如果心跳什么都没做，那"0 调用"就毫无意义。必须先证明它**干了活**。
"""

from __future__ import annotations

import argparse
import logging
import time

from ..config import Config
from ..core.db import connect
from ..core.heartbeat import Heartbeat
from ..core.migrate import migrate
from ..core.store import Store

# 起点 = 明天 03:00。选 03:00 是为了让"每日巩固"那一轮落在窗口里；
# 选明天是为了让"今天创建的疑问"在起点已经过期——模拟的是"从现在起
# 挂机 24 小时"，不是"回放历史"。
_tomorrow = time.localtime(time.time() + 86400)
T0 = int(time.mktime((_tomorrow.tm_year, _tomorrow.tm_mon, _tomorrow.tm_mday,
                      3, 0, 0, 0, 0, -1)))


def _seed(store: Store) -> None:
    """灌一批"心跳应该处理"的数据。"""
    # 计划：一条早就到期（应在首轮触发）、一条明天到期（不该触发）
    store.add_plan(session="s", due_at=T0 - 120, content="叫用户起床")
    store.add_plan(session="s", due_at=T0 + 86400, content="提醒交报告")
    # 疑问：立刻过期（expire_days=0），起点时已经超时 → 应转成 cold 记忆
    store.add_doubt(session="s", question="用户说的「那边」是公司还是老家？",
                    expire_days=0)
    # 情绪：一天没动过（应衰减并落库）
    store.add_turns("s", [("user", "今天心烦", 0)], ts=T0 - 86400)
    store.upsert_affect("s", updated=T0 - 86400, joy=90, anger=70)
    # 重复事件：三条几乎一样（夜里巩固会合并）
    for i in range(3):
        store.add_event(session="s", raw=f"用户说他下周要去深圳出差{i}",
                        summary="用户下周去深圳出差", keywords=["深圳", "出差"],
                        event_time=T0 - 86400 * (i + 1),
                        ingest_time=T0 - 86400 * (i + 1))


def main() -> int:
    logging.getLogger("hippo").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="M24.2 心跳空转模拟")
    ap.add_argument("--minutes", type=int, default=10,
                    help="模拟多少分钟的挂机（1440 = 24 小时）")
    ap.add_argument("--interval", type=int, default=60, help="tick 间隔（秒）")
    args = ap.parse_args()

    conn = connect(":memory:")
    migrate(conn)
    store = Store(conn)
    _seed(store)

    cfg = Config()
    cfg.heartbeat.interval_seconds = args.interval
    hb = Heartbeat(store, cfg=cfg)

    ticks = max(1, args.minutes * 60 // args.interval)
    t_begin = time.perf_counter()
    for i in range(ticks):
        hb.tick(now=T0 + i * args.interval)
    elapsed = time.perf_counter() - t_begin

    c = hb.counts
    print(f"ticks={c['ticks']} plans_checked={c['plans']} "
          f"tier_recompute={c['tier']} llm_calls={c['llm_calls']}")
    print(f"  plans_fired={c['plans_fired']} doubts_converted={c['doubts_n']} "
          f"mood_written={c['mood_n']} clusters={c['clusters']} "
          f"errors={c['errors']}")
    print(f"  实际耗时 {elapsed * 1000:.0f}ms / {ticks} 轮"
          f"（平均 {elapsed / ticks * 1000:.2f}ms 一轮）")
    # 第二次跑同样的窗口做对照：巩固/疑问只在第一次有活干，之后必须归零
    before = dict(c)
    for i in range(ticks):
        hb.tick(now=T0 + (ticks + i) * args.interval)
    c2 = hb.counts
    print(f"  第二轮同窗口：plans_fired+{c2['plans_fired'] - before['plans_fired']} "
          f"doubts+{c2['doubts_n'] - before['doubts_n']} "
          f"mood+{c2['mood_n'] - before['mood_n']} "
          f"clusters+{c2['clusters'] - before['clusters']}")
    return 0 if c["llm_calls"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
