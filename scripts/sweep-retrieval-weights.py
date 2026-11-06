# -*- coding: utf-8 -*-
"""检索权重对照：同一批人工标注 20 题，换权重看严格命中怎么变。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:\h\hippo")

from hippo.config import Config  # noqa: E402
from hippo.core.pack import pack  # noqa: E402
from hippo.core.store import Store  # noqa: E402
from hippo.tools.eval_all import clone_store, norm  # noqa: E402

DB = Path(r"C:\h\stress\humanlab.db")
LABELS = json.loads(Path(r"C:\h\stress\human-labels-20.json").read_text(encoding="utf-8"))

VARIANTS = {
    "baseline": {},
    "recency+.15": {"w_recency": 0.25},
    "kw-.15/recent+.20": {"w_keyword": 0.40, "w_recency": 0.30},
    "soft_since+.15": {"w_soft_since": 0.20},
}


def measure(cfg: Config) -> tuple[int, list[str]]:
    cfg.db_path = str(DB)
    hits = 0
    missed = []
    with Store.open(DB, cfg) as store:
        for item in LABELS:
            ans = norm(item["answer"])
            probe = clone_store(store, cfg)
            try:
                text = pack(probe, item["session"], item["question"], cfg=cfg, use_cache=False).text
            finally:
                probe.close()
            if ans in norm(text):
                hits += 1
            else:
                missed.append(item["question"][:18])
    return hits, missed


def main() -> int:
    out = {}
    for name, patch in VARIANTS.items():
        cfg = Config()
        for key, value in patch.items():
            setattr(cfg.rank, key, value)
        hits, missed = measure(cfg)
        out[name] = {"hits": hits, "total": len(LABELS), "rate": round(hits / len(LABELS), 3),
                     "missed": missed}
        print(f"{name:22} {hits}/{len(LABELS)}  ({hits/len(LABELS):.0%})", flush=True)
    Path(r"C:\h\stress\report-weight-sweep.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
