# -*- coding: utf-8 -*-
"""M26.1 · 语料体检。

    python -m hippo.tools.check_fixture tests/fixtures/dialog_200.jsonl

查四件事，任何一件不过就退出码 1：

1. 结构：200 轮连续、无空洞、role 只有 user/assistant
2. 针：50 条齐全、每条都有问句和答案、都真的在对话里说过
3. 事实 key：15 条 fact 针必须落在 `fact_keys.yaml` 的闭集里
4. 跨段引用：≥20 处

第 3 条最容易漏，也最致命。`set_fact` 对表外 key 是**静默拒收**
（只写一条 info 日志，返回值里 action=reject）。如果语料里写了
`insurance` 这种词表里没有的 key，跑完 200 轮你会看到"这条事实
召回率 0"，然后去查检索、查 pack、查权重，查一整天——而它压根
没进过库。所以语料体检必须自己先查一遍。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hippo.core.store import fact_keys  # noqa: E402

MIN_CROSS = 20
EXPECT_TURNS = 200
EXPECT_FACTS = 50
EXPECT_KINDS = {"fact": 15, "rel": 12, "pref": 10, "plan": 8, "seed": 5}


def load(jsonl: Path) -> tuple[list[dict], dict]:
    rows = [json.loads(line) for line in
            jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    pins_path = jsonl.with_suffix(".pins.json")
    if not pins_path.exists():
        raise SystemExit(f"缺针索引：{pins_path}（先跑 tools.gen_dialog200）")
    pins = json.loads(pins_path.read_text(encoding="utf-8"))
    return rows, pins


def check(rows: list[dict], pins: dict) -> tuple[dict, list[str]]:
    errs: list[str] = []

    # ---- 1. 结构
    turns = [r["turn"] for r in rows]
    if turns != list(range(1, len(rows) + 1)):
        errs.append("turn 不连续或有空洞")
    if len(rows) != EXPECT_TURNS:
        errs.append(f"轮数 {len(rows)} ≠ {EXPECT_TURNS}")
    bad_role = {r["role"] for r in rows} - {"user", "assistant"}
    if bad_role:
        errs.append(f"未知 role：{bad_role}")

    # ---- 2. 针
    if len(pins) != EXPECT_FACTS:
        errs.append(f"针数 {len(pins)} ≠ {EXPECT_FACTS}")
    for pid, meta in pins.items():
        if not meta.get("q") or not meta.get("a"):
            errs.append(f"{pid} 缺问句或答案")
        if not meta.get("mentions"):
            errs.append(f"{pid} 从未在对话里出现")

    # ---- 3. fact key 闭集
    table = fact_keys()
    for pid, meta in pins.items():
        if meta["kind"] != "fact":
            continue
        if table.canonical(meta["key"]) is None:
            errs.append(f"{pid} 的 key `{meta['key']}` 不在 fact_keys 词表里"
                        "（set_fact 会静默拒收）")

    # ---- 4. 跨段
    seg_of = {r["turn"]: r["seg"] for r in rows}
    cross = 0
    for meta in pins.values():
        s0 = seg_of[meta["first_turn"]]
        cross += sum(1 for t in meta["mentions"]
                     if t != meta["first_turn"] and seg_of[t] != s0)
    if cross < MIN_CROSS:
        errs.append(f"跨段引用 {cross} < {MIN_CROSS}")

    lens = [len(r["text"]) for r in rows]
    ulens = [len(r["text"]) for r in rows if r["role"] == "user"]
    kinds: dict[str, int] = {}
    for meta in pins.values():
        kinds[meta["kind"]] = kinds.get(meta["kind"], 0) + 1
    if kinds != EXPECT_KINDS:
        errs.append(f"针类型分布 {kinds} ≠ {EXPECT_KINDS}")

    st = {
        "turns": len(rows),
        "facts": len(pins),
        "cross_refs": cross,
        "avg_len": round(statistics.mean(lens), 1),
        "user_avg_len": round(statistics.mean(ulens), 1),
        "segs": {s: sum(1 for r in rows if r["seg"] == s) for s in range(1, 6)},
        "kinds": kinds,
        "first_seg_turns": sum(1 for m in pins.values() if m["first_turn"] <= 80),
    }
    return st, errs


def main() -> int:
    ap = argparse.ArgumentParser(description="200 轮语料体检")
    ap.add_argument("path", nargs="?", default="tests/fixtures/dialog_200.jsonl")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rows, pins = load(Path(args.path))
    st, errs = check(rows, pins)

    print(f"turns={st['turns']} facts={st['facts']} "
          f"cross_refs={st['cross_refs']} avg_len={st['avg_len']} chars")
    if not args.quiet:
        print(f"  用户轮均长 {st['user_avg_len']} 字（总均长被 80 条简短回复拉低）")
        print(f"  段分布 {st['segs']}")
        print(f"  类型   {st['kinds']}")
        print(f"  前两段就埋下的针 {st['first_seg_turns']} / {st['facts']}")

    if errs:
        print("\n未通过：", file=sys.stderr)
        for e in errs:
            print(f"  · {e}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
