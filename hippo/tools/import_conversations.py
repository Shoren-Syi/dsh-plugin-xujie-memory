"""把真实 DeepSeek 导出导入海马体，并跑真实抽取（M28 验收 · 真实数据）。

用法::

    # 只看会导入什么、花多少窗口
    python -m hippo.tools.import_conversations conversations.json \
        --dir C:\\h\\t3 --dry-run

    # 真导入 + 真抽取（花钱）
    python -m hippo.tools.import_conversations conversations.json \
        --dir C:\\h\\t3 --limit-sessions 3 --turns-per-session 60 \
        --title-contains 交接 中转 ITX

每一步都走真实路径：解析导出 → 写 `turns`（和代理落库同一张表）→
调真实模型抽取（和 `hippo extract` 同一个 Extractor）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from ..config import load_config
from ..core.store import Store


def _walk_chain(mapping: dict) -> list[dict]:
    """沿 mapping 树的 children 链取节点，返回正确的时间顺序。

    不能按 `inserted_at` 排序：DeepSeek 导出里 assistant 回复的时间戳可能
    比对应 user 消息**早几毫秒**（THINK 先落、REQUEST 后落），按时间排会把
    assistant 排到 user 前面；窗口切片要求"user 开头、assistant 结尾"，
    一旦顺序反了，预筛会把整段对话判成不可抽。
    """
    nodes = mapping or {}
    roots = [n for n in nodes.values()
             if str(n.get("parent")) == "root" or str(n.get("id")) == "root"]
    roots.sort(key=lambda n: str(n.get("id")))
    order = []
    seen = set()
    cursor = roots[0] if roots else None
    while cursor is not None:
        nid = str(cursor.get("id"))
        if nid in seen:
            break
        seen.add(nid)
        order.append(cursor)
        children = cursor.get("children") or []
        cursor = nodes.get(str(children[0])) if children else None
    return order


def parse_export(path: str | Path) -> list[dict]:
    """DeepSeek 导出 → [{id, title, msgs:[{t, user, assistant}]}]，按树序。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for conv in raw:
        msgs = []
        for node in _walk_chain(conv.get("mapping") or {}):
            m = node.get("message") or {}
            frags = m.get("fragments") or []
            if not frags:
                continue
            req = next((f.get("content") for f in frags if f.get("type") == "REQUEST"), "")
            res = next((f.get("content") for f in frags if f.get("type") == "RESPONSE"), "")
            if req or res:
                msgs.append({"t": str(m.get("inserted_at") or ""), "user": req or "",
                             "assistant": res or ""})
        if msgs:
            out.append({"id": conv.get("id") or "unknown",
                        "title": conv.get("title") or "", "msgs": msgs})
    return out


def _ts(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except (ValueError, TypeError):
        return int(datetime.now().timestamp())


def import_conv(store: Store, conv: dict, max_turns: int) -> dict:
    session = conv["id"][:12]
    pairs = conv["msgs"][:max_turns]
    n = 0
    for m in pairs:
        items: list[tuple[str, str, int]] = []
        if m["user"]:
            items.append(("user", m["user"], 0))
        if m["assistant"]:
            items.append(("assistant", m["assistant"], 0))
        if items:
            store.add_turns(session=session, items=items, ts=_ts(m["t"]))
            n += 1
    return {"session": session, "title": conv["title"], "turns": n}


async def extract_sessions(cfg, store: Store, key: str, sessions: list[str]):
    import httpx

    from ..core.encode import Encoder
    from ..core.extractor import Extractor

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=cfg.extract.timeout,
                              write=30.0, pool=10.0),
    ) as client:
        ex = Extractor(store, cfg, Encoder(cfg, client, key))
        return [await ex.run(s, force=True) for s in sessions]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hippo.tools.import_conversations",
                                description="把真实 DeepSeek 导出导入海马体并抽取")
    p.add_argument("export", help="conversations.json 路径")
    p.add_argument("--dir", default="", help="hippo 工作目录（默认 ~/.hippo）")
    p.add_argument("--limit-sessions", type=int, default=3)
    p.add_argument("--turns-per-session", type=int, default=60)
    p.add_argument("--title-contains", nargs="*", default=[],
                   help="只导入标题含这些词之一的对话")
    p.add_argument("--no-extract", action="store_true", help="只导入不抽取")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)

    cfg = load_config()
    if a.dir:
        target = Path(a.dir).expanduser()
        # 必须读 --dir 下的 config.yaml：否则 prescreen: false 这类开关
        # 在导入工具里完全不生效（和 cli 的 _config_for 同一个语义）。
        cfg_file = target / "config.yaml"
        cfg = load_config(str(cfg_file)) if cfg_file.exists() else load_config()
        cfg.db_path = str(target / "mem.db")
    db = Path(cfg.db_path).expanduser()
    if not db.exists():
        print(f"数据库不存在：{db}（先 hippo init --dir ...）", file=sys.stderr)
        return 1

    convs = parse_export(a.export)
    picks = [c for c in convs
             if not a.title_contains or any(k in c["title"] for k in a.title_contains)]
    picks.sort(key=lambda c: len(c["msgs"]), reverse=True)
    picks = picks[: a.limit_sessions]
    if not picks:
        print("没有匹配的对话", file=sys.stderr)
        return 1

    print(f"选中 {len(picks)} 段：")
    for c in picks:
        print(f"  {len(c['msgs']):>4} msg  {c['title'][:40]}")

    if a.dry_run:
        total = sum(min(len(c["msgs"]), a.turns_per_session) for c in picks)
        print(f"\n预计导入 {total} 轮，约 {total // 5} 个抽取窗口")
        return 0

    store = Store.open(db, cfg)
    try:
        done = [import_conv(store, c, a.turns_per_session) for c in picks]
    finally:
        store.close()

    for d in done:
        print(f"  导入 [{d['session']}] {d['turns']} 轮  {d['title'][:30]}")

    if a.no_extract:
        print("跳过抽取（--no-extract）")
        return 0

    key = cfg.resolve_api_key(None)
    if not key:
        print("缺少 API Key：设 DEEPSEEK_API_KEY 或写进 config", file=sys.stderr)
        return 1

    store = Store.open(db)
    try:
        reports = asyncio.run(extract_sessions(cfg, store, key, [d["session"] for d in done]))
    finally:
        store.close()

    total_events = 0
    total_cost = 0.0
    for rep in reports:
        print(f"[{rep.session}] 事件 {rep.events} 条  花费 ¥{rep.cost:.4f}")
        for r in rep.results:
            print("  " + r.line())
        total_events += rep.events
        total_cost += rep.cost
    print(f"\n合计事件 {total_events} 条，花费 ¥{total_cost:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
