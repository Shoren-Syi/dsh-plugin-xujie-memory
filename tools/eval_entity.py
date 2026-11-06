"""实体归一评测（M7.1）。

跑的是一组"应该合并"和"绝不能合并"的用例，但**这些用例是我自己编的**。
所以这个数字只能说明"规则没有明显破洞"，不能说明线上准确率——
真实对话里"老王"可能是同事、岳父、或一只狗。

因此脚本同时打印 **每一个误判的原文**，让人能判断是规则错了还是用例错了。
只看 accuracy 这一栏就下结论，是 M4/M5.5 已经犯过两次的错。

每个用例**独立建库**。第一版让所有用例共用一个实体表，结果 w1 的"老王"
和 n4 的"老王"被算成"跨用例串号"——同一个字符串本来就该是一个实体，
那不是 bug。用例之间共享状态只会让指标变成噪声。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hippo.core.entities import decide, normalize_name, similarity

DEFAULT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "entity_cases.jsonl"


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_one(case: dict, *, verbose: bool = False) -> tuple[bool, list[str], int]:
    table: dict[int, dict] = {}
    next_id = 1
    ids: list[int] = []
    log: list[str] = []

    for i, m in enumerate(case["mentions"]):
        existing = [(eid, e["canonical"], e["type"], e["aliases"]) for eid, e in table.items()]
        mt = decide(m["name"], m.get("type", "person"), existing, aliases=m.get("aliases"))
        if mt.action == "merge" and mt.target_id is not None:
            eid = mt.target_id
            table[eid]["aliases"] = sorted(
                set(table[eid]["aliases"]) | set(m.get("aliases") or [])
            )
        else:
            eid = next_id
            next_id += 1
            table[eid] = {
                "canonical": normalize_name(m["name"]),
                "type": m.get("type", "person"),
                "aliases": list(m.get("aliases") or []),
            }
        ids.append(eid)
        log.append(f"  {case['id']}.{i} {m['name']:<8} → #{eid} [{mt.action}] {mt.reason} {mt.score:.2f}")

    want_same = case["same"]
    got_same = len(set(ids)) == 1
    ok = got_same == want_same
    if verbose:
        print("\n".join(log))
    return ok, log, len(table)


def run(cases: list[dict], *, show_sim: bool = False) -> int:
    hard = [c for c in cases if c.get("hard")]
    easy = [c for c in cases if not c.get("hard")]
    ok = 0
    bad: list[str] = []
    splits = 0
    for case in cases:
        good, _, n_ent = run_one(case, verbose=show_sim)
        if good:
            ok += 1
        elif not case.get("hard"):
            bad.append(f"  {case['id']} 期望 same={case['same']}  ({case.get('note', '')})")
            print(f"\n[FAIL] {case['id']}")
            run_one(case, verbose=True)
        else:
            print(f"[已知难例·未通过] {case['id']}  {case.get('note', '')}")
        if case["same"] and n_ent > 1:
            splits += n_ent - 1

    n = len(cases)
    ne = len(easy)
    he = sum(1 for c in hard if run_one(c)[0])
    print(f"\n用例 {n}（规则用例 {ne} + 已知难例 {len(hard)}）")
    print(f"规则用例准确 {ne - len(bad)}/{ne} = {(ne - len(bad)) / ne:.1%}   "
          f"难例通过 {he}/{len(hard)}   过度拆分 {splits} 个多余实体")
    if bad:
        print("未达预期：")
        print("\n".join(bad))

    if show_sim:
        # 相似度分布：确认 0.8 阈值到底还能不能触发
        names = sorted({normalize_name(m["name"]) for c in cases for m in c["mentions"]})
        pairs = []
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                pairs.append((similarity(a, b), a, b))
        pairs.sort(reverse=True)
        print("\n最相似的 10 对（全库口径，含不同人）：")
        for s, a, b in pairs[:10]:
            print(f"  {s:.2f}  {a} / {b}")
        print(f"  ≥0.8 的对数 = {sum(1 for s, _, _ in pairs if s >= 0.8)} / {len(pairs)}")
    return 0 if not bad else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=Path, default=DEFAULT)
    ap.add_argument("--show-sim", action="store_true")
    a = ap.parse_args()
    return run(load(a.cases), show_sim=a.show_sim)


if __name__ == "__main__":
    raise SystemExit(main())
