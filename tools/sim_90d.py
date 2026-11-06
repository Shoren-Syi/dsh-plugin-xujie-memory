"""M14 DoD 的端到端验证：跑 90 天，看老记忆还找不找得回来。

手册的验收只有一句：

> 模拟 90 天时间推进，30 天前的重要事件仍能被检索到（top-8 内）。

这句话没法用单元测试验——它跨越了检索（M9/M10）、装配（M13）、
重算（M14）三条链路，任何一环坏了都会表现成"找不到"。所以单独做一个
仿真：真的走 pack（不调 API，抽取关掉），真的每天重算，真的用查询去检索。

    python3 -m tools.sim_90d --db /tmp/h2/mem.db --days 90

## 仿真里最容易被偷掉的公平性

1. **每天的对话要覆盖各个话题**。只聊工作的话，工作类事件天天被召回、
   权重回升，其它全冷掉，结论会变成"这套动力学只会记住工作"——
   那是我写脚本的方式造成的，不是系统的问题。
2. **查询要用用户的口气**，不能直接拿事件摘要去查。拿摘要查等于
   把答案当题目，关键词命中率 100%，一切都会"很好"。
3. **中途不能 reseed**。库是 day0 的快照，之后所有变化都来自仿真本身。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hippo.config import load_config                  # noqa: E402
from hippo.core.dynamics import recompute             # noqa: E402
from hippo.core.pack import pack                      # noqa: E402
from hippo.core.rank import l2_search                 # noqa: E402
from hippo.core.store import Store                    # noqa: E402

# 用户会怎么问。刻意不抄摘要里的词——抄了就是自问自答。
QUERIES = {
    "老板骂我": ["老板上次骂我那事", "我是不是该辞职了", "季度报告还没写"],
    "搬家": ["我住哪儿来着", "搬完家通勤多久", "房租涨了多少"],
    "老王": ["老王什么时候来", "我答应老王什么了", "老王坐什么来"],
    "猫": ["我家猫叫什么", "我怕猫吗", "年糕几岁了"],
    "忌口": ["我能吃辣吗", "我忌口什么", "香菜行不行"],
    "工作": ["我在哪上班", "我做什么方向的", "当时在场的人多吗"],
}

# 每天轮流聊两个话题。顺序固定 = 可复现，不用随机数。
ROTATION = ["工作", "老王", "忌口", "搬家", "猫"]


def run(days: int, db: str, *, keep: bool = False) -> int:
    cfg = load_config()
    tmp = Path(tempfile.mkdtemp(prefix="hippo-sim-"))
    work = tmp / "mem.db"
    shutil.copy(db, work)
    store = Store.open(work, cfg)

    conn = sqlite3.connect(work)
    conn.row_factory = sqlite3.Row
    base_ts = int(conn.execute(
        "SELECT max(ingest_time) FROM events").fetchone()[0])
    day0 = datetime.fromtimestamp(base_ts).replace(hour=10, minute=0, second=0)

    # DoD 的观察对象：day0 的 hot 事件（吵架那三条）
    watch = [int(r["id"]) for r in conn.execute(
        "SELECT id FROM events WHERE tier = 1 ORDER BY id")]
    watch_txt = {int(r["id"]): str(r["summary"])[:18] for r in conn.execute(
        "SELECT id, summary FROM events")}
    conn.close()

    print(f"库 {db}　{len(watch)} 条 hot 起手：{[watch_txt[i] for i in watch]}")
    print(f"起点 {day0:%Y-%m-%d}　推进 {days} 天\n")

    hits_at: dict[int, list] = {}
    golden: dict[int, int] = {}      # 第 d 天造的那件"强事件"的 id
    try:
        for d in range(1, days + 1):
            now = day0 + timedelta(days=d)
            for topic in (ROTATION[d % len(ROTATION)],
                          ROTATION[(d * 3) % len(ROTATION)]):
                for q in QUERIES[topic][:2]:
                    pack(store, "main", q, cfg=cfg, now=now, use_cache=True)
            # 必须有新事件进来，否则 hot 层会随着老事件降温而空掉——
            # 那是仿真没造输入造成的，不是公式的问题。
            #
            # 比例也要像真的：真人一天里绝大多数是琐事，偶尔一件强情绪的事。
            # 早先版本每 3 天只造一条 arousal=0.1 的琐事，等于把"高唤醒事件
            # 占比"人为压到 0，然后 hot 占比自然是 0%，验收当然不过。
            # **造输入的手一歪，指标就跟着歪，而且看不出来。**
            if d % 3 == 0:
                store.add_event(
                    session="main", raw=f"第{d}天的琐事：中午吃了面",
                    summary=f"第{d}天中午吃了面", keywords=["午饭", "面"],
                    event_time=int(now.timestamp()),
                    ingest_time=int(now.timestamp()),
                    w0=0.45, weight=0.45, arousal=0.1, tier=2)
            if d % 10 == 0:
                golden[d] = store.add_event(
                    session="main", raw=f"第{d}天跟人吵了一架，很上头",
                    summary=f"第{d}天跟人吵了一架，很上头",
                    keywords=["吵架", "上头", "第" + str(d) + "天"],
                    event_time=int(now.timestamp()),
                    ingest_time=int(now.timestamp()),
                    w0=0.75, weight=0.75, arousal=0.7, tier=1)

            rep = recompute(store, now, cfg=cfg.weight)
            if d in (1, 7, 30, 60, 90) or d == days:
                print(f"  day{d:>3}  {rep.line()}   迁移 {rep.matrix()}")
            if d in (7, 30, 60, 90) or d == days:
                # DoD 说的是"**30 天前**的重要事件"，不是"day0 的事件"。
                # 盯住第 d-30 天那件强事件：它此刻正好 30 天。
                target = {}
                if (d - 30) in golden:
                    target[golden[d - 30]] = f"day{d - 30}"
                hits_at[d] = _probe(store, target or dict.fromkeys(watch, "day0"), cfg)
    finally:
        store.close()

    print("\n===== DoD：30 天前的重要事件，此刻在不在 top-8 =====")
    print("  （每天只列「工作」话题的 3 个查询，无关话题命不中是应该的）")
    ok = 0
    for d, rows in sorted(hits_at.items()):
        cells = []
        any_hit = False
        for q, ids in rows:
            any_hit = any_hit or bool(ids)
            cells.append(f"{q[:7]}:{'/'.join(map(str, ids)) or '—'}")
        ok += 1 if any_hit else 0
        print(f"  day{d:>3}  " + "  ".join(cells)
              + ("   ✓" if any_hit else "   ✗ 没找到"))

    n = len(hits_at)
    print(f"\n  判定口径：这个时间点只要**有任意一个查询**把目标捞进 top-8 就算找到。")
    print(f"  用「12 次机会命中 6 次」当成绩是错的——3 个查询里 2 个问的是"
          f"别的主题，{n} 个时间点 × 3 个查询，最多也就 {n} 次该命中。")
    print(f"  → {ok}/{n}（{ok / n:.0%}）")

    if keep:
        print(f"\n仿真库保留在 {work}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def _probe(store, watch: dict[int, str], cfg) -> list[tuple[str, list[int]]]:
    """只拿"工作"话题的查询去打。

    早先版本把所有 18 个查询都算进去，然后拿"命中次数/18"当命中率——
    分母里 15 个查询问的是猫、忌口、搬家，吵架那三条凭什么进 top-8？
    分母错了会让指标看起来很差，然后有人去"优化"一个没坏的东西。
    """
    out = []
    for q in QUERIES["工作"]:
        res = l2_search(store, "main", q)
        got = [it.id for it in res.items][:8]
        out.append((q, [i for i in got if i in watch]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--keep", action="store_true", help="保留仿真库")
    a = ap.parse_args()
    return run(a.days, a.db, keep=a.keep)


if __name__ == "__main__":
    raise SystemExit(main())
