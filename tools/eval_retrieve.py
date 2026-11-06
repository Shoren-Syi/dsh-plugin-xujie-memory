"""L2 检索评测（M10.2）。

读 `tests/fixtures/retrieve_queries.jsonl`，跑 `l2_search`，算三个数：

    hit@8   第一标注项落在前 8 条里的比例（手册 DoD：≥85%）
    mrr     第一条命中的倒数排名，衡量"排得够不够前"
    tokens  平均装填量，用来核对预算没被吃满

## 标注为什么用事件 id 而不是文本

`need` 是事件 id 列表（真机库 `reports/data/eval_l1.db`）。用文本做 needle
会被摘要的措辞绑架："换工作" 和 "辞职" 在两条不同事件里，用文本匹配
就得先决定它们算不算同一个答案——那是语义判断，评测工具不该做。

代价是库必须固定。`--rebuild` 会重新抽一遍（花钱且 id 会变），此时
fixture 失效，工具会直接报错而不是给出一个假分数。

## --naive 对照组

手册说"命中率 ≥85%"。但这句话单独看没有意义——一个"按 weight 取前 8"
的实现也可能有 30~40%。所以这个工具带 `--naive`：同样的查询，用
L1 候选 + weight 排序，不做关键词。两个数的差才是 L2 的贡献。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from hippo.core.rank import l2_search
from hippo.core.retrieve import l1_candidates, parse_query

DB = Path("reports/data/eval_l1.db")
QUERIES = Path("tests/fixtures/retrieve_queries.jsonl")
# 留出集：写完调过词表之后**只跑一次**的那批查询。
# 它存在的意义就是把"调过的"和"没调过的"分开报——
# 只看前者，任何在测试集上手调的实现都能到 0.95。
HOLDOUT = Path("tests/fixtures/retrieve_holdout.jsonl")
SESSION = "main"


def load_queries(path: Path | None = None) -> list[dict]:
    out = []
    src = path or QUERIES
    for line in src.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _rank_of(ids: list[int], need: list[int]) -> int:
    """第一条命中的位置（1 起）。0 = 没命中。"""
    want = set(need)
    for i, eid in enumerate(ids, 1):
        if eid in want:
            return i
    return 0


def evaluate(store, *, naive: bool = False, limit: int = 8,
             path: Path | None = None) -> dict:
    rows = []
    total_events = store.conn.execute(
        "SELECT COUNT(*) c FROM events").fetchone()["c"]
    for item in load_queries(path):
        q = item["q"]
        qq = parse_query(q, SESSION, store)
        if naive:
            l1 = l1_candidates(store, SESSION, qq)
            evs = store.top_events(SESSION, l1.event_ids, limit=limit,
                                   include_historical=qq.include_historical)
            ids = [e.id for e in evs]
            tokens = sum(len(e.summary) for e in evs)
            res = None
        else:
            res = l2_search(store, SESSION, q, query=qq, limit=limit)
            ids = [it.id for it in res.items]
            tokens = res.total_tokens
        rows.append({
            "q": q, "need": item["need"], "ids": ids,
            "rank": _rank_of(ids, item["need"]),
            "tokens": tokens,
            "degraded": (res.degraded if res else ""),
            "kw_hit": (res.matched if res else 0),
            "cand": (res.candidates if res else 0),
        })

    n = len(rows)
    hit = sum(1 for r in rows if r["rank"] and r["rank"] <= limit)
    mrr = sum(1.0 / r["rank"] for r in rows if r["rank"]) / n
    return {
        "n": n, "hit": hit, "hit_rate": hit / n, "mrr": mrr,
        "tokens": int(statistics.mean(r["tokens"] for r in rows)),
        "rows": rows, "total_events": total_events,
        "degraded": sum(1 for r in rows if r["degraded"]),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--naive", action="store_true", help="对照组：只按 weight 排")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--holdout", action="store_true",
                    help="跑留出集（只跑一次，调词表时不许看）")
    ap.add_argument("--fail-under", type=float, default=0.0,
                    help="命中率低于此值时返回非零（CI 用）")
    args = ap.parse_args(argv)

    from hippo.core.store import Store

    store = Store.open(args.db)
    try:
        out = evaluate(store, naive=args.naive, limit=args.limit,
                       path=HOLDOUT if args.holdout else None)
    finally:
        store.close()

    tag = ("留出集" if args.holdout else "标注集")
    tag += " / 对照组(只按weight)" if args.naive else " / L2"
    print(f"{tag}：{out['n']} 条查询，库 {out['total_events']} 条事件")
    if args.verbose:
        for r in out["rows"]:
            mark = "✓" if r["rank"] and r["rank"] <= args.limit else "✗"
            extra = f" 降级:{r['degraded']}" if r["degraded"] else ""
            print(f"  {mark} 名次{r['rank'] or '-':>2} kw={r['kw_hit']:<2} "
                  f"候选{r['cand']:>3} [{r['q']}]{extra}")
    print(f"hit@{args.limit}={out['hit_rate']:.2f}  mrr={out['mrr']:.2f}  "
          f"avg_tokens={out['tokens']}  降级 {out['degraded']}/{out['n']}")

    if args.fail_under and out["hit_rate"] < args.fail_under:
        print(f"FAIL：命中率 {out['hit_rate']:.2f} < {args.fail_under}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
