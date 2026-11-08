# -*- coding: utf-8 -*-
"""为 200 题人工标注准备素材：挑 40 个中长会话，分批导出用户原话。

用法：python scripts/label-prep.py [批次]
批次 = 0..3，每批 10 个会话，导出到 C:\h\stress\label-batch-<n>.txt
"""
from __future__ import annotations

import collections
import io
import json
import sys
from pathlib import Path

CORPUS = Path(r"C:\h\stress\real-corpus.jsonl")
OUT = Path(r"C:\h\stress")


def main() -> int:
    batch = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    rows = [json.loads(l) for l in io.open(CORPUS, encoding="utf-8") if l.strip()]
    by = collections.defaultdict(list)
    for r in rows:
        by[r["session"]].append(r)
    # 中长会话：30~60 条消息，用户消息至少 8 条
    pool = []
    for sid, turns in by.items():
        users = [t for t in turns if t["role"] == "user" and 12 <= len(t["text"]) <= 400]
        if len(turns) >= 20 and len(users) >= 8:
            pool.append((sid, turns, users))
    pool.sort(key=lambda x: x[0])
    pick = pool[batch * 10:(batch + 1) * 10]
    if not pick:
        print(f"批次 {batch} 没有会话（总数 {len(pool)}）")
        return 0
    out = []
    for sid, _turns, users in pick:
        out.append("=" * 60)
        out.append(f"session: {sid}")
        for i, t in enumerate(users[:14], 1):
            text = " ".join(t["text"].split())[:150]
            out.append(f"U{i}: {text}")
        out.append("")
    path = OUT / f"label-batch-{batch}.txt"
    io.open(path, "w", encoding="utf-8").write("\n".join(out))
    print(f"批次 {batch}: {len(pick)} 个会话 -> {path}；池子里共 {len(pool)} 个可用会话")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
