"""真机探针：同一个人四个称呼，会不会被记成四个人。

这是 M7.1 唯一真正要回答的问题。fixtures 里的用例是我编的，
模型实际会不会把"王哥，就是老王"写进 aliases，只有真跑才知道。

跑法：
    python3 -m tools.probe_alias
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx

from hippo.config import load_config
from hippo.core.db import connect
from hippo.core.encode import Encoder
from hippo.core.migrate import migrate
from hippo.core.persist import persist_result
from hippo.core.store import Store
from hippo.core.turns import record_pair, slice_windows

TURNS = [
    ("王哥今天又加班到十点，他老婆都抱怨了", "加班到这个点，确实辛苦。"),
    ("对了老王说他下周要去深圳出差，让我帮他喂猫", "他经常出差吗？喂猫这事你答应了？"),
    ("我叫他王哥是因为他比我大几岁，其实他叫王建国", "原来是这么个称呼。"),
    ("隔壁老王这人挺热心的，上次帮我搬家", "邻里关系不错。"),
    ("王建国说他女儿今年高考，压力挺大", "那确实是个关口。"),
]


async def run() -> dict:
    cfg = load_config()
    cfg.deepseek.thinking = "disabled"
    tmp = Path(tempfile.mkdtemp(prefix="hippo-alias-"))
    db = tmp / "a.db"
    c = connect(str(db))
    migrate(c)
    c.close()
    for u, a in TURNS:
        record_pair(str(db), "alias", u, a)

    store = Store.open(db)
    cost = 0.0
    detail: list[dict] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.extract.timeout + 30)) as client:
        enc = Encoder(cfg, client, cfg.deepseek.api_key)
        for w in slice_windows(store.pending_turns("alias"), session="alias",
                               per_window=5, max_tokens=1200):
            if not w.complete:
                continue
            out = await enc.encode(w, known="（暂无）")
            cost += out.cost
            if out.status != "ok":
                detail.append({"error": out.error})
                continue
            for ev in out.result.events:
                detail.append({
                    "raw": ev.raw[:40],
                    "entities": [{"n": r.name, "t": r.type, "a": r.aliases}
                                 for r in ev.entities],
                    "traits": [{"e": t.entity, "t": t.trait, "s": t.slot}
                               for t in out.result.traits],
                })
            persist_result(store, "alias", w, out.result)

    ents = [{"id": e.id, "name": e.canonical, "aliases": e.aliases, "type": e.type}
            for e in store.list_entities("alias")]
    store.close()
    return {"cost": round(cost, 6), "events": detail, "entities": ents}


def main() -> int:
    r = asyncio.run(run())
    print(f"成本 ¥{r['cost']:.6f}")
    print("\n模型每轮给出的实体：")
    for d in r["events"]:
        if "error" in d:
            print(f"  失败 {d['error']}")
            continue
        print(f"  {d['raw']}")
        for e in d["entities"]:
            print(f"     name={e['n']} type={e['t']} aliases={e['a']}")
        for t in d["traits"]:
            print(f"     trait: {t['e']}｜{t['t']} [{t['s']}]")
    print(f"\n最终实体（{len(r['entities'])} 个）：")
    for e in r["entities"]:
        print(f"  #{e['id']} {e['name']} type={e['type']} aliases={e['aliases']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
