"""探针：模型到底会吐出哪些 fact key / value。

M8 的手册要求"key 用固定词表（约 40 个），不在词表内一律拒绝"。
但这 40 个词不该由我拍脑袋列——M7 的教训是"先测分布再定阈值"，
key 词表同理：先看模型真实写什么，再决定哪些进词表、哪些是别名。

同时回答第二个问题：**不做事归一，会误报多少矛盾。**
`上海` / `上海市`、`字节` / `字节跳动` 在 `set_fact` 里是"值不同"，
按现在的实现直接判矛盾。这个数字决定了 M8 的验收能不能信。

用法：
    python3 -m tools.probe_facts --limit 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx

from hippo.config import load_config
from hippo.core.db import connect
from hippo.core.encode import Encoder
from hippo.core.migrate import migrate
from hippo.core.persist import persist_result
from hippo.core.store import Store
from hippo.core.turns import record_pair, slice_windows
from tools.live_extract import CORPUS


async def run(limit: int | None) -> dict:
    cfg = load_config()
    cfg.deepseek.thinking = "disabled"

    tmp = Path(tempfile.mkdtemp(prefix="hippo-facts-"))
    db = tmp / "facts.db"
    conn = connect(str(db))
    migrate(conn)
    conn.close()

    corpus = CORPUS[:limit] if limit else CORPUS
    for name, turns in corpus:
        for u, a in turns:
            record_pair(str(db), name, u, a)

    cost = 0.0
    raw_facts: list[dict] = []
    store = Store.open(db)
    async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.extract.timeout + 30)) as client:
        enc = Encoder(cfg, client, cfg.deepseek.api_key)
        for name, _ in corpus:
            for w in slice_windows(store.pending_turns(name), session=name,
                                   per_window=5, max_tokens=1200):
                if not w.complete:
                    continue
                out = await enc.encode(w, known="（暂无）")
                cost += out.cost
                if out.status != "ok":
                    continue
                for f in out.result.facts:
                    raw_facts.append({
                        "session": name, "key": f.key, "value": f.value,
                        "entity": f.entity, "confidence": f.confidence,
                    })
                persist_result(store, name, w, out.result)

    # 落库后的事实：这里能看到 persist 之后真正存下来的东西
    rows = [dict(r) for r in store.conn.execute(
        "SELECT id, session, entity_id, key, value, valid_from, valid_to, confidence "
        "FROM facts ORDER BY id").fetchall()]
    store.close()

    # 同一个 (session, entity, key) 下出现过几个不同写法 —— 假矛盾的来源
    by_slot: dict[tuple, list[str]] = defaultdict(list)
    for r in rows:
        by_slot[(r["session"], r["entity_id"], r["key"])].append(r["value"])
    variant_slots = {f"{k[2]}": v for k, v in by_slot.items() if len(v) > 1}

    report = {
        "model": cfg.deepseek.extract_model,
        "windows": len(corpus),
        "cost": round(cost, 6),
        "n_raw": len(raw_facts),
        "raw": raw_facts,
        "key_dist": dict(Counter(f["key"] for f in raw_facts).most_common()),
        "n_rows": len(rows),
        "historical": sum(1 for r in rows if r["valid_to"] is not None),
        "variant_slots": variant_slots,
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()

    rep = asyncio.run(run(args.limit))

    print(f"模型 {rep['model']}  窗口 {rep['windows']}  成本 ¥{rep['cost']:.6f}")
    print(f"模型输出 fact {rep['n_raw']} 条，落库 {rep['n_rows']} 条，"
          f"其中 historical {rep['historical']} 条")
    print("\nkey 分布：")
    for k, n in rep["key_dist"].items():
        print(f"  {k:<16} {n}")
    print("\n原始 fact：")
    for f in rep["raw"]:
        print(f"  [{f['session']}] {f['entity']} | {f['key']} = {f['value']}"
              f"  (conf {f['confidence']})")
    if rep["variant_slots"]:
        print("\n同一槽位出现过多个写法（不做归一会误判成矛盾）：")
        for k, v in rep["variant_slots"].items():
            print(f"  {k}: {v}")
    else:
        print("\n同一槽位没有出现多写法。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
