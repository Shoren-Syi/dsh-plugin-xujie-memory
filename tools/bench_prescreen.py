"""预筛基准（M4.3）。

    python -m tools.bench_prescreen tests/fixtures/prescreen_labeled.jsonl --split dev
    python -m tools.bench_prescreen tests/fixtures/prescreen_labeled.jsonl --split holdout

输出混淆矩阵、P/R、放行率、各分支贡献、误判清单。

为什么要分 split
----------------
样本是我自己造的。用它调参再报同一批样本的成绩，等于自己出题自己判卷。
所以 dev 用来调，holdout 只在最后跑一次。命令行必须显式指定 split，
不给默认值——默认值会让人"顺手"看 holdout，那它就废了。
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hippo.core.prescreen import EntityIndex, load_rules, prescreen  # noqa: E402


def _entities(spec: str) -> EntityIndex | None:
    """实体词表：命令行给了就用命令行的，否则读 fixture 里的默认表。

    基准必须带实体表，否则标注为 entity 类的样本对 M4.1 不公平——
    它们是靠 M4.2 命中的，不带表当然测不出来。
    """
    words: list[str] = []
    if spec:
        words = [w for w in spec.split(",") if w.strip()]
    else:
        default = ROOT / "tests/fixtures/prescreen_entities.txt"
        if default.exists():
            words = [w.strip() for w in default.read_text(encoding="utf-8").splitlines()
                     if w.strip()]
    return EntityIndex([(w, i) for i, w in enumerate(words)]) if words else None


def load(path: Path, split: str) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if split == "all" or r.get("split") == split:
            rows.append(r)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="预筛基准")
    ap.add_argument("path", nargs="?", default="tests/fixtures/prescreen_labeled.jsonl")
    ap.add_argument("--split", choices=["dev", "holdout", "test", "blind", "all"], required=True)
    ap.add_argument("--entities", default="", help="实体词表，逗号分隔（可选）")
    ap.add_argument("--show", type=int, default=8, help="列多少条误判")
    ap.add_argument("--skip-tag", default="", help="跳过带该 tag 的样本（复现中间态用）")
    ap.add_argument("--window", type=int, default=0,
                    help="按 N 条一窗算窗口级召回（真实生效的指标）")
    args = ap.parse_args(argv)

    rows = load(ROOT / args.path if not Path(args.path).is_absolute() else Path(args.path),
                args.split)
    if args.skip_tag:
        rows = [r for r in rows if r.get("tag") != args.skip_tag]
    if not rows:
        print(f"没有 split={args.split} 的样本", file=sys.stderr)
        return 1

    rules = load_rules()
    ents = _entities(args.entities)

    verdict: dict[str, bool] = {}
    tp = fp = fn = tn = 0
    reasons: collections.Counter[str] = collections.Counter()
    fp_rows: list[tuple[str, str]] = []
    fn_rows: list[tuple[str, str]] = []
    times: list[float] = []

    for r in rows:
        t0 = time.perf_counter()
        res = prescreen(r["text"], entities=ents, rules=rules)
        times.append((time.perf_counter() - t0) * 1000)

        want = bool(r["should_extract"])
        got = res.passed
        verdict[r["text"]] = got
        reasons[res.reason] += 1
        if want and got:
            tp += 1
        elif want and not got:
            fn += 1
            fn_rows.append((r["text"], f"{r['why']} → {res.reason}"))
        elif not want and got:
            fp += 1
            fp_rows.append((r["text"], f"{r['why']} → {res.reason}:{res.matched[0] if res.matched else ''}"))
        else:
            tn += 1

    n = len(rows)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    pass_rate = (tp + fp) / n
    avg_ms = statistics.mean(times)
    p95_ms = sorted(times)[int(n * 0.95) - 1]

    print(f"split={args.split}  n={n}")
    print()
    print("                 pred_T  pred_F")
    print(f"  actual_T    {tp:8d}{fn:8d}     (recall    {recall:.3f})")
    print(f"  actual_F    {fp:8d}{tn:8d}     (precision {precision:.3f})")
    print()
    print(f"pass_rate = {pass_rate:.3f}   precision = {precision:.3f}   recall = {recall:.3f}")
    print(f"latency   avg={avg_ms:.3f}ms  p95={p95_ms:.3f}ms")

    print("\n各分支命中分布")
    for reason, cnt in reasons.most_common():
        print(f"  {reason:<12} {cnt:4d}  {cnt / n * 100:5.1f}%")

    # 窗口级：M5 是一个窗口送一次抽取器，只要窗内任一条放行，整窗都进。
    # 所以"句级漏一条"不等于"信息丢了"——除非整窗一条都没放行。
    if args.window > 0:
        w = args.window
        # 必须先打乱再切窗。语料是按正/负分块写的，直接切会让每个窗口
        # 全是正样本，窗口级召回必然虚高到 1.000 —— 那是个假的漂亮数字。
        # 固定种子保证可复现。
        import random
        shuffled = list(rows)
        random.Random(20261104).shuffle(shuffled)
        wins = [shuffled[i: i + w] for i in range(0, len(shuffled), w)]
        wtp = wfn = 0
        for win in wins:
            if not any(r["should_extract"] for r in win):
                continue
            if any(verdict[r["text"]] for r in win):
                wtp += 1
            else:
                wfn += 1
        wr = wtp / (wtp + wfn) if wtp + wfn else 0.0
        print(f"\n窗口级（{w} 条/窗，{len(wins)} 窗，{wtp + wfn} 窗含正样本）"
              f"  recall = {wr:.3f}   ({wtp} 中 {wfn} 漏)")

    if args.show:
        if fp_rows:
            print(f"\n误报（挡不住，白花钱）{len(fp_rows)} 条，前 {args.show}：")
            for text, why in fp_rows[: args.show]:
                print(f"  ✗ {text[:36]:<38} {why}")
        if fn_rows:
            print(f"\n漏报（放走了，信息永久丢失）{len(fn_rows)} 条，前 {args.show}：")
            for text, why in fn_rows[: args.show]:
                print(f"  ✗ {text[:36]:<38} {why}")

    return 0 if precision >= 0.85 and recall >= 0.90 else 2


if __name__ == "__main__":
    raise SystemExit(main())
