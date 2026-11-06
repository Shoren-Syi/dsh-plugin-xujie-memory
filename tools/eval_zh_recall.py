"""中文召回专项评测（M10.3）。

四类难例，各测一件事：

    synonym  同义      "住处" vs "居住"     —— 靠 synonyms.yaml
    abbrev   简称      "字节" vs "字节跳动"  —— 靠前缀兜底
    typo     错别字    "老汪" vs "老王"      —— 靠编辑距离（本工具量基线）
    trad     繁简      "貓" vs "猫"          —— 靠 t2s 字表

为什么非要分开报：合成一个"中文召回率 78%"，下一轮根本不知道该改哪一块。
分开之后，繁简掉到 0.6 就是在说"字表漏了"，错字掉到 0.1 就是在说
"这条路径根本没实现"——后者是**信息**，不是失败。
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from hippo.core.rank import l2_search
from hippo.core.retrieve import parse_query

DB = Path("reports/data/eval_l1.db")
CASES = Path("tests/fixtures/zh_hard_cases.jsonl")
SESSION = "main"
LABELS = {
    "synonym": "同义",
    "abbrev": "简称",
    "typo": "错别字",
    "trad": "繁简",
}


def load_cases(path: Path | None = None) -> list[dict]:
    src = path or CASES
    out = []
    for line in src.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def evaluate(store, *, limit: int = 8, path: Path | None = None) -> dict:
    by_kind: dict[str, list[int]] = defaultdict(list)
    rows = []
    for c in load_cases(path):
        q = parse_query(c["q"], SESSION, store)
        res = l2_search(store, SESSION, c["q"], query=q, limit=limit)
        ids = {it.id for it in res.items}
        ok = bool(ids & set(c["need"]))
        by_kind[c["kind"]].append(1 if ok else 0)
        rows.append({"q": c["q"], "kind": c["kind"], "ok": ok,
                     "degraded": res.degraded,
                     "top": [it.id for it in res.items]})
    stats = {k: {"n": len(v), "hit": sum(v), "rate": sum(v) / len(v)}
             for k, v in by_kind.items()}
    return {"rows": rows, "stats": stats}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--fail-under", type=float, default=0.0)
    args = ap.parse_args(argv)

    from hippo.core.store import Store

    store = Store.open(args.db)
    try:
        out = evaluate(store)
    finally:
        store.close()

    if args.verbose:
        for r in out["rows"]:
            mark = "✓" if r["ok"] else "✗"
            extra = f" 降级:{r['degraded']}" if r["degraded"] else ""
            print(f"  {mark} [{LABELS.get(r['kind'], r['kind'])}] {r['q']}{extra}")

    parts = []
    worst = 1.0
    for kind in ("synonym", "abbrev", "typo", "trad"):
        s = out["stats"].get(kind)
        if not s:
            continue
        parts.append(f"{kind}={s['rate']:.2f}")
        worst = min(worst, s["rate"])
    total = sum(s["hit"] for s in out["stats"].values())
    n = sum(s["n"] for s in out["stats"].values())
    print("  ".join(parts) + f"  (n={n}, 合计 {total}/{n})")

    if args.fail_under and worst < args.fail_under:
        print(f"FAIL：最差一类 {worst:.2f} < {args.fail_under}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
