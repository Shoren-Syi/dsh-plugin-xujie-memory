# -*- coding: utf-8 -*-
"""194 题人工标注评测：建库 → 真实抽取 → 摘要 → 严格/判定。

数据来源：
  - 50 题来自 private-eval-*（旧导出，turns 在 humanlab.db）
  - 144 题来自 real-*（real-corpus.jsonl）
两条线合并进同一个库，避免"只在同一条数据上好看"。
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\h\hippo")

import httpx  # noqa: E402

from hippo.config import Config  # noqa: E402
from hippo.core.digest import build_digest, get_digest, save_meta  # noqa: E402
from hippo.core.encode import Encoder  # noqa: E402
from hippo.core.extractor import Extractor  # noqa: E402
from hippo.core.pack import pack  # noqa: E402
from hippo.core.store import Store  # noqa: E402
from hippo.tools.eval_all import clone_store, norm  # noqa: E402

STRESS = Path(r"C:\h\stress")
DB = STRESS / "labels200-v5.db"
LABELS = json.loads((STRESS / "human-labels-200.json").read_text(encoding="utf-8"))
CORPUS = STRESS / "real-corpus.jsonl"
OLD_DB = STRESS / "humanlab.db"
KEY = os.environ.get("HIPPO_TEST_KEY", "")


def import_turns(cfg: Config) -> dict:
    wanted = {x["session"] for x in LABELS}
    real_sessions = {s for s in wanted if s.startswith("real-")}
    rows: list[tuple] = []
    for line in (CORPUS).open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r["session"] in real_sessions:
            rows.append((r["session"], r["role"], r["text"]))
    old_sessions = {s for s in wanted if s.startswith("private-")}
    old_rows: list[tuple] = []
    if old_sessions and OLD_DB.exists():
        src = sqlite3.connect(OLD_DB)
        marks = ",".join("?" * len(old_sessions))
        for session, role, content, ts in src.execute(
                f"select session, role, content, ts from turns where session in ({marks})"
                " order by session, idx", tuple(old_sessions)):
            old_rows.append((session, role, content, int(ts or 0)))
        src.close()
    # 只导入"被标注的那一段"：每个会话取到第 14 条用户消息为止。
    # 真实导出里有 1000+ 轮的超长会话，全量导入会让抽取窗口爆炸（几百次调用），
    # 而且标注本来就是按前 14 条用户消息写的。
    capped: dict[str, list[tuple]] = collections.defaultdict(list)
    user_seen: collections.Counter = collections.Counter()
    for session, role, text in rows:
        if user_seen[session] >= 14:
            continue
        capped[session].append((role, text))
        if role == "user":
            user_seen[session] += 1
    rows = [(s, r, t) for s, items in capped.items() for (r, t) in items]
    with Store.open(DB, cfg) as store:
        by_session = collections.defaultdict(list)
        for session, role, text in rows:
            by_session[session].append((role, text, 0))
        for session, items in by_session.items():
            store.add_turns(session, items, ts=1_760_000_000)
        by_old = collections.defaultdict(list)
        for session, role, content, ts in old_rows:
            by_old[session].append((role, content, 0, ts))
        for session, items in by_old.items():
            store.add_turns(session, [(r, c, p) for r, c, p, _ in items],
                            ts=items[0][3] or 1_760_000_000)
    return {"real_rows": len(rows), "old_rows": len(old_rows)}


async def prepare(cfg: Config) -> dict:
    stats = {"windows": 0, "events": 0, "facts": 0, "traits": 0, "digests": 0}
    async with httpx.AsyncClient(timeout=120) as client:
        with Store.open(DB, cfg) as store:
            sessions = [r[0] for r in store.conn.execute(
                "select distinct session from turns").fetchall()]
            ex = Extractor(store, cfg, Encoder(cfg, client, KEY))
            for i, session in enumerate(sessions, 1):
                rep = await ex.run(session, force=True)
                stats["windows"] += len(rep.results)
                stats["events"] += rep.events
                stats["facts"] += sum(r.facts for r in rep.results)
                stats["traits"] += sum(r.traits for r in rep.results)
                if i % 10 == 0:
                    print(f"  抽取 {i}/{len(sessions)} 会话，事件 {stats['events']}", flush=True)
            for session in sessions:
                if get_digest(store, session):
                    continue
                if await build_digest(store, session, cfg, client, KEY):
                    save_meta(store, session, idx=store.last_turn_idx(session), at=0)
                    stats["digests"] += 1
    return stats


async def ask(client, messages, max_tokens: int = 160) -> str:
    r = await client.post("https://api.deepseek.com/chat/completions",
                          headers={"Authorization": f"Bearer {KEY}"},
                          json={"model": "deepseek-flash", "messages": messages,
                                "temperature": 0, "max_tokens": max_tokens,
                                "thinking": {"type": "disabled"}})
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    return str(msg.get("content") or msg.get("reasoning_content") or "")


async def measure(cfg: Config) -> dict:
    rows = []
    strict_hits = judged_hits = 0
    async with httpx.AsyncClient(timeout=120) as client:
        with Store.open(DB, cfg) as store:
            for i, item in enumerate(LABELS, 1):
                probe = clone_store(store, cfg)
                try:
                    ctx = pack(probe, item["session"], item["question"], cfg=cfg, use_cache=False).text
                finally:
                    probe.close()
                strict = norm(item["answer"]) in norm(ctx)
                answer = await ask(client, [
                    {"role": "system", "content": "仅依据下面的记忆回答问题；没有依据就回答「不知道」。\n" + ctx},
                    {"role": "user", "content": item["question"]}])
                verdict = await ask(client, [
                    {"role": "system", "content": "判断「回答」是否包含「期望信息」的意思（同义即可）。只输出 correct/partial/missing。"},
                    {"role": "user", "content": f"期望信息：{item['answer']}\n回答：{answer}"}], max_tokens=12)
                v = verdict.strip().lower()
                kind = "correct" if v.startswith("correct") else ("partial" if v.startswith("partial") else "missing")
                strict_hits += int(strict)
                judged_hits += int(kind in ("correct", "partial"))
                rows.append({"q": item["question"][:30], "strict": strict, "verdict": kind})
                if i % 25 == 0:
                    print(f"  评测 {i}/{len(LABELS)}：严格 {strict_hits} 判定 {judged_hits}", flush=True)
    return {"strict": strict_hits, "judged": judged_hits, "total": len(rows), "rows": rows}


async def main() -> int:
    if not KEY:
        print("缺 HIPPO_TEST_KEY")
        return 2
    if DB.exists():
        DB.unlink()
    cfg = Config()
    cfg.db_path = str(DB)
    cfg.extract.retries = 0
    cfg.extract.retry_empty = True
    imported = import_turns(cfg)
    print(json.dumps(imported, ensure_ascii=False), flush=True)
    stats = await prepare(cfg)
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    res = await measure(cfg)
    out = {**imported, **stats, "strict": res["strict"], "judged": res["judged"],
           "total": res["total"], "rows": res["rows"]}
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, ensure_ascii=False), flush=True)
    (STRESS / "report-labels200-v5.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
