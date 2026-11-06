# -*- coding: utf-8 -*-
"""人工标注的真实召回测量（20 题，题与答案都是人写的，不是模型生成）。

两种口径：
1. 严格：期望原词是否出现在装配上下文里；
2. 定位：没进上下文时，看答案是"库里没有"还是"有但没被选中"还是"选中但摘要改写了"。
"""
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

DB = Path(r"C:\h\stress\humanlab.db")
LABELS = Path(r"C:\h\stress\human-labels-20.json")


def main() -> int:
    labels = json.loads(LABELS.read_text(encoding="utf-8"))
    cfg = Config()
    cfg.db_path = str(DB)
    hits, diag, rows = 0, Counter(), []
    with Store.open(DB, cfg) as store:
        for item in labels:
            ans = norm(item["answer"])
            probe = clone_store(store, cfg)
            try:
                res = pack(probe, item["session"], item["question"], cfg=cfg, use_cache=False)
            finally:
                probe.close()
            ctx = norm(res.text)
            hit = ans in ctx
            hits += int(hit)
            if not hit:
                ev = store.conn.execute(
                    "select id, raw, summary, keywords from events where session=?",
                    (item["session"],)).fetchall()
                any_id = {r["id"] for r in ev if ans in norm(
                    " ".join(str(r[x] or "") for x in ("raw", "summary", "keywords")))}
                sum_id = {r["id"] for r in ev if ans in norm(str(r["summary"] or ""))}
                sel = {it.id for it in res.items}
                if not any_id:
                    kind = "库里没有"
                elif not sum_id:
                    kind = "原文有·选中" if any_id & sel else "原文有·没选中"
                elif any_id & sel:
                    kind = "摘要有·选中未渲染"
                else:
                    kind = "摘要有·没选中"
                diag[kind] += 1
            rows.append({"q": item["question"], "a": item["answer"], "hit": hit})
    n = len(labels)
    print(json.dumps({
        "questions": n, "strict_hits": hits, "strict_rate": round(hits / n, 3),
        "miss_diagnosis": dict(diag),
        "detail": rows,
    }, ensure_ascii=False, indent=2))
    Path(r"C:\h\stress\report-human20.json").write_text(json.dumps({
        "questions": n, "strict_hits": hits, "strict_rate": round(hits / n, 3),
        "miss_diagnosis": dict(diag), "detail": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
