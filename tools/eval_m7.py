"""M7 真机验证：实体类型、槽位、角色卡规模。

要回答三个问题：

1. **地名还会不会被当成一个人。**
   M5 时 `persist.py` 里写死了 `type="person"`，模型给的 type 被整列丢掉。
   于是"朝阳""海淀"进了角色卡候选池，三个月后 AI 会问"朝阳最近怎么样"。
   这个 bug 不会报错，只会让卡慢慢长歪，所以必须用真机数据看类型分布。

2. **模型会不会用 slot。**
   槽位是 M7.2 矛盾检测的唯一依据。模型不给，冲突就检测不出来；
   模型乱给（"在哪工作""工作"），同一件事会被拆成两个槽，同样失效。

3. **一张卡到底多大。**
   traits 每窗口几条，决定 M9 注入时的 token 预算。

用法：
    python3 -m tools.eval_m7 --limit 14
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from collections import Counter
from pathlib import Path

import httpx

from hippo.config import load_config
from hippo.core.db import connect
from hippo.core.encode import Encoder
from hippo.core.migrate import migrate
from hippo.core.store import Store
from hippo.core.turns import record_pair, slice_windows
from tools.live_extract import CORPUS

OUT = Path("reports/data/eval_m7.json")

# 语料里出现的真实地名。它们**绝对不能**成为 person。
PLACES = {"朝阳", "海淀", "深圳", "上海", "望京", "陆家嘴"}


async def run(limit: int | None) -> dict:
    cfg = load_config()
    cfg.deepseek.thinking = "disabled"

    tmp = Path(tempfile.mkdtemp(prefix="hippo-m7-"))
    db = tmp / "m7.db"
    conn = connect(str(db))
    migrate(conn)
    conn.close()

    corpus = CORPUS[:limit] if limit else CORPUS
    for name, turns in corpus:
        for u, a in turns:
            record_pair(str(db), name, u, a)

    cost = 0.0
    t0 = time.perf_counter()
    store = Store.open(db)
    failures: list[dict] = []
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
                    failures.append({"window": name, "error": out.error})
                    continue
                store_result = out.result
                from hippo.core.persist import persist_result
                persist_result(store, name, w, store_result)

            # 同一个会话里，名字写法合并了多少
    store.close()
    wall = time.perf_counter() - t0

    # ---------------------------------------------------------- 汇总
    c = connect(str(db))
    st = Store(c)
    sessions = [s for s, in c.execute("SELECT DISTINCT session FROM entities").fetchall()]

    ents = []
    for s in sessions:
        ents.extend(st.list_entities(s))
    place_errors = [e.canonical for e in ents
                    if e.type == "person" and e.canonical in PLACES]

    traits_all = []
    for e in ents:
        traits_all.extend(st.list_traits(e.id, active_only=False))
    active = [t for t in traits_all if t.valid_to is None]

    slot_dist = Counter((t.slot or "<empty>") for t in active)
    inferred = [t for t in active if t.status == "inferred"]
    with_ev = [t for t in active if t.evidence]

    # 同一会话内"名字出现次数 vs 实体数"——差值就是合并掉的水分
    names_seen = c.execute(
        "SELECT session, count(*) FROM ("
        "  SELECT DISTINCT e.session, ee.entity_id FROM events e "
        "  JOIN event_entities ee ON ee.event_id = e.id)"
        " GROUP BY session").fetchall()

    report = {
        "model": cfg.deepseek.extract_model,
        "windows": len(corpus),
        "wall_s": round(wall, 1),
        "cost": round(cost, 6),
        "failures": failures,
        "entities": [
            {"id": e.id, "name": e.canonical, "type": e.type,
             "aliases": e.aliases, "traits": len(st.list_traits(e.id))}
            for e in ents
        ],
        "type_dist": dict(Counter(e.type for e in ents)),
        "place_as_person": place_errors,
        "traits": {
            "total": len(traits_all),
            "active": len(active),
            "inferred": len(inferred),
            "inferred_share": round(len(inferred) / len(active), 3) if active else 0,
            "with_evidence": len(with_ev),
            "slot_filled_share": round(
                sum(1 for t in active if t.slot) / len(active), 3) if active else 0,
            "slot_dist": dict(slot_dist.most_common()),
            "per_entity": round(len(active) / len(ents), 2) if ents else 0,
        },
        "linked_entities_per_session": {s: n for s, n in names_seen},
    }
    c.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    r = asyncio.run(run(a.limit))

    print(f"模型 {r['model']}  窗口 {r['windows']}  耗时 {r['wall_s']}s  成本 ¥{r['cost']:.6f}")
    if r["failures"]:
        print(f"失败窗口 {len(r['failures'])}：{r['failures']}")
    print(f"实体 {len(r['entities'])}  类型分布 {r['type_dist']}")
    print(f"地名被当成人：{r['place_as_person'] or '无'}")
    t = r["traits"]
    print(f"traits 总计 {t['total']}（active {t['active']}，inferred {t['inferred']}"
          f" = {t['inferred_share']:.0%}）")
    print(f"证据覆盖 {t['with_evidence']}/{t['active']}   "
          f"槽位填充率 {t['slot_filled_share']:.0%}   每实体 {t['per_entity']} 条")
    print(f"槽位分布 {t['slot_dist']}")
    print("\n实体清单：")
    for e in r["entities"]:
        alias = f"  别名={e['aliases']}" if e["aliases"] else ""
        print(f"  #{e['id']:<3} {e['name']:<10} {e['type']:<7} traits={e['traits']}{alias}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
