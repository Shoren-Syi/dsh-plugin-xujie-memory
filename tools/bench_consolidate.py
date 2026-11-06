"""M15 巩固的端到端基准：重复率到底降了多少。

## 为什么不能只看 `dup_rate()`

`consolidate.dup_rate()` 算的是"用当前判据还能合掉多少"。合并一轮之后再跑，
它能找到的东西自然变少——**这个数是自我实现的**，拿它验收等于自己证明自己。

真正要回答的是："同一件事说的那几条，现在还剩几条"。这需要 ground truth，
也就是"每条事件原本属于哪个话题"。生产库里没有这个字段，所以只能造。

`tools/gen_dialog30.py` 造数据时就给每条打了 `truth`。这个基准把 truth
留在内存里（不写库——写了就污染被测对象），合并后按"代表者"回算：

    rep(id) = events[id].superseded_by or id
    residual = Σ_话题 (该话题的 rep 去重个数 - 1)

一个话题的 6 条事件如果全并成一条，distinct rep = 1，residual = 0；
散在 3 个簇里就是 2。跨话题合并会让两个话题共享同一个 rep —— 那是误合并，
单列 purity 报出来，不能混进 residual。

用法：
    python3 -m tools.bench_consolidate --n 142
    python3 -m tools.bench_consolidate --scale 10000     # 只测聚类性能
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hippo.core.consolidate import (          # noqa: E402
    DupRow, find_clusters, candidate_pairs, run,
)
from hippo.core.db import connect              # noqa: E402
from hippo.core.migrate import migrate         # noqa: E402
from hippo.core.store import Store             # noqa: E402
from tools.gen_dialog30 import build           # noqa: E402


def _fresh(tmp: str, rows: list[dict]) -> Store:
    p = os.path.join(tmp, "mem.db")
    c = connect(p)
    migrate(c)
    s = Store(c)
    for r in rows:
        s.add_event(session="main", raw=r["raw"], summary=r["summary"],
                    keywords=r["keywords"], event_time=int(r["ts"]),
                    ingest_time=int(r["ts"]), w0=0.5, weight=0.5,
                    arousal=0.1, tier=2)
    return s


def _truth_map(rows: list[dict]) -> dict[int, str | None]:
    """事件 id → 话题。id 按插入顺序 = 行号，和 gen_dialog30 的 `id` 一致。"""
    return {i + 1: r["truth"] for i, r in enumerate(rows)}


def evaluate(store: Store, truth: dict[int, str | None]) -> dict:
    """按 ground truth 算合并后的簇质量。"""
    rep: dict[int, int] = {}
    for eid in truth:
        ev = store.get_event(eid)
        if ev is None:                 # 被归档了（本基准不归档，防御性写法）
            rep[eid] = eid
            continue
        rep[eid] = ev.superseded_by or eid

    by_topic: dict[str, list[int]] = defaultdict(list)
    for eid, t in truth.items():
        if t:
            by_topic[t].append(eid)

    residual = 0
    for t, ids in by_topic.items():
        residual += max(0, len({rep[i] for i in ids}) - 1)

    # purity：每个代表者下混了几个话题
    groups: dict[int, Counter] = defaultdict(Counter)
    for eid, t in truth.items():
        groups[rep[eid]][t] += 1
    cross = 0
    pure = 0
    tot = 0
    for g in groups.values():
        tot += sum(g.values())
        top = max(g.values())
        pure += top
        topics = [k for k in g if k is not None]
        if len(topics) > 1 or (None in g and len(g) > 1):
            cross += sum(g.values()) - top
    return {
        "residual": residual,
        "cross": cross,
        "purity": pure / tot if tot else 1.0,
        "clusters": len(groups),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=142, help="数据规模（话题 + filler）")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scale", type=int, default=0,
                    help="只测聚类性能，灌这么多条噪声事件")
    ap.add_argument("--keep", action="store_true", help="保留临时库")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="bench15-")
    try:
        if args.scale:
            # ---- 纯性能：造 N 条事件，关键词数量按真实分布（3~5 个）
            import random
            rng = random.Random(1)
            pool = [f"w{i}" for i in range(4000)]
            rows = []
            for i in range(args.scale):
                kw = rng.sample(pool, rng.randint(3, 5))
                rows.append({"raw": f"事件{i}，" + "内容" * rng.randint(5, 20),
                             "summary": f"摘要{i}", "keywords": kw,
                             "ts": 1793754000 + rng.randint(0, 30 * 86400),
                             "truth": f"t{i % 500}"})
            t0 = time.perf_counter()
            s = _fresh(tmp, rows)
            t_build = time.perf_counter() - t0
            t0 = time.perf_counter()
            drows = [DupRow.of(d) for d in s.consolidate_rows("main")]
            pairs = candidate_pairs(drows)
            t_pair = time.perf_counter() - t0
            t0 = time.perf_counter()
            cl = find_clusters(drows)
            t_cl = time.perf_counter() - t0
            print(f"规模 {args.scale} 条")
            print(f"  灌库     {t_build:.1f}s")
            print(f"  候选对   {len(pairs):,} / {len(drows) * (len(drows) - 1) // 2:,}"
                  f"   {t_pair * 1000:.0f}ms")
            print(f"  聚类     {len(cl)} 簇   {t_cl * 1000:.0f}ms"
                  f"   {'✓ 达标（<10s）' if t_cl < 10 else '✗ 超 10s'}")
            s.close()
            return 0

        # ---- 效果：带 truth 的 30 天数据
        rows = build(args.seed, 30, max(0, args.n - 42))
        truth = _truth_map(rows)
        s = _fresh(tmp, rows)
        n = len(rows)
        dup_before = sum(v - 1 for v in Counter(truth.values()).values()
                         if v > 1 and list(Counter(truth.values()).keys())[0] is not None)
        by_t = Counter(t for t in truth.values() if t)
        dup_before = sum(v - 1 for v in by_t.values() if v > 1)
        print(f"事件 {n} 条   话题 {len(by_t)} 个   独立事件 {truth_kind(truth)}")
        print(f"合并前：多余重复 {dup_before} 条 → 重复率 {dup_before / n:.1%}")

        t0 = time.perf_counter()
        rep = run(s, "main", now=1793754000 + 30 * 86400)
        dt = time.perf_counter() - t0
        m = evaluate(s, truth)
        after = m["residual"] / n
        print(f"\n{rep.line()}")
        print(f"合并后：多余重复 {m['residual']} 条 → 重复率 {after:.1%}")
        print(f"       纯度 {m['purity']:.1%}   跨话题误合 {m['cross']} 条"
              f"（{m['cross'] / n:.1%}）")
        print(f"       簇数 {m['clusters']}   耗时 {dt:.2f}s")
        ok1 = after < 0.05
        ok2 = m["cross"] / n < 0.02
        print(f"\nDoD：重复率 <5% {'✓' if ok1 else '✗'}   "
              f"误合并 <2% {'✓' if ok2 else '✗'}")
        s.close()
        return 0 if (ok1 and ok2) else 1
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)


def truth_kind(truth: dict[int, str | None]) -> int:
    return sum(1 for v in truth.values() if v is None)


if __name__ == "__main__":
    raise SystemExit(main())
