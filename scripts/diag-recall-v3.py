# -*- coding: utf-8 -*-
"""诊断 recall-v3 的 50 题：漏在抽取、检索、还是渲染。"""
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

DB = Path(r"C:\h\stress\recall-v4.db")
LABELS = json.loads(Path(r"C:\h\stress\human-labels-50.json").read_text(encoding="utf-8"))


def main() -> int:
    cfg = Config()
    cfg.db_path = str(DB)
    diag = Counter()
    misses = []
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
            misses.append({"q": item["question"][:26], "a": item["answer"], "kind": kind,
                           "trigger": res.trigger.reason if res.trigger else "-",
                           "items": len(res.items)})
    print(json.dumps({"diag": dict(diag)}, ensure_ascii=False))
    for m in misses:
        print(f"  [{m['kind']}] {m['q']} → {m['a']}  (trigger={m['trigger']}, items={m['items']})")
    Path(r"C:\h\stress\report-recall-v4-diag.json").write_text(
        json.dumps({"diag": dict(diag), "misses": misses}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
