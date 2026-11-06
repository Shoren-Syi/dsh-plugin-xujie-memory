"""M8.3 评测：相对时间解析准确率。

手册要求 30 条 ≥95%。这里测的是"区间"不是"时间点"，
因为事件要的是"哪一段时间"，而点会丢掉"去年冬天"这种信息。

三类分开报，因为它们的失败代价完全不同：

  relative / absolute  错了就是错的（事件落到错的日子）
  season               跨年最容易错（12 月到次年 2 月）
  fuzzy                **宽到不会错**。M17.1 起有年龄锚时给十年量级区间
                       （"小时候"= 3~13 岁），无锚时向两端各放宽几年；
                       "以前""春节"这种无指代的仍然留空——不硬猜。

用法：
    python3 -m tools.eval_timeparse
    python3 -m tools.eval_timeparse --show
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from hippo.core.timeparse import parse_time

CASES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "time_hints.jsonl"
OUT = Path("reports/data/eval_timeparse.json")
TZ = "Asia/Shanghai"

# 固定"现在"，否则评测结果随运行日期漂移，昨天的 29/30 今天变成 27/30。
BASE = datetime(2026, 11, 5, 10, 0, tzinfo=ZoneInfo(TZ))

# 跨年用例单独跑：1 月说"去年冬天"必须回推到上一年 12 月
CROSS_YEAR_BASE = datetime(2026, 1, 15, 10, 0, tzinfo=ZoneInfo(TZ))


def _d(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, ZoneInfo(TZ)).strftime("%Y-%m-%d")


def run_one(case: dict, base: datetime = BASE) -> dict:
    # M17.1 起统一走 parse_time：它会先试人生阶段和事件锚，再兜底给
    # parse_span。评测如果还用 parse_span，"小时候"这类永远拿不到区间。
    sp = parse_time(case["hint"], ref=base, tz=TZ, age=case.get("age"))
    got_start = _d(sp.start) if sp else None
    got_end = _d(sp.end) if sp else None
    got_precision = sp.precision if sp else "none"
    ok = (got_start == case["start"] and got_end == case["end"]
          and got_precision == case["precision"])
    # 夹具里写了 guessed 才比对。全部比对会让 44 条旧用例全要补这个字段，
    # 而它们本来就不关心"这个时刻是不是默认 9 点"。
    if "guessed" in case and sp is not None:
        ok = ok and sp.guessed == case["guessed"]
    return {
        "hint": case["hint"], "kind": case["kind"],
        "want": [case["start"], case["end"], case["precision"]],
        "got": [got_start, got_end, got_precision],
        "guessed": sp.guessed if sp else None,
        "ok": ok, "matched": sp.matched if sp else "",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--cases", default="",
                    help="夹具路径（默认 tests/fixtures/time_hints.jsonl）")
    args = ap.parse_args()

    path = Path(args.cases) if args.cases else CASES
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    rows = [run_one(c) for c in cases]

    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)

    graded = [r for r in rows if r["kind"] != "fuzzy"]
    fuzzy = [r for r in rows if r["kind"] == "fuzzy"]
    ok = sum(1 for r in graded if r["ok"])
    fuzzy_ok = sum(1 for r in fuzzy if r["ok"])
    acc = ok / len(graded) if graded else 0.0

    # 跨年单测：1 月 15 日说"去年冬天" → 2025-12-01 ~ 2026-03-01
    cy = run_one({"hint": "去年冬天", "start": "2025-12-01", "end": "2026-03-01",
                  "precision": "season", "kind": "season"}, CROSS_YEAR_BASE)
    cy2 = run_one({"hint": "上个月", "start": "2025-12-01", "end": "2026-01-01",
                   "precision": "month", "kind": "relative"}, CROSS_YEAR_BASE)

    print(f"用例 {len(rows)}（计分 {len(graded)} + 模糊 {len(fuzzy)}）  现在={BASE:%Y-%m-%d}")
    print(f"准确 {ok}/{len(graded)} = {acc:.1%}   模糊处理正确 {fuzzy_ok}/{len(fuzzy)}")
    for k, rs in by_kind.items():
        good = sum(1 for r in rs if r["ok"])
        print(f"  {k:<10} {good}/{len(rs)}")
    print(f"跨年  去年冬天 {cy['got'][0]}~{cy['got'][1]} {'OK' if cy['ok'] else 'FAIL'}"
          f"   上个月 {cy2['got'][0]}~{cy2['got'][1]} {'OK' if cy2['ok'] else 'FAIL'}")

    bad = [r for r in rows if not r["ok"]]
    if bad:
        print("\n未命中：")
        for r in bad:
            print(f"  {r['hint']:<10} 期望 {r['want']}  实际 {r['got']}")

    if args.show:
        for r in rows:
            print(f"  {'✓' if r['ok'] else '✗'} {r['hint']:<10} {r['got']}")

    out_path = OUT if not args.cases else OUT.with_name(f"eval_timeparse_{path.stem}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "base": BASE.isoformat(), "cases": len(rows),
        "graded": len(graded), "ok": ok, "accuracy": round(acc, 4),
        "fuzzy_ok": fuzzy_ok, "fuzzy_total": len(fuzzy),
        "cross_year": [cy, cy2], "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = acc >= 0.95 and fuzzy_ok == len(fuzzy) and cy["ok"] and cy2["ok"]
    print("\n" + ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
