"""M14.2 每日重算 —— 把新权重刷回库里，并记下"谁在搬家"。

## 为什么要有"迁移矩阵"

只报"hot 12 条 warm 30 条"没有用：这个数在健康和不健康的情况下都可能一样。
有用的是**流向**：

    hot→warm 3   warm→hot 1   warm→cold 8   cold→warm 0

- `cold→warm` 不为 0，说明有记忆被召回后"复活"了。这是好事，但要确认
  不是靠 `recall_count` 虚高顶上去的（M14.3 只管注入过的事件）。
- `warm→hot` 很多，说明阈值太低或者 w0 普遍虚高。
- 全是单向往下、一条都不回升，说明这套库已经没有新输入了。

矩阵是唯一能看出"这套动力学是不是活的"的东西。所以它是 M14.2 的
验收项之一，不只是日志。

## 幂等

`compute_weight` 只读 `w0 / pinned / recall_count / last_access / ingest_time`，
**不读 `weight`**。所以连跑 100 次结果一样，重算失败可以放心重试。
如果哪天有人把 `weight` 加进公式（比如"权重高的更容易被再召回"），
幂等立刻破掉，而且不会有任何报错——只会让库每次重算都在缓慢漂移。
"""

from __future__ import annotations

import time
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Any

from .weight import compute_weight, now_ts, tier_of

__all__ = ["RecomputeReport", "recompute"]

# 比 dict 快，比 pydantic 快得多。重算一次要走 10 万遍属性访问。
_Item = namedtuple("_Item", "w0 pinned recall_count last_access ingest_time")


@dataclass
class RecomputeReport:
    total: int = 0
    changed: int = 0
    dry_run: bool = False
    dist: dict[int, int] = field(default_factory=dict)
    moves: dict[tuple[int, int], int] = field(default_factory=dict)
    pinned_bad: int = 0          # pinned 却不是 hot 的条数，应为 0
    elapsed_ms: float = 0.0
    w_min: float = 0.0
    w_avg: float = 0.0
    w_max: float = 0.0

    @property
    def hot_ratio(self) -> float:
        return self.dist.get(1, 0) / self.total if self.total else 0.0

    def line(self) -> str:
        d = self.dist
        return (f"{self.total} 条 {self.elapsed_ms:.0f}ms | "
                f"hot={d.get(1, 0)}({self.hot_ratio:.1%}) "
                f"warm={d.get(2, 0)} cold={d.get(3, 0)}")

    def matrix(self) -> str:
        if not self.moves:
            return "（无迁移）"
        names = {1: "hot", 2: "warm", 3: "cold"}
        parts = []
        for (a, b), n in sorted(self.moves.items()):
            arrow = "↑" if b < a else "↓"
            parts.append(f"{names[a]}{arrow}{names[b]} {n}")
        return "  ".join(parts)


def recompute(store: Any, now: Any = None, *, cfg: Any = None,
              dry_run: bool = False, session: str | None = None,
              limit: int = 0) -> RecomputeReport:
    """全库重算权重与 tier。

    `now` 可以是 datetime / 时间戳 / None（= 现在）。测试传固定值，
    生产不传。

    `dry_run=True` 只算不写，用来回答"如果现在重算会怎样"。
    """
    cfg = cfg if cfg is not None else getattr(store, "cfg", None)
    t = now_ts(now)
    t0 = time.perf_counter()

    rows = store.decay_rows(session=session, limit=limit)
    rep = RecomputeReport(total=len(rows), dry_run=dry_run)

    updates: list[tuple[int, float, int]] = []
    total_w = 0.0
    rep.w_min = 1.0
    for r in rows:
        item = _Item(
            w0=float(r["w0"] or 0.0),
            pinned=bool(r["pinned"]),
            recall_count=int(r["recall_count"] or 0),
            last_access=r["last_access"],
            ingest_time=int(r["ingest_time"] or 0),
        )
        old_tier = int(r["tier"])
        old_w = float(r["weight"])
        w = compute_weight(item, t, cfg=cfg)
        nt = tier_of(w, pinned=item.pinned, prev=old_tier, cfg=cfg)

        if item.pinned and nt != 1:
            rep.pinned_bad += 1
        rep.dist[nt] = rep.dist.get(nt, 0) + 1
        if nt != old_tier:
            rep.moves[(old_tier, nt)] = rep.moves.get((old_tier, nt), 0) + 1
        # 0.0005 = 小于四位小数的抖动不算"变了"，否则 changed 永远是 total
        if abs(w - old_w) > 5e-4 or nt != old_tier:
            rep.changed += 1
            updates.append((int(r["id"]), w, nt))

        total_w += w
        rep.w_min = min(rep.w_min, w)
        rep.w_max = max(rep.w_max, w)

    if rows:
        rep.w_avg = total_w / len(rows)
    if rep.w_min == 1.0 and not rows:
        rep.w_min = 0.0

    if not dry_run and updates:
        store.set_weights(updates, chunk=int(getattr(
            getattr(cfg, "weight", None), "batch", 2000)))

    rep.elapsed_ms = (time.perf_counter() - t0) * 1000
    return rep
