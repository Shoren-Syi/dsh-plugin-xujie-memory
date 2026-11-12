# -*- coding: utf-8 -*-
"""只测量不重建：在已有的 ab-raw.db 上比较三种配置的严格命中。

off      : 只查事件（旧行为）
terms    : 关键词捞原话
terms+recent : 关键词 + 最近两条原话
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, r"C:\h\hippo")

from hippo.config import Config  # noqa: E402
from hippo.core.pack import pack  # noqa: E402
from hippo.core.store import Store  # noqa: E402
from hippo.tools.eval_all import norm  # noqa: E402

STRESS = Path(r"C:\h\stress")
CORPUS = STRESS / "real-corpus.jsonl"
DB = STRESS / "ab-raw.db"
LABELS = json.loads((STRESS / "human-labels-200.json").read_text(encoding="utf-8"))


def load_corpus() -> dict[str, list[tuple[str, str]]]:
    by: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for line in CORPUS.open(encoding="utf-8"):
        line = line.strip()
        if line:
            row = json.loads(line)
            by[row["session"]].append((row["role"], row["text"]))
    return dict(by)


def map_labels(by) -> list[dict]:
    out = []
    for item in LABELS:
        if not item["session"].startswith("real-"):
            continue
        ans = norm(item["answer"])
        for sid, turns in by.items():
            if any(role == "user" and ans in norm(text) for role, text in turns):
                out.append({**item, "new_session": sid})
                break
    return out


def main() -> int:
    mapped = map_labels(load_corpus())
    cfg = Config()
    cfg.db_path = str(DB)
    results = {}
    rows = []
    with Store.open(DB, cfg) as store:
        for mode, (raw, recent) in {
            "off": (False, False),
            "terms": (True, False),
            "terms+recent": (True, True),
        }.items():
            cfg.retrieve.raw_turns = raw
            cfg.retrieve.raw_recent = recent
            hit = 0
            for item in mapped:
                text = pack(store, item["new_session"], item["question"], cfg=cfg, use_cache=False).text
                if norm(item["answer"]) in norm(text):
                    hit += 1
            results[mode] = hit
            print(mode, hit, "/", len(mapped), flush=True)
    print(json.dumps({"total": len(mapped), **results}, ensure_ascii=False))
    (STRESS / "report-ab-raw2.json").write_text(
        json.dumps({"total": len(mapped), **results}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
