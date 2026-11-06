# -*- coding: utf-8 -*-
"""M26.1 · 200 轮长对话语料生成器。

    python3 -m tools.gen_dialog200                # 写文件 + 统计
    python3 -m tools.gen_dialog200 --stats        # 只看统计

产出两份：

    tests/fixtures/dialog_200.jsonl       200 行，{turn, role, text, injected_facts, seg}
    tests/fixtures/dialog_200.pins.json   50 针索引（首次出现轮 / 复现次数 / 段分布）

## 为什么索引要单独存

评测要问"第 200 轮后还记不记得第 1 段说的过敏"，就必须知道那一针
第一次出现在第几轮、后面又被提过几次。挤在 jsonl 里的话，
每一行都得背一份全量定义，200 行 × 50 针，文件里 90% 是重复。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.dialog200_data import PINS, UNITS  # noqa: E402

OUT = Path("tests/fixtures/dialog_200.jsonl")
PINS_OUT = Path("tests/fixtures/dialog_200.pins.json")

TURNS_PER_UNIT = 5


def build() -> tuple[list[dict], dict]:
    """展开成 200 行 + 针索引。顺带做结构校验——数据错了就不该写文件。"""
    assert len(UNITS) == 40, f"单元数应为 40，实为 {len(UNITS)}"
    rows: list[dict] = []
    first: dict[str, int] = {}
    mentions: dict[str, list[int]] = {}

    turn = 0
    for seg, unit in UNITS:
        assert len(unit) == TURNS_PER_UNIT, f"第 {seg} 段有单元 {len(unit)} 轮"
        for role, text, pins in unit:
            turn += 1
            for pid in pins:
                assert pid in PINS, f"turn {turn} 引用了不存在的针 {pid}"
                first.setdefault(pid, turn)
                mentions.setdefault(pid, []).append(turn)
            rows.append({
                "turn": turn,
                "role": role,
                "text": text,
                "injected_facts": list(pins),
                "seg": seg,
            })

    # 每一针必须真的被说过。没说过却写进索引，评测会拿它去问，
    # 然后得到"召回率 0"，而原因根本不是系统记不住。
    never = [p for p in PINS if p not in first]
    assert not never, f"以下针从未出现在对话里：{never}"

    index = {}
    for pid, meta in PINS.items():
        index[pid] = {
            **{k: v for k, v in meta.items() if k != "seg"},
            "first_turn": first[pid],
            "mentions": mentions[pid],
            "n_mentions": len(mentions[pid]),
        }
    return rows, index


def stats(rows: list[dict], index: dict) -> dict:
    """手册四项验收的原始数字。"""
    # 跨段引用：某一针在第 N 次出现时的段号，与它首次出现的段不同。
    # 同一段内换个说法再提不算跨段——那只是重复，不是长程记忆。
    seg_of_turn = {r["turn"]: r["seg"] for r in rows}
    cross = 0
    for pid, meta in index.items():
        s0 = seg_of_turn[meta["first_turn"]]
        cross += sum(1 for t in meta["mentions"]
                     if t != meta["first_turn"] and seg_of_turn[t] != s0)

    lens = [len(r["text"]) for r in rows]
    kinds: dict[str, int] = {}
    for meta in PINS.values():
        kinds[meta["kind"]] = kinds.get(meta["kind"], 0) + 1

    return {
        "turns": len(rows),
        "facts": len(index),
        "cross_refs": cross,
        "avg_len": round(statistics.mean(lens), 1),
        "max_len": max(lens),
        "user_turns": sum(1 for r in rows if r["role"] == "user"),
        "kinds": kinds,
        "segs": {s: sum(1 for r in rows if r["seg"] == s) for s in range(1, 6)},
        "multi_pin_turns": sum(1 for r in rows if len(r["injected_facts"]) > 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 200 轮长对话语料")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--stats", action="store_true", help="只看统计，不写文件")
    args = ap.parse_args()

    rows, index = build()
    st = stats(rows, index)

    print(f"turns={st['turns']} facts={st['facts']} "
          f"cross_refs={st['cross_refs']} avg_len={st['avg_len']} chars")
    print(f"  user={st['user_turns']} assistant={st['turns'] - st['user_turns']} "
          f"多针同轮={st['multi_pin_turns']}")
    print(f"  段分布 {st['segs']}")
    print(f"  类型   {st['kinds']}")

    if args.stats:
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    pins_path = out.with_suffix(".pins.json")
    pins_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {out}  ({out.stat().st_size // 1024} KB)")
    print(f"→ {pins_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
