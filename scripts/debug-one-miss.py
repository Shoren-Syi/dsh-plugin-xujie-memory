# -*- coding: utf-8 -*-
"""定位一个具体漏题：目标事件到底有没有进 L1 候选 / L2 打分池。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, r"C:\h\hippo")

from hippo.config import Config  # noqa: E402
from hippo.core.rank import l1_candidates, l2_search, parse_query  # noqa: E402
from hippo.core.store import Store  # noqa: E402
from hippo.core.topic import should_retrieve, user_keywords  # noqa: E402
from hippo.tools.eval_all import norm  # noqa: E402

DB = Path(r"C:\h\stress\humanlab.db")
CASES = [
    ("private-eval-01", "系统唯一的恢复密钥存放在哪里？", "存放在U盘上"),
    ("private-eval-05", "用户要求生图的比例是多少？", "3:4"),
    ("private-eval-08", "游戏的最终目标是什么？", "逃出潘宏园区"),
]


def main() -> int:
    cfg = Config()
    cfg.db_path = str(DB)
    with Store.open(DB, cfg) as store:
        for session, q, ans in CASES:
            a = norm(ans)
            print("=" * 58)
            print("Q:", q, "| 期望:", ans)
            trig = should_retrieve(store, session, q)
            print("  trigger:", trig.fire, trig.reason, "kws=", sorted(user_keywords(store, session, q)))
            qy = parse_query(q, session, store)
            l1 = l1_candidates(store, session, qy)
            print("  L1 候选:", len(l1.event_ids))
            res = l2_search(store, session, q, query=qy)
            print("  L2 取回:", len(res.items), " 空原因:", res.degraded if hasattr(res, "degraded") else "-")
            evs = store.conn.execute(
                "select id, summary from events where session=?", (session,)).fetchall()
            target = [r["id"] for r in evs if a in norm(str(r["summary"] or ""))]
            print("  目标事件(摘要含答案):", target, " 在 L1:", [t for t in target if t in set(l1.event_ids)],
                  " 在 L2:", [t for t in target if t in {i.id for i in res.items}])
            for it in res.items[:4]:
                mark = " <== 目标" if it.id in target else ""
                print(f"    {it.score:.3f} #{it.id} kw_hit={it.kw_hit} {str(it.event.summary)[:26]}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
