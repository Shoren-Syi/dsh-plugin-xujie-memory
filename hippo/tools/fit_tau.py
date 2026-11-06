# -*- coding: utf-8 -*-
"""M21.2 τ 拟合。

夹具 tests/fixtures/affect_scenarios.jsonl：6 场景，每场景 delta
（推出初值 v0）+ 人工直觉采样点 (分钟, 剩余值)。采样值是**设计者
直觉标注**（不是真机人工实验——M26 补），工具做的事是真实的：
对每场景网格搜索 τ 最小二乘拟合，与 init 值对比，偏差 >30% 给
调整建议并写回 config 片段。

用法：
    python -m hippo.tools.fit_tau --scenarios tests/fixtures/affect_scenarios.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from hippo.core.affect import BASELINE, MOODS, TAU_MINUTES

GRID = [1, 1.5, 2, 3, 4, 5, 7, 10, 14, 20, 30, 45, 60, 90, 120,
        180, 240, 300, 360, 480, 600, 720]          # 分钟


def fit_tau(v0: float, base: float, samples: list[tuple[float, float]]
            ) -> float:
    """网格搜索 τ：v(t) = base + (v0-base)·e^(-t/τ)，最小化 SSE。"""
    best_tau, best_sse = None, math.inf
    for tau in GRID:
        sse = 0.0
        for t, v in samples:
            pred = base + (v0 - base) * math.exp(-t / tau)
            sse += (pred - v) ** 2
        if sse < best_sse:
            best_tau, best_sse = tau, sse
    return float(best_tau)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenarios", type=Path, required=True)
    args = ap.parse_args()

    rows = [json.loads(line) for line in
            args.scenarios.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 6, f"手册验收 #1：六维 τ 都要拟合过，现 {len(rows)}/6"
    seen = {r["mood"] for r in rows}
    assert seen == set(MOODS), f"缺维度: {set(MOODS) - seen}"

    print(f"{'mood':<9} {'init':>6} {'fit':>6} {'bias':>7}  采样点")
    out: dict[str, float] = {}
    for r in rows:
        m = r["mood"]
        base = BASELINE[m]
        v0 = base + r["delta"]
        samples = [(float(t), float(v)) for t, v in r["samples"].items()]
        tau = fit_tau(v0, base, samples)
        init = TAU_MINUTES[m]
        bias = (tau - init) / init
        flag = " ← 调整" if abs(bias) > 0.30 else ""
        out[m] = tau
        pts = " ".join(f"{t:.0f}m:{v:.0f}" for t, v in sorted(samples))
        print(f"{m:<9} {init:>6.1f} {tau:>6.1f} {bias:>+6.0%}{flag}  {pts}")
        print(f"          直觉: {r['note']}")

    avg_bias = sum(abs((out[m] - TAU_MINUTES[m]) / TAU_MINUTES[m])
                   for m in out) / len(out)
    print(f"\n平均直觉偏差: {avg_bias:.0%}（验收 #2 要求 <20%）")

    # ---- 生成写回 config 的片段（只调偏差 >30% 的）
    adj = {m: t for m, t in out.items()
           if abs((t - TAU_MINUTES[m]) / TAU_MINUTES[m]) > 0.30}
    if adj:
        print("\n建议写回 config.affect.tau_minutes（偏差 >30% 的维度）：")
        for m, t in adj.items():
            print(f"  {m}: {TAU_MINUTES[m]} -> {t:.0f}")
    else:
        print("\n无维度偏差 >30%，初始 τ 全部保留（写回 config 无需改动）")


if __name__ == "__main__":
    main()
