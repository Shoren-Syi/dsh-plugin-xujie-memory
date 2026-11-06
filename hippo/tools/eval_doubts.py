"""M18 DoD 评测：50 轮对话，追问 ≤5 次且次次合理。

    python -m hippo.tools.eval_doubts

数据：tests/fixtures/doubts_dialog_50.jsonl。50 轮真实语感的对话，
15 处模糊信息（每处标了 doubt 候选 + 为什么该问/不该问的 note）。
全链路离线跑：filter_doubts（入库质检）→ pick_doubt（限流）→
resolve_matching_doubts（用户回答自动关闭），不调一次模型。

手册 DoD 原文：「连聊 50 轮，追问次数 ≤5 次，且每次追问都被用户
认为'问得合理'（人工评 ≥4/5）」。次数是这里算的；"合理"由每条
doubt 的 note 提供依据，最终评语落在 R-18-Gate。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..core.db import connect
from ..core.doubt import (filter_doubts, pick_doubt,
                          resolve_matching_doubts)
from ..core.migrate import migrate
from ..core.schema import DoubtOut
from ..core.store import Store

FIXTURE = Path(__file__).resolve().parent.parent.parent / "tests" / \
    "fixtures" / "doubts_dialog_50.jsonl"


def run(store: Store, rows: list[dict], *, turn_state: bool = True) -> dict:
    """逐轮喂对话，返回账目。

    turn_state=False 时不维护 turn_no（纯函数模式，测试用）。
    """
    asks: list[dict] = []
    for row in rows:
        turn = row["turn"]
        # 1) 这一轮用户留下的模糊信息 → 候选入库（走 M18.1 质检）
        d = row.get("doubt")
        if d:
            kept, st = filter_doubts(
                [DoubtOut(**d)],
                store.active_doubts("s"),
                closed_recent=store.recent_resolved_doubts("s"))
            for m in st.merged_into_existing:
                store.bump_doubt_impact(m.doubt_id, m.impact)
            for k in kept:
                store.add_doubt(session="s", question=k.question,
                                confidence=k.confidence, cost=k.cost,
                                impact=k.impact, expire_days=7)
            store.prune_doubts("s", keep=20)
        # 2) 用户回答（若有）→ 自动关闭对应疑问
        for frag in row.get("resolve") or []:
            resolve_matching_doubts(store, "s", [frag])
        # 3) 限流注入：每轮至多 1 条
        pick = pick_doubt(store, "s", turn=turn)
        if pick is not None:
            asks.append({"turn": turn, "question": pick.question,
                         "impact": pick.impact, "cost": pick.cost,
                         "asked_count": pick.asked_count})
    return {"asks": asks, "n": len(asks)}


def main() -> None:
    ap = argparse.ArgumentParser(description="M18 DoD：50 轮追问评测")
    ap.add_argument("--fixture", type=Path, default=FIXTURE)
    args = ap.parse_args()

    import tempfile
    conn = connect(tempfile.mktemp(suffix=".db"))
    migrate(conn)
    store = Store(conn)
    rows = [json.loads(line) for line in
            args.fixture.read_text(encoding="utf-8").splitlines() if line.strip()]
    res = run(store, rows)

    print(f"turns={len(rows)} candidates="
          f"{sum(1 for r in rows if r.get('doubt'))} "
          f"pool={store.counts()['doubts']} "
          f"asks={res['n']} / {len(rows)}")
    for a in res["asks"]:
        note = next((r.get("note", "") for r in rows
                     if r.get("doubt") and
                     r["doubt"]["question"] == a["question"]), "")
        print(f"  turn {a['turn']:>3} -> 「{a['question']}」")
        if note:
            print(f"            依据: {note[:60]}")


if __name__ == "__main__":
    main()
