"""M6 三级表示评测：关键词 + 摘要，一次真机跑完。

为什么合成一个工具：关键词和摘要出自同一次抽取。分两次跑，
等于把同一批输入喂给模型两遍、花两遍钱、还引入两批不同的随机性，
最后无法判断"关键词变好了"是不是因为"这次抽的事件本来就不一样"。

四个指标（对应手册 R-6.1 / R-6.2）：

    avg_kw            平均关键词数（清洗前 / 后）
    empty_rate        模型给的空词占比（**清洗前**统计，清洗后永远是 0）
    self_recall@1     用某条自己的关键词搜，top-1 是不是它自己
    summary 长度      平均字数 / 超 70 字比例
    entity_keep       原文出现的实体，有多少留在了摘要里

用法：
    python3 -m tools.eval_m6                    # 全语料
    python3 -m tools.eval_m6 --limit 6 --no-repair
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

import httpx

from hippo.config import load_config
from hippo.core.db import connect
from hippo.core.encode import Encoder
from hippo.core.keywords import MAX_KEYWORDS, clean_keywords, is_empty_word
from hippo.core.migrate import migrate
from hippo.core.persist import persist_result
from hippo.core.store import Store
from hippo.core.summary import keep_rate, number_keep_rate, scan, too_long
from hippo.core.turns import record_pair, slice_windows
from tools.live_extract import CORPUS

OUT = Path("reports/data/eval_m6.json")
PRESCREEN_SKIP = {"chitchat", "chitchat2", "aitalk", "halfanswer", "repeat",
                  "correction", "ambiguous"}


async def run(limit: int | None, repair: bool, only: str | None = None) -> dict:
    cfg = load_config()
    cfg.deepseek.thinking = "disabled"
    cfg.extract.summary_repair = repair

    tmp = Path(tempfile.mkdtemp())
    db = tmp / "m6.db"
    conn = connect(str(db))
    migrate(conn)
    conn.close()

    corpus = CORPUS[:limit] if limit else CORPUS
    if only:
        corpus = [c for c in corpus if c[0] == only]
    for name, turns in corpus:
        for u, a in turns:
            record_pair(str(db), name, u, a)

    rows: list[dict] = []
    t_all = time.perf_counter()
    store = Store.open(db)
    async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.extract.timeout + 30)) as client:
        enc = Encoder(cfg, client, cfg.deepseek.api_key)
        for name, _ in corpus:
            pending = store.pending_turns(name)
            if not pending:
                continue
            windows = slice_windows(pending, session=name, per_window=5, max_tokens=1200)
            for w in windows:
                if not w.complete:
                    continue
                known = "（暂无）"
                out = await enc.encode(w, known=known)
                row: dict = {"window": name, "status": out.status,
                             "ms": out.latency_ms, "cost": round(out.cost, 6),
                             "cached": out.cached_tokens, "pt": out.prompt_tokens,
                             "repairs": out.repairs, "repair_kinds": out.repair_kinds,
                             "ev": 0, "kw": [], "kw_clean": [], "summary": [],
                             "raw": [], "entities": [], "issues": []}
                if out.status != "ok":
                    row["error"] = out.error
                    rows.append(row)
                    continue

                res = out.result
                for ev in res.events:
                    names = [r.name for r in ev.entities]
                    raw_kw = [str(k) for k in (ev.keywords or [])]
                    row["raw"].append(ev.raw)
                    row["kw"].append(raw_kw)
                    row["kw_clean"].append(clean_keywords(raw_kw, entities=names))
                    row["summary"].append(ev.summary)
                    row["entities"].append(names)
                row["issues"] = [(it.idx, list(it.kinds)) for it in scan(res.events)]
                row["ev"] = len(res.events)

                stats = persist_result(store, name, w, res)
                row["persisted"] = len(stats.event_ids)
                rows.append(row)
        store.close()

    return {
        "model": cfg.deepseek.extract_model,
        "repair": repair,
        "windows": len(rows),
        "wall_s": round(time.perf_counter() - t_all, 1),
        "rows": rows,
        "metrics": _metrics(rows, db),
    }


def _metrics(rows: list[dict], db: Path) -> dict:
    kw_before, kw_after, empty, total_kw = [], [], 0, 0
    sums, over70, trunc, flagged = [], 0, 0, 0
    keeps: list[float] = []
    all_sum: list[str] = []
    all_raw: list[str] = []
    for r in rows:
        for raw_kw, clean, s, raw, names in zip(
                r["kw"], r["kw_clean"], r["summary"], r["raw"], r["entities"]):
            total_kw += len(raw_kw)
            empty += sum(1 for k in raw_kw if is_empty_word(k, names))
            kw_before.append(len(raw_kw))
            kw_after.append(len(clean))
            sums.append(len(s))
            over70 += int(too_long(s))
            keeps.append(keep_rate([s], [raw], [names]))
            all_sum.append(s)
            all_raw.append(raw)
        rejected = sum(1 for i, kinds in r.get("issues", []) if kinds)
        flagged += rejected

    # self_recall：用入库后的关键词反查，top-1 是否命中自己
    recall1 = recall3 = total = 0
    misses: list[dict] = []
    with Store.open(db) as store:
        for session in store.list_sessions():
            flags = store.summary_flag_counts(session)
            trunc += flags["truncated"]
            ids = store.event_ids_of_session(session, skip_kind="raw")
            for eid in ids:
                ev = store.get_event(eid)
                kws = ev.keyword_list if ev else []
                if not kws:
                    continue
                total += 1
                hits = store.search_by_tokens(session, kws, limit=5)
                hit_ids = [h[0].id for h in hits]
                if hit_ids and hit_ids[0] == eid:
                    recall1 += 1
                elif len(misses) < 10:
                    top = store.get_event(hit_ids[0]) if hit_ids else None
                    misses.append({
                        "kw": kws,
                        "self": (ev.raw or "")[:60] if ev else "",
                        "top1": (top.raw or "")[:60] if top else "（无命中）",
                        "top1_kw": top.keyword_list if top else [],
                    })
                if eid in hit_ids[:3]:
                    recall3 += 1

    return {
        "events": len(sums),
        "kw_avg_before": round(statistics.mean(kw_before), 2) if kw_before else 0,
        "kw_avg_after": round(statistics.mean(kw_after), 2) if kw_after else 0,
        "empty_rate_before": round(empty / total_kw, 4) if total_kw else 0,
        "kw_cap": MAX_KEYWORDS,
        "self_recall@1": round(recall1 / total, 4) if total else 0,
        "self_recall@3": round(recall3 / total, 4) if total else 0,
        "recall_n": total,
        "recall_misses": misses,
        "summary_avg_len": round(statistics.mean(sums), 1) if sums else 0,
        "summary_over70": round(over70 / len(sums), 4) if sums else 0,
        "summary_entity_keep": round(statistics.mean(keeps), 4) if keeps else 0,
        # 两个口径都给：朴素的（含量词/不认换算）和修正后的。
        # 只报修正后的等于藏起"我改了指标"这件事。
        "summary_number_keep_raw": number_keep_rate(all_sum, all_raw, keep_filler=True),
        "summary_number_keep": number_keep_rate(all_sum, all_raw),
        # 入库后的真实标记（不是从内存里猜的）
        "summary_flagged": flagged,
        "summary_truncated": trunc,
        "cost": round(sum(r["cost"] for r in rows), 6),
        "repairs": sum(r.get("repairs", 0) for r in rows),
        "repair_kinds": _kind_hist(rows),
    }


def _kind_hist(rows: list[dict]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for r in rows:
        for k in (r.get("repair_kinds") or "").split(","):
            if k:
                hist[k] = hist.get(k, 0) + 1
    return dict(sorted(hist.items(), key=lambda x: -x[1]))


def replay() -> dict:
    """只重算指标，不调模型。摘要/关键词原文都在 JSON 里，够用了。

    存在的理由：改指标口径不该再花一遍钱。但**召回率算不了**——
    它要真库，所以沿用上一次跑出来的值，并在报告里标明是沿用的。
    """
    data = json.loads(OUT.read_text(encoding="utf-8"))
    rows = data["rows"]
    m = data["metrics"]
    sums = [s for r in rows for s in r["summary"]]
    raws = [w for r in rows for w in r["raw"]]
    m["summary_number_keep_raw"] = number_keep_rate(sums, raws, keep_filler=True)
    m["summary_number_keep"] = number_keep_rate(sums, raws)
    m["summary_avg_len"] = round(statistics.mean([len(x) for x in sums]), 1) if sums else 0
    data["metrics"] = m
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-repair", action="store_true")
    ap.add_argument("--only", help="只跑这个名字的窗口（针对性验证用）")
    ap.add_argument("--replay", action="store_true",
                    help="只重算指标，不调模型（省钱的调参方式）")
    args = ap.parse_args()

    data = replay() if args.replay else asyncio.run(
        run(args.limit, not args.no_repair, only=args.only))
    # --only 是针对性验证，不能覆盖全量结果——否则"跑过一次全量"这个证据就没了。
    out = OUT if not args.only else OUT.with_name(f"eval_m6_{args.only}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    m = data["metrics"]
    print(f"\n模型 {data['model']}  窗口 {data['windows']}  事件 {m['events']}  "
          f"耗时 {data['wall_s']}s  成本 ¥{m['cost']}")
    print("\n关键词")
    print(f"  平均个数        {m['kw_avg_before']} → {m['kw_avg_after']}"
          f"（上限 {m['kw_cap']}）")
    print(f"  空词占比        {m['empty_rate_before'] * 100:.1f}%（清洗前）")
    print(f"  自召回 @1       {m['self_recall@1'] * 100:.1f}%   "
          f"@3 {m['self_recall@3'] * 100:.1f}%   n={m['recall_n']}")
    print("\n摘要")
    print(f"  平均字数        {m['summary_avg_len']}")
    print(f"  超 70 字比例    {m['summary_over70'] * 100:.1f}%")
    print(f"  实体保留率      {m['summary_entity_keep'] * 100:.1f}%")
    print(f"  数字保留率      {m['summary_number_keep'] * 100:.1f}%"
          f"（朴素口径 {m['summary_number_keep_raw'] * 100:.1f}%）")
    print(f"  触发修复        {m['repairs']} 次  {m['repair_kinds']}")
    print(f"  修复后仍不合格  {m['summary_flagged']} 条")
    print(f"  截断            {m['summary_truncated']} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
