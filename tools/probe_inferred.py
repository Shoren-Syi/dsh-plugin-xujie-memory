"""真机探针：模型到底会不会给 inferred。

M7.3 的核心是"推测必须带前缀"，可要是模型从不输出 inferred，
这条链路就是**没被真实流量走过**——单测绿只能证明格式对，
证明不了它会出现在上下文里。

所以单独喂一段"句句都在暗示、但没有一句直说"的对话，
看模型会不会推断，以及推断挂没挂 evidence。

跑法：
    python3 -m tools.probe_inferred
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx

from hippo.config import load_config
from hippo.core.card import format_trait
from hippo.core.db import connect
from hippo.core.encode import Encoder
from hippo.core.migrate import migrate
from hippo.core.persist import persist_result
from hippo.core.store import Store
from hippo.core.turns import record_pair, slice_windows

TURNS = [
    ("今天又加班到十点，老板还在群里阴阳怪气", "听着就累。"),
    ("算了不说了，反正说了也没用", "嗯，我在。"),
    ("我最近老失眠，三点多就醒", "这确实难受，持续多久了？"),
    ("有时候觉得这工作真没意思", "这种时候挺难熬的。"),
    ("不过年终奖还没发，先忍着", "理解，先把该拿的拿到。"),
]


async def run() -> dict:
    cfg = load_config()
    cfg.deepseek.thinking = "disabled"
    tmp = Path(tempfile.mkdtemp(prefix="hippo-inf-"))
    db = tmp / "i.db"
    c = connect(str(db))
    migrate(c)
    c.close()
    for u, a in TURNS:
        record_pair(str(db), "inf", u, a)

    store = Store.open(db)
    cost = 0.0
    async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.extract.timeout + 30)) as client:
        enc = Encoder(cfg, client, cfg.deepseek.api_key)
        for w in slice_windows(store.pending_turns("inf"), session="inf",
                               per_window=5, max_tokens=1200):
            if not w.complete:
                continue
            out = await enc.encode(w, known="（暂无）")
            cost += out.cost
            if out.status != "ok":
                return {"error": out.error}
            persist_result(store, "inf", w, out.result)

    rows = []
    for e in store.list_entities("inf"):
        for t in store.list_traits(e.id, active_only=False):
            rows.append({"entity": e.canonical, "status": t.status, "conf": t.confidence,
                         "trait": t.trait, "line": format_trait(t),
                         "ev": t.evidence, "slot": t.slot})
    store.close()
    return {"cost": round(cost, 6), "traits": rows}


def main() -> int:
    r = asyncio.run(run())
    if "error" in r:
        print("失败：", r["error"])
        return 1
    print(f"成本 ¥{r['cost']:.6f}")
    inf = [t for t in r["traits"] if t["status"] == "inferred"]
    print(f"traits {len(r['traits'])} 条，其中 inferred {len(inf)} 条\n")
    for t in r["traits"]:
        mark = "  ← 推测" if t["status"] == "inferred" else ""
        print(f"  [{t['status']:<8}] {t['line']}")
        print(f"             slot={t['slot'] or '-'} conf={t['conf']:.2f} ev={t['ev']}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
