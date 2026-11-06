"""每日重算的独立入口（cron 用）。

    # 试算，看会怎么迁移
    python3 -m tools.recompute --db ~/.hippo/mem.db --dry-run

    # 真跑
    python3 -m tools.recompute --db ~/.hippo/mem.db

    # 每天 3 点跑一次
    0 3 * * *  cd /path/to/hippo && python3 -m tools.recompute >> ~/.hippo/recompute.log 2>&1

为什么不写成 `hippo recompute` 就够了——那个也在，但 cron 里调用 CLI
要处理虚拟环境、工作目录、配置发现，而这里只需要一个 db 路径。
**运维脚本越少依赖越好**：出问题时人是在电话里排查，不是在 REPL 里。

退出码：0 正常；1 有 pinned 违规（pinned 却不是 hot，说明公式被改坏了）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hippo.config import load_config                 # noqa: E402
from hippo.core.dynamics import recompute            # noqa: E402
from hippo.core.store import Store                   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="", help="库路径（默认取配置）")
    ap.add_argument("--session", default="", help="只算这个会话")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", action="store_true", help="输出一行 JSON，方便喂监控")
    a = ap.parse_args()

    cfg = load_config()
    db = a.db or str(cfg.db_file)
    if not Path(db).expanduser().exists():
        print(f"库不存在：{db}", file=sys.stderr)
        return 2

    store = Store.open(db, cfg)
    try:
        rep = recompute(store, cfg=cfg.weight, dry_run=a.dry_run,
                        session=a.session or None, limit=a.limit)
    finally:
        store.close()

    if a.json:
        import json
        print(json.dumps({
            "total": rep.total, "changed": rep.changed, "dry_run": rep.dry_run,
            "dist": rep.dist, "hot_ratio": round(rep.hot_ratio, 4),
            "moves": {f"{k[0]}->{k[1]}": v for k, v in rep.moves.items()},
            "pinned_bad": rep.pinned_bad, "ms": round(rep.elapsed_ms, 1),
        }, ensure_ascii=False))
    else:
        tag = "[dry-run] " if rep.dry_run else ""
        print(f"{tag}{rep.line()}")
        print(f"  变动 {rep.changed}　迁移 {rep.matrix()}")
        if rep.pinned_bad:
            print(f"  ⚠ pinned 却不是 hot 的 {rep.pinned_bad} 条", file=sys.stderr)

    return 1 if rep.pinned_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
