# -*- coding: utf-8 -*-
"""真实召回复测 v2：当前管线（importance + 去重 + 摘要）跑同一批 50 题。

流程：从 humanlab.db 复制原始 turn → 新库重新抽取 → 生成会话摘要 → 测严格/判定。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
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

SRC = Path(r"C:\h\stress\humanlab.db")
DST = Path(r"C:\h\stress\recall-v8.db")
LABELS = json.loads(Path(r"C:\h\stress\human-labels-50.json").read_text(encoding="utf-8"))
KEY = os.environ.get("HIPPO_TEST_KEY", "")


def clone_turns() -> int:
    if DST.exists():
        DST.unlink()
    src = sqlite3.connect(SRC)
    rows = src.execute("select session, idx, role, content, ts, partial from turns order by session, idx").fetchall()
    src.close()
    cfg = Config()
    cfg.db_path = str(DST)
    with Store.open(DST, cfg) as store:
        for session, _idx, role, content, ts, partial in rows:
            store.add_turns(session, [(role, content, int(partial or 0))], ts=int(ts or 0))
    return len(rows)


async def prepare(cfg: Config) -> dict:
    stats = {"windows": 0, "events": 0, "facts": 0, "traits": 0, "dup": 0, "digests": 0}
    async with httpx.AsyncClient(timeout=120) as client:
        with Store.open(DST, cfg) as store:
            ex = Extractor(store, cfg, Encoder(cfg, client, KEY))
            for session in [r[0] for r in store.conn.execute(
                    "select distinct session from turns").fetchall()]:
                rep = await ex.run(session, force=True)
                stats["windows"] += len(rep.results)
                stats["events"] += rep.events
                stats["traits"] += sum(r.traits for r in rep.results)
                stats["facts"] += sum(r.facts for r in rep.results)
            for session in [r[0] for r in store.conn.execute(
                    "select distinct session from turns").fetchall()]:
                if get_digest(store, session):
                    continue
                text = await build_digest(store, session, cfg, client, KEY)
                if text:
                    save_meta(store, session, idx=store.last_turn_idx(session), at=0)
                    stats["digests"] += 1
    return stats


async def measure(cfg: Config) -> dict:
    rows = []
    async with httpx.AsyncClient(timeout=120) as client:
        with Store.open(DST, cfg) as store:
            for item in LABELS:
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
                rows.append({"q": item["question"][:24], "strict": strict, "verdict": kind})
    strict_hits = sum(1 for r in rows if r["strict"])
    ok = sum(1 for r in rows if r["verdict"] in ("correct", "partial"))
    return {"strict": strict_hits, "judged": ok, "total": len(rows), "rows": rows}


async def ask(client, messages, max_tokens=160) -> str:
    r = await client.post("https://api.deepseek.com/chat/completions",
                          headers={"Authorization": f"Bearer {KEY}"},
                          json={"model": "deepseek-flash", "messages": messages,
                                "temperature": 0, "max_tokens": max_tokens,
                                "thinking": {"type": "disabled"}})
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    return str(msg.get("content") or msg.get("reasoning_content") or "")


async def main() -> int:
    if not KEY:
        print("缺 HIPPO_TEST_KEY")
        return 2
    cfg = Config()
    cfg.db_path = str(DST)
    cfg.extract.retries = 0
    n = clone_turns()
    stats = await prepare(cfg)
    res = await measure(cfg)
    out = {"turns": n, **stats, "strict": res["strict"], "judged": res["judged"],
           "total": res["total"], "rows": res["rows"]}
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, ensure_ascii=False))
    Path(r"C:\h\stress\report-recall-v8.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
