# -*- coding: utf-8 -*-
"""194 题诊断：漏在抽取、检索还是渲染。"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, r"C:\h\hippo")

from hippo.config import Config  # noqa: E402
from hippo.core.pack import pack  # noqa: E402
from hippo.core.store import Store  # noqa: E402
from hippo.tools.eval_all import clone_store, norm  # noqa: E402

STRESS = Path(r"C:\h\stress")
DB = STRESS / "labels200.db"
LABELS = json.loads((STRESS / "human-labels-200.json").read_text(encoding="utf-8"))


def main() -> int:
    cfg = Config()
    cfg.db_path = str(DB)
    diag = Counter()
    samples = {}
    with Store.open(DB, cfg) as store:
        for item in LABELS:
            ans = norm(item["answer"])
            probe = clone_store(store, cfg)
            try:
                res = pack(probe, item["session"], item["question"], cfg=cfg, use_cache=False)
            finally:
                probe.close()
            if ans in norm(res.text):
                diag["hit"] += 1
                continue
            rows = store.conn.execute(
                "select id, raw, summary, keywords from events where session=?",
                (item["session"],)).fetchall()
            any_id = {r["id"] for r in rows if ans in norm(
                " ".join(str(r[x] or "") for x in ("raw", "summary", "keywords")))}
            sum_id = {r["id"] for r in rows if ans in norm(str(r["summary"] or ""))}
            sel = {it.id for it in res.items}
            if not any_id:
                kind = "库里没有"
            elif sum_id and (sum_id & sel):
                kind = "摘要有·选中未渲染"
            elif sum_id:
                kind = "摘要有·没选中"
            elif any_id & sel:
                kind = "原文有·选中"
            else:
                kind = "原文有·没选中"
            diag[kind] += 1
            samples.setdefault(kind, []).append(
                {"q": item["question"][:28], "a": item["answer"], "session": item["session"]})
    print(json.dumps(dict(diag), ensure_ascii=False))
    for kind, items in samples.items():
        print(f"--- {kind} ---")
        for it in items[:4]:
            print(f"  [{it['session']}] {it['q']} → {it['a']}")
    (STRESS / "report-labels200-diag.json").write_text(
        json.dumps({"diag": dict(diag), "samples": samples}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
