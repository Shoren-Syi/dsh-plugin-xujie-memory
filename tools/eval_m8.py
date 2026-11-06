"""M8 真机验证：模型会不会按词表给 key、会不会把值写成句子。

M8.1 的验收在单测里是"key 表外一律拒绝"。这条在测试里很容易过——
因为测试是我喂的假数据。真机上要回答的是另外两个问题：

1. **模型真的会用词表吗**，还是继续写 `居住城市` / `房贷剩余年限`？
   如果它大面积越界，说明 prompt 里的词表没人看，得换约束方式。
2. **值是值还是句子**？"吃不了辣（微辣可以，中辣不行）、不喝冰的"
   这种值无法参与比较，矛盾检测对它完全失效——而它不会报错，
   只会让 M8.2 的统计一直是 0 条矛盾。

用法：
    python3 -m tools.eval_m8 --limit 14
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
import time
from collections import Counter
from pathlib import Path

import httpx

from hippo.config import load_config
from hippo.core.db import connect
from hippo.core.encode import Encoder
from hippo.core.facts import load_keys
from hippo.core.migrate import migrate
from hippo.core.persist import persist_result
from hippo.core.store import Store
from hippo.core.turns import record_pair, slice_windows
from tools.live_extract import CORPUS

OUT = Path("reports/data/eval_m8.json")

# 句子级值的判据：超过这个长度基本就是在写句子，不是在写值。
VALUE_LEN_LIMIT = 12


async def run(limit: int | None) -> dict:
    cfg = load_config()
    cfg.deepseek.thinking = "disabled"

    tmp = Path(tempfile.mkdtemp(prefix="hippo-m8-"))
    db = tmp / "m8.db"
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
    t0 = time.perf_counter()
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
                    raw_facts.append({"session": name, "key": f.key,
                                      "value": f.value, "entity": f.entity,
                                      "conf": f.confidence})
                persist_result(store, name, w, out.result)
    wall = time.perf_counter() - t0

    table = load_keys()
    rows = [dict(r) for r in store.conn.execute(
        "SELECT key, key_raw, value, card, valid_from, valid_to, confidence "
        "FROM facts ORDER BY id").fetchall()]

    # 表外 key：只有在 persist 之前统计得到（写库时已被拒绝）
    unknown = Counter(f["key"] for f in raw_facts if table.canonical(f["key"]) is None)
    mapped = Counter(table.canonical(f["key"]) or "<拒>" for f in raw_facts)

    active = [r for r in rows if r["valid_to"] is None]
    hist = [r for r in rows if r["valid_to"] is not None]
    long_values = [r for r in active if len(r["value"]) > VALUE_LEN_LIMIT]
    sentence_like = [r for r in active
                     if re.search(r"[，。；、]", r["value"]) or len(r["value"]) > 20]

    # 同一 (session, entity, key) 是否还有多个 active（single 槽应当没有）
    dup: dict[str, int] = {}
    for r in store.conn.execute(
            "SELECT session, entity_id, key, count(*) n FROM facts "
            "WHERE valid_to IS NULL AND card = 'single' "
            "GROUP BY session, entity_id, key HAVING n > 1").fetchall():
        dup[f"{r['key']}@{r['entity_id']}"] = r["n"]

    report = {
        "model": cfg.deepseek.extract_model,
        "windows": len(corpus),
        "wall_s": round(wall, 1),
        "cost": round(cost, 6),
        "n_raw": len(raw_facts),
        "unknown_keys": dict(unknown),
        "key_mapped": dict(mapped),
        "raw": raw_facts,
        "rows": len(rows),
        "active": len(active),
        "historical": len(hist),
        "value_len_avg": round(sum(len(r["value"]) for r in active) / len(active), 1)
                          if active else 0,
        "long_values": [{"key": r["key"], "value": r["value"]}
                        for r in long_values][:12],
        "sentence_like": [{"key": r["key"], "value": r["value"]}
                          for r in sentence_like][:12],
        "duplicate_active": dup,
        "card_dist": dict(Counter(r["card"] for r in rows)),
    }
    store.close()
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=14)
    args = ap.parse_args()

    rep = asyncio.run(run(args.limit))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"模型 {rep['model']}  窗口 {rep['windows']}  耗时 {rep['wall_s']}s  "
          f"成本 ¥{rep['cost']:.6f}")
    print(f"模型输出 fact {rep['n_raw']} 条   落库 {rep['rows']} 条"
          f"（active {rep['active']} / historical {rep['historical']}）")
    print(f"key 分布 {rep['key_mapped']}")
    print(f"表外 key：{rep['unknown_keys'] or '无'}")
    print(f"平均值长 {rep['value_len_avg']} 字   超 {VALUE_LEN_LIMIT} 字 "
          f"{len(rep['long_values'])} 条   句子级 {len(rep['sentence_like'])} 条")
    print(f"重复 active：{rep['duplicate_active'] or '无'}")
    if rep["long_values"]:
        print("\n长值样本：")
        for r in rep["long_values"]:
            print(f"  {r['key']}: {r['value']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
