# -*- coding: utf-8 -*-
"""M20.1 情绪衰减 24 小时模拟。

用法：
    python -m hippo.tools.sim_affect --hours 24
    python -m hippo.tools.sim_affect --hours 24 --plot out.png

输出手册验收要求的内容：
  1. 六维从"被打满"状态衰减的采样表（10/30/60 分钟）
  2. 曲线与公式的 R²（验收 #1：>0.98——离散积分对解析解）
  3. 发散检查：全程 0~100（验收 #3）
"""
from __future__ import annotations

import argparse
import math

from hippo.core.affect import BASELINE, MOODS, TAU_MINUTES, decay_moods

# 模拟起点：六维全部打到 90（比"被打满"更狠的边界条件）
START = 90.0
SAMPLES = (10, 30, 60)          # 分钟（手册样例的采样点）
STEP = 1.0                      # 步长 1 分钟，离散积分


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--plot", nargs="?", const="affect_decay.png",
                    default=None, metavar="PNG")
    args = ap.parse_args()

    total = int(args.hours * 60)
    start = {m: START for m in MOODS}

    # ---- 离散积分：每分钟走一步
    curves: dict[str, list[float]] = {m: [START] for m in MOODS}
    moods = dict(start)
    for _ in range(total):
        moods = decay_moods(moods, baseline=BASELINE,
                            tau=TAU_MINUTES, minutes=STEP)
        for m in MOODS:
            curves[m].append(moods[m])

    # ---- 采样表（手册样例格式）
    print(f"start={START:.0f} baseline={BASELINE}")
    for m in MOODS:
        pts = " -> ".join(f"{t}min: {curves[m][t]:.1f}" for t in SAMPLES)
        print(f"{m:9s} {START:.0f} -> {pts}")

    # ---- R²：离散解 vs 解析解。衰减公式是线性的，60 步离散积分
    #      与连续解的差是 O(h)（h=1min），对 τ=3 的 surprise 相对误差
    #      也应 <2%。R² 按回归定义 1 - SSE/SST 算。
    worst = 1.0
    for m in MOODS:
        tau, base = TAU_MINUTES[m], BASELINE[m]
        ys = curves[m]
        xs = list(range(len(ys)))
        ybar = sum(ys) / len(ys)
        sst = sum((y - ybar) ** 2 for y in ys)
        # 解析解在相同 x 上取样（分钟为单位的浮点时间）
        ana = [base + (START - base) * math.exp(-x / tau) for x in xs]
        sse = sum((y - a) ** 2 for y, a in zip(ys, ana))
        r2 = 1 - sse / sst if sst > 0 else 1.0
        worst = min(worst, r2)
        print(f"R2[{m}] = {r2:.5f}")
    print(f"worst R2 = {worst:.5f}  ({'>=0.98 PASS' if worst >= 0.98 else 'FAIL'})")

    # ---- 发散检查
    lo = min(min(c) for c in curves.values())
    hi = max(max(c) for c in curves.values())
    print(f"bounds: [{lo:.2f}, {hi:.2f}]  "
          f"({'PASS' if 0 <= lo and hi <= 100 else 'FAIL'})")

    # ---- 终点回基线检查：24h 后 joy(τ=180) 应贴近基线
    end = {m: curves[m][-1] for m in MOODS}
    print(f"after {args.hours}h: " +
          " ".join(f"{m}={end[m]:.1f}(base {BASELINE[m]:.0f})" for m in MOODS))

    if args.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(9, 5))
        xs = [x / 60 for x in range(total + 1)]
        colors = {"joy": "#d62728", "sadness": "#1f77b4", "anger": "#ff7f0e",
                  "fear": "#9467bd", "disgust": "#8c564b", "surprise": "#2ca02c"}
        for m in MOODS:
            ax.plot(xs, curves[m], label=f"{m} (τ={TAU_MINUTES[m]:.0f}m)",
                    color=colors[m], lw=2)
            ax.axhline(BASELINE[m], color=colors[m], ls=":", lw=0.8, alpha=0.5)
        ax.set_xlabel("hours")
        ax.set_ylabel("value")
        ax.set_title(f"M20.1 affect decay, {args.hours}h, worst R2={worst:.4f}")
        ax.legend(ncol=3)
        ax.grid(alpha=0.2)
        fig.savefig(args.plot, dpi=120, bbox_inches="tight")
        print(f"plot -> {args.plot}")


if __name__ == "__main__":
    main()
