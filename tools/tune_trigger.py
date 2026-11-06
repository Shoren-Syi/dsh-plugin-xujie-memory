"""话题切换阈值标定（M12.3）。

扫五档 Jaccard 阈值，看两个互相拉扯的数：

    触发率   每轮检索的概率 → 直接乘成本
    漏触发   该检索没检索的比例 → 直接表现为"AI 忘事"

手册的取舍是"漏触发 <10% 的前提下，触发率最低"。
这个方向是刻意的：多检索一次花 600 token，AI 忘事用户会换掉它。

## 口径说明（和线上不完全一样，必须写清）

标定用的是 fixture 里**上一句用户消息**的关键词集，不是线上"上一轮 pack 的
8 条事件关键词并集"。后者更大，J 会系统性偏低，触发率会比这里高。
所以这份表给的是"下界"，线上实际触发率要再加 3~5 个点（见 R-12.3）。

用同一份 fixture 跑两遍也没意义——所以这里只做**参数扫描**，
不拿来调判据本身。
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path

from hippo.config import load_config
from hippo.core.store import Store
from hippo.core.retrieve import extract_entities_in_query
from hippo.core.topic import (
    MIN_INTERVAL_TURNS, last_keywords, relate, save_keywords, should_retrieve,
    user_keywords,
)

DEFAULT_DB = Path("reports/data/eval_l1.db")
DEFAULT_DIALOG = Path("tests/fixtures/dialog_100.jsonl")
GRID = (0.10, 0.15, 0.20, 0.25, 0.30, 0.35)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--dialog", default=str(DEFAULT_DIALOG))
    ap.add_argument("--session", default="main")
    ap.add_argument("--grid", default="")
    ap.add_argument("--interval", type=int, default=MIN_INTERVAL_TURNS,
                    help="两次检索最小间隔（轮），0 = 关掉节流")
    ap.add_argument("--window", type=int, default=3,
                    help="用几轮关键词模拟 pack（线上约 8 条事件 ≈ 3~5 轮）")
    args = ap.parse_args()

    rows = [json.loads(line) for line in
            Path(args.dialog).read_text(encoding="utf-8").splitlines() if line.strip()]
    grid = tuple(float(x) for x in args.grid.split(",")) if args.grid else GRID

    store = Store.open(args.db, load_config())
    try:
        # 用 3 轮滑动窗口模拟"上一轮 pack 的关键词"：线上 pack 装 8 条事件，
        # 大致覆盖最近 3~5 轮的内容。只用上一句会严重低估话题集的规模——
        # 而规模直接决定覆盖率的分母。
        base = []
        for _th in (0,):
            win: deque[set[str]] = deque(maxlen=args.window)
            for r in rows:
                kb = user_keywords(store, args.session, r["cur"])
                ka = set().union(*win) if win else set()
                score, _basis = relate(kb, ka, r["cur"], store=store)
                base.append((score, bool(r["switch"]), kb, r["cur"]))
                win.append(kb)
    finally:
        store.close()

    n = len(base)
    n_switch = sum(1 for _, sw, _, _ in base if sw)
    print(f"对话 {n} 轮，人工标切换 {n_switch} 轮（{n_switch / n:.0%}）  "
          f"节流间隔 {args.interval} 轮")
    print(f"相似度分布：{_hist([b[0] for b in base])}")
    print()
    print(f"{'th':>5} {'触发率':>7} {'漏触发':>7} {'误触发':>7}  {'触发轮数':>8}")

    best = None
    for th in grid:
        # 节流是有状态的：同一份对话，第 3 轮判不判要看第 1 轮有没有检索。
        # 所以每档都要从头跑一遍，不能复用上面的 base。
        store = Store.open(args.db, load_config())
        try:
            for k in ("turn_no", "last_retrieve_turn", "last_pack_keywords"):
                store.set_state(args.session, k, "0")
            flags = []
            for r in rows:
                tr = should_retrieve(store, args.session, r["cur"],
                                     threshold=th, interval=args.interval)
                ids, _ = extract_entities_in_query(r["cur"], args.session, store)
                save_keywords(store, args.session,
                              user_keywords(store, args.session, r["cur"]), ids)
                flags.append(tr.fire)
        finally:
            store.close()

        rate = sum(flags) / n
        miss = sum(1 for f, b in zip(flags, base) if b[1] and not f) / max(1, n_switch)
        extra = sum(1 for f, b in zip(flags, base) if b[1] is False and f) / max(1, n - n_switch)
        mark = ""
        if miss < 0.10 and (best is None or rate < best[1]):
            best = (th, rate)
            mark = "  <-- 候选"
        print(f"{th:5.2f} {rate:7.0%} {miss:7.0%} {extra:7.0%}  {sum(flags):8d}{mark}")

    if best:
        print(f"\n建议 trigger_jaccard = {best[0]:.2f}（触发率 {best[1]:.0%}，漏触发 <10%）")
    else:
        print("\n没有一档同时满足「漏触发 <10%」——说明判据本身不够，别调阈值。")
    return 0


def _hist(values: list[float]) -> str:
    bins = [0, 0.001, 0.15, 0.25, 0.35, 0.55, 1.01]
    labels = ["J=0", "0~.15", ".15~.25", ".25~.35", ".35~.55", ">.55"]
    counts = [0] * (len(bins) - 1)
    for v in values:
        for i in range(len(bins) - 1):
            if bins[i] <= v < bins[i + 1]:
                counts[i] += 1
                break
    return "  ".join(f"{lab}:{c}" for lab, c in zip(labels, counts))


if __name__ == "__main__":
    raise SystemExit(main())
