# -*- coding: utf-8 -*-
"""M22.1 弃权协议评测：50 个任务里模型该弃权的弃了、简单任务不该弃的没弃。

**如实声明**：本工具是**离线协议评测**——不调真机。做法是把每个任务
的"理想响应"（含或不含 giveup 标签）喂给解析/剥离/改写全链路，
验证协议契约（level 合法、decline 带 reason、标签零泄漏）。真机
"模型会不会用"（验收 #1：50 次里 ≥3 次）在 M26 真机评测补，届时
同一份夹具换成真模型输出即可。

用法：
    python -m hippo.tools.eval_giveup
    python -m hippo.tools.eval_giveup --tasks tests/fixtures/hard_tasks.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hippo.core.giveup import LEVELS, StreamFilter, audit, finish, rewrite

FIXTURE = Path(__file__).resolve().parent.parent.parent / "tests" / \
    "fixtures" / "hard_tasks.jsonl"


def simulate(row: dict) -> str:
    """按任务标注模拟模型的一次输出（协议合规版）。

    硬任务 → 带 giveup 标签（level 按标注）；简单任务 → 正常回复。
    这就是"理想模型"的样本，用来验证**代理层契约**是否扛得住。
    """
    if not row.get("hard"):
        return "好的，这就办。"
    level = row["expect"]
    reason = row["note"]
    body = "我先试着处理一下。" if level == "partial" else ""
    return f'{body}<giveup level="{level}" reason="{reason}"/>'


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tasks", type=Path, default=FIXTURE)
    ap.add_argument("--chunk", type=int, default=3,
                    help="流式切块大小（默认 3，专挑标签中间切）")
    args = ap.parse_args()

    rows = [json.loads(line) for line in
            args.tasks.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 50, f"手册口径 50 次交互，夹具 {len(rows)}"

    n_giveup = 0
    n_easy = 0
    easy_false = 0
    bad_level = 0
    missing_reason = 0
    leaked = 0
    rewrite_bad = 0

    for r in rows:
        raw = simulate(r)
        # 真流式：按 chunk 切，跨块缓冲必须扛住
        f = StreamFilter()
        out = "".join(f.feed(raw[i:i + args.chunk])
                      for i in range(0, len(raw), args.chunk))
        text = finish(out, f)

        # 标签零泄漏（验收 #1）
        if audit(text):
            leaked += 1

        if not r.get("hard"):
            n_easy += 1
            if f.tags:                       # 简单任务不该弃权
                easy_false += 1
            continue

        if not f.tags:
            rewrite_bad += 1                 # 硬任务没弃权 → 硬编了
            continue
        n_giveup += 1
        g = f.tags[0]
        if g.level not in LEVELS:
            bad_level += 1
        if g.level == "decline" and not g.reason:
            missing_reason += 1
        # 改写必须是人话（retry_alt 无输出是预期）
        rw = rewrite(g, text)
        if g.level != "retry_alt" and (not rw or "<" in rw):
            rewrite_bad += 1

    rate = n_giveup / len(rows)
    ef = easy_false / n_easy if n_easy else 0.0
    print(f"giveup_rate={rate:.2f} ({n_giveup}/{len(rows)}) | "
          f"easy_task_false={ef:.2f} ({easy_false}/{n_easy})")
    print(f"level 非法={bad_level} | decline 缺 reason={missing_reason} | "
          f"标签残留={leaked} | 改写失败={rewrite_bad}")
    ok = (n_giveup >= 3 and ef < 0.05 and bad_level == 0
          and missing_reason == 0 and leaked == 0 and rewrite_bad == 0)
    print("PASS" if ok else "FAIL")
    print("\n注：本表是协议契约评测（离线），模型真机弃权意愿见 M26。")


if __name__ == "__main__":
    main()
