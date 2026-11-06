"""M14.1 开工探针：先把权重公式量一遍，再决定照抄还是改。

## 为什么要有这个工具

手册 §M14.1 给了公式：

    w = clamp(w0 × decay(hl[tier]) × (1+0.2·rc) × (1+0.3·arousal) × pin)

抄下来只要十分钟。但这个公式有三个地方**读起来没问题、跑起来会出事**，
而且三种毛病都不会报错——只会让 90 天后的库变成一坨：

1. **自指**：`hl` 按"当前 tier"取，而 tier 又由 weight 算出来。
   hot 半衰期 3 天 → 3 天后 weight 掉一半 → 判成 warm → 半衰期变 30 天
   → 用**绝对年龄**重算 → weight 跳回 0.85 → 又判成 hot。
   于是 hot/warm 每轮翻转，tier 永远不稳定。

2. **饱和**：增益最大 2.0(rc) × 1.3(arousal) × 2.0(pin) = 5.2 倍。
   w0=0.75 的事件被 clamp 削平在 1.0，一削就是几十天——这期间
   "时间越久权重越低"在**输出上不成立**（曲线是平顶），tier 也跟着冻结。

3. **重复计 arousal**：`w0` 在写入时就是 `0.40 + 0.50×arousal` 算出来的，
   公式里再乘一次 `(1+0.3×arousal)`，同一件事收两遍钱。

## 这个工具怎么判

不猜，跑 90 天，四个指标：

- `振荡`：同一条事件的 tier 变了几次。健康值接近 0（单向降级）。
- `饱和率`：clamp 前一 >1 的比例。健康值是 0。
- `单调违例`：w(t+1) > w(t) 的天数（在没被召回的前提下）。
- top-8 保留：DoD 说"30 天前的重要事件仍在 top-8"，直接验。

两种公式并排跑，谁的数好用谁的。

    python3 -m tools.probe_weight --db /tmp/h2/mem.db --days 90
    python3 -m tools.probe_weight --db /tmp/h2/mem.db --days 90 --formula manual
"""

from __future__ import annotations

import argparse
import math
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DAY = 86400

# 手册 §M14.1 的三档半衰期（按 tier）
HL_BY_TIER = {1: 3.0, 2: 30.0, 3: 180.0}

# 修正版（D82）：按 w0 分档，越重要忘得越慢。w0 是常量，不自指。
HL_BY_W0 = ((0.70, 180.0), (0.40, 30.0), (0.00, 3.0))

TIER_HOT, TIER_WARM = 0.70, 0.30
# 滞回带：升级用 0.70，降级要跌破 0.65 才算。
# 没有它的话，w 恰好落在 0.70 的事件会每天翻一次——探针里 id=13
# （w0 正好 0.70）90 天翻了 30 次。tier 一抖，M16 的注入深度跟着抖，
# 用户会看到"同一条记忆今天给全文、明天给一行"。
HYST = 0.05


def _hl_of_w0(w0: float) -> float:
    for lo, hl in HL_BY_W0:
        if w0 >= lo:
            return hl
    return 3.0


@dataclass
class Row:
    id: int
    w0: float
    arousal: float
    recall: int
    last_access: int | None
    ingest: int
    pinned: bool
    tier: int
    weight: float
    # 统计
    flips: int = 0          # 全部 tier 变化（含被召回后重新升级，正常）
    idle_flips: int = 0     # 只算"没被召回那天"的变化，正常应为 0
    clamped: int = 0
    mono_violation: int = 0
    _prev: float = field(default=0.0)


def clamp(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def weight_manual(r: Row, now: int) -> tuple[float, bool]:
    """手册原式。返回 (weight, 是否被 clamp 削平)。"""
    base = max(r.last_access or 0, r.ingest)
    dt = max(0.0, (now - base) / DAY)
    hl = HL_BY_TIER.get(r.tier, 30.0)
    decay = math.exp(-math.log(2) * dt / hl)
    recall = 1 + 0.20 * min(r.recall, 5)
    arousal = 1 + 0.30 * r.arousal
    pin = 2.0 if r.pinned else 1.0
    raw = r.w0 * decay * recall * arousal * pin
    return clamp(raw), raw > 1.0


def weight_revised(r: Row, now: int) -> tuple[float, bool]:
    """D82 修正版。

    - 半衰期按 w0 定（常量），不自指 → 不会振荡
    - 去掉 arousal（w0 里已经有了）
    - "用进"改成延长半衰期，而不是直接乘权重 → 永不顶棚
    - pin = 不衰减，而不是 ×2
    """
    base = max(r.last_access or 0, r.ingest)
    dt = max(0.0, (now - base) / DAY)
    if r.pinned:
        return clamp(r.w0), False
    hl = _hl_of_w0(r.w0) * (1 + 0.25 * min(r.recall, 5))
    w = r.w0 * (0.5 ** (dt / hl))
    return clamp(w), w > 1.0


def _tier(w: float, pinned: bool, prev: int | None = None) -> int:
    """由 weight 派生 tier。**tier 是输出，不是输入**——这是断自指的关键。

    带滞回：升 hot 要 ≥0.70，掉出 hot 要 <0.65。降 cold 要 <0.30，
    出 cold 要 ≥0.35。
    """
    if pinned:
        return 1
    hi = TIER_HOT - (HYST if prev == 1 else 0.0)
    lo = TIER_WARM + (HYST if prev == 3 else 0.0)
    if w >= hi:
        return 1
    return 2 if w >= lo else 3


# ------------------------------------------------------------------ 话题命中
def topic_events(conn: sqlite3.Connection) -> dict[str, list[int]]:
    """按"常问话题"给事件分组。模拟检索命中用。

    不用真的跑 M9/M10 检索——那要 90 天 × 3 轮 × 20ms，而且这里要验的是
    权重曲线的形状，不是检索质量。命中用关键词粗匹配，够用。
    """
    topics = {
        "老王": ["老王", "小李", "北京", "喂猫"],
        "搬家": ["搬家", "海淀", "朝阳", "通勤"],
        "工作": ["老板", "方案", "辞职", "报告", "字节", "骂"],
        "猫": ["猫", "年糕", "挠"],
        "忌口": ["辣", "香菜", "冰"],
    }
    out: dict[str, list[int]] = {}
    for name, kws in topics.items():
        ids: list[int] = []
        for r in conn.execute("SELECT id, summary, keywords FROM events"):
            blob = f"{r['summary']} {r['keywords']}"
            if any(k in blob for k in kws):
                ids.append(int(r["id"]))
        out[name] = ids
    return out


def load(conn: sqlite3.Connection) -> list[Row]:
    rows = []
    for r in conn.execute(
            "SELECT id, w0, arousal, recall_count, last_access, ingest_time,"
            " pinned, tier, weight FROM events WHERE archived = 0"):
        rows.append(Row(
            id=int(r["id"]), w0=float(r["w0"]), arousal=float(r["arousal"]),
            recall=int(r["recall_count"]), last_access=r["last_access"],
            ingest=int(r["ingest_time"]), pinned=bool(r["pinned"]),
            tier=int(r["tier"]), weight=float(r["weight"]),
            _prev=float(r["weight"]),
        ))
    return rows


def run(conn: sqlite3.Connection, days: int, formula: str, seed: int = 7) -> dict:
    rows = load(conn)
    topics = topic_events(conn)
    fn = weight_manual if formula == "manual" else weight_revised
    t0 = max(r.ingest for r in rows)
    rng = random.Random(seed)

    start_hot = {r.id for r in rows if r.tier == 1}
    start_weight = {r.id: r.weight for r in rows}
    hist: list[tuple[int, float, float]] = []   # (day, hot 占比, 平均 weight)
    tier_hist: list[dict[int, int]] = []
    recalled_days: dict[int, int] = {}

    for d in range(1, days + 1):
        now = t0 + d * DAY
        # 一天问两个话题，命中者被 touch（M14.3 的行为）
        recalled_today: set[int] = set()
        for name in rng.sample(sorted(topics), 2):
            hits = topics[name]
            if not hits:
                continue
            for eid in rng.sample(hits, min(3, len(hits))):
                r = next(x for x in rows if x.id == eid)
                r.recall += 1
                r.last_access = now
                recalled_today.add(eid)
                recalled_days[eid] = recalled_days.get(eid, 0) + 1

        # 每日重算
        hot = 0
        tot = 0.0
        dist = {1: 0, 2: 0, 3: 0}
        for r in rows:
            w, cut = fn(r, now)
            if cut:
                r.clamped += 1
            new_tier = _tier(w, r.pinned, r.tier)
            if new_tier != r.tier:
                r.flips += 1
                if r.id not in recalled_today and d > 1:
                    r.idle_flips += 1
            r.tier = new_tier
            # 单调性：只查"没被召回的那天"是不是涨了
            if r.id not in recalled_today and d > 1 and w > r._prev + 1e-9:
                r.mono_violation += 1
            r._prev = w
            r.weight = w
            dist[new_tier] += 1
            tot += w
            if new_tier == 1:
                hot += 1
        hist.append((d, hot / len(rows), tot / len(rows)))
        tier_hist.append(dist)

    rows.sort(key=lambda x: -x.weight)
    top8 = [r.id for r in rows[:8]]
    # DoD：30 天前的重要事件（初始 hot）是否还在 top-8
    kept = start_hot & set(top8)

    return {
        "formula": formula,
        "rows": rows,
        "hist": hist,
        "tier_hist": tier_hist,
        "start_hot": start_hot,
        "kept": kept,
        "top8": top8,
        "oscillating": [r.id for r in rows if r.idle_flips >= 3],
        "idle_flips": sum(r.idle_flips for r in rows),
        "clamp_rate": sum(r.clamped for r in rows) / max(1, len(rows) * days),
        "mono": sum(r.mono_violation for r in rows),
        "start_weight": start_weight,
    }


def report(res: dict, days: int, verbose: bool = True) -> None:
    rows, hist = res["rows"], res["hist"]
    print(f"\n===== {res['formula']} · {days} 天 =====")
    print(f"hot 占比：day1 {hist[0][1]:.1%} → day{days} {hist[-1][1]:.1%}"
          f"  区间 [{min(h[1] for h in hist):.1%}, {max(h[1] for h in hist):.1%}]")
    print(f"平均 weight：{hist[0][2]:.3f} → {hist[-1][2]:.3f}")
    print(f"clamp 削平率 {res['clamp_rate']:.1%}   "
          f"单调违例 {res['mono']}   空闲期翻层 {res['idle_flips']} 次")
    print(f"初始 hot {sorted(res['start_hot'])} → 90 天后 top-8 里还剩 "
          f"{sorted(res['kept'])}（{len(res['kept'])}/{len(res['start_hot'])}）")
    if verbose:
        print("\n  最终 top-8：")
        for r in rows[:8]:
            print(f"    id={r.id:<3} w0={r.w0:.2f} rc={r.recall:<2} "
                  f"tier={r.tier} w={r.weight:.3f} flip={r.flips}")

    # tier 迁移矩阵：拿最后一天的分布和第一天比，看流向
    d0, d1 = res["tier_hist"][0], res["tier_hist"][-1]
    print(f"\n  tier 分布 day1 {d0}  →  day{days} {d1}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--formula", choices=["manual", "revised", "both"], default="both")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    forms = ["manual", "revised"] if a.formula == "both" else [a.formula]
    out = {}
    for f in forms:
        res = run(conn, a.days, f, a.seed)
        out[f] = res
        report(res, a.days)

    if len(out) == 2:
        print("\n===== 对照 =====")
        for k in ("clamp_rate", "mono"):
            print(f"  {k:<12} manual={out['manual'][k]:.3f}   revised={out['revised'][k]:.3f}")
        print(f"  {'空闲期翻层':<9} manual={out['manual']['idle_flips']}   "
              f"revised={out['revised']['idle_flips']}")
        print(f"  {'DoD 保留':<10} manual={len(out['manual']['kept'])}/{len(out['manual']['start_hot'])}   "
              f"revised={len(out['revised']['kept'])}/{len(out['revised']['start_hot'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
