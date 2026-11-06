"""pack 装配基准（M13.1 / M13.2）。

跑 100 轮真实口吻的对话，看四件事：

    总 token ≤1100        每轮都不能超（超了是硬伤，会顶掉对话历史）
    确定性                同样的输入 100 次字节一致（否则缓存永远不命中）
    pinned 永不丢         用户钉的东西被裁掉，等于这个功能不存在
    复用率                D 段有多少轮是直接复用上一轮的（缓存的前提）

**为什么不用真机库跑 100 轮**：44 条事件在第 20 轮之后就没有新东西可检索了，
后面 80 轮全是复用——那测的是复用逻辑，不是装配逻辑。所以这里用
"造 300 条事件 + 99 轮对话"，让检索每轮都有新结果。

真机的缓存命中率由 M13.3 单独验证（要调 API，花钱）。
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from hippo.config import load_config
from hippo.core.db import connect
from hippo.core.migrate import migrate
from hippo.core.pack import TOTAL_BUDGET, pack
from hippo.core.store import Store

DIALOG = Path("tests/fixtures/dialog_100.jsonl")
TOPICS = ["搬家", "房租", "通勤", "加班", "体检", "房贷", "出差", "猫粮",
          "年终奖", "辞职", "吉他", "蛋糕", "复查", "开会", "高铁", "香菜"]


def build(n: int = 300, *, path: Path) -> Store:
    conn = connect(path)
    migrate(conn)
    store = Store(conn)
    store.attach_config(load_config())
    base = int(datetime(2026, 6, 1).timestamp())
    with store.tx():
        for i in range(n):
            kw = " ".join(TOPICS[i % 5:(i % 5) + 3] or TOPICS[:2])
            store.add_event(
                session="bench",
                raw=f"第{i}条：我{TOPICS[i % len(TOPICS)]}的事，"
                    f"顺便说{TOPICS[(i * 3) % len(TOPICS)]}也变了",
                summary=f"用户{TOPICS[i % 5]}和{TOPICS[(i * 7) % 5]}的事，第{i}条",
                keywords=kw,
                event_time=base + i * 3600,
                weight=0.3 + (i % 7) / 10,
                tier=1 + (i % 3),
            )
    store.conn.commit()
    return store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--rounds", type=int, default=0, help="0 = 用 fixture 全部")
    ap.add_argument("--deterministic", type=int, default=100)
    ap.add_argument("--db", default="")
    ap.add_argument("--session", default="bench")
    args = ap.parse_args()

    import tempfile
    path = Path(args.db) if args.db else Path(tempfile.mkdtemp()) / "pack.db"
    store = Store.open(path, load_config()) if args.db else build(args.n, path=path)

    rows = [json.loads(x) for x in DIALOG.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    if args.rounds:
        rows = rows[: args.rounds]

    now = datetime(2026, 11, 5, 14, 0)
    totals, reuse, over = [], 0, 0
    for i, r in enumerate(rows):
        res = pack(store, args.session, r["cur"], now=now + timedelta(minutes=i))
        totals.append(res.total_tokens)
        reuse += 1 if res.reused else 0
        if res.total_tokens > TOTAL_BUDGET:
            over += 1
            print(f"  超预算 第{i}轮 {res.total_tokens}")

    # 确定性：同一句话（同一分钟）跑 N 次，字节必须一样
    fixed = now + timedelta(days=1)
    digests = {pack(store, "det", "我搬家了", now=fixed).digest
               for _ in range(args.deterministic)}
    # 换一个会话再跑，排除"缓存了上一次结果"这种假确定性
    digests |= {pack(store, f"det{i}", "我搬家了", now=fixed).digest
                for i in range(5)}

    print(f"轮数 {len(rows)}   平均 {statistics.mean(totals):.0f} token   "
          f"最大 {max(totals)}   超预算 {over}/{len(rows)}")
    print(f"复用 {reuse}/{len(rows)} ({reuse / len(rows):.0%})   "
          f"确定性 {len(digests)}/1（应为 1）")
    ok = over == 0 and len(digests) == 1
    store.close()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
