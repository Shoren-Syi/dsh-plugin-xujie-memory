# -*- coding: utf-8 -*-
"""重建评测库并做 A/B：原话兜底开 vs 关（严格命中，不需要模型判定）。

步骤：
1. 从重建的 real-corpus.jsonl 里，按"答案反查"把 194 题里的 real-* 标注映射到新会话；
2. 新库导入这些会话的 turns，用真 key 抽取 + 生成摘要；
3. 对每个标注做两次 pack（raw_turns 关/开），比较严格命中。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, r"C:\h\hippo")

import httpx  # noqa: E402

from hippo.config import Config  # noqa: E402
from hippo.core.digest import build_digest, get_digest, save_meta  # noqa: E402
from hippo.core.encode import Encoder  # noqa: E402
from hippo.core.extractor import Extractor  # noqa: E402
from hippo.core.pack import pack  # noqa: E402
from hippo.core.store import Store  # noqa: E402
from hippo.tools.eval_all import norm  # noqa: E402

STRESS = Path(r"C:\h\stress")
CORPUS = STRESS / "real-corpus.jsonl"
LABELS = json.loads((STRESS / "human-labels-200.json").read_text(encoding="utf-8"))
DB = STRESS / "ab-raw.db"
KEY = os.environ.get("HIPPO_TEST_KEY", "")


def load_corpus() -> dict[str, list[tuple[str, str]]]:
    by: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for line in CORPUS.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        by[row["session"]].append((row["role"], row["text"]))
    return dict(by)


def map_labels(by: dict[str, list[tuple[str, str]]]) -> list[dict]:
    """用答案反查会话：答案必须逐字出现在该会话的 user 消息里。"""
    out = []
    for item in LABELS:
        if not item["session"].startswith("real-"):
            continue
        ans = norm(item["answer"])
        for sid, turns in by.items():
            if any(role == "user" and ans in norm(text) for role, text in turns):
                out.append({**item, "new_session": sid})
                break
    return out


async def prepare(cfg: Config, by: dict[str, list[tuple[str, str]]], mapped: list[dict]) -> dict:
    sessions = sorted({m["new_session"] for m in mapped})
    if DB.exists():
        DB.unlink()
    # 只导入到第 14 条用户消息为止：标注本来就是按前 14 条写的，
    # 全量导入会让长会话的窗口数爆炸（实测 real-101 有 60+ 窗口）。
    with Store.open(DB, cfg) as store:
        for sid in sessions:
            kept, users = [], 0
            for role, text in by[sid]:
                if users >= 14:
                    break
                kept.append((role, text, 0))
                if role == "user":
                    users += 1
            store.add_turns(sid, kept, ts=1_760_000_000)
    stats = {"sessions": len(sessions), "windows": 0, "events": 0}
    async with httpx.AsyncClient(timeout=120) as client:
        with Store.open(DB, cfg) as store:
            ex = Extractor(store, cfg, Encoder(cfg, client, KEY))
            for i, sid in enumerate(sessions, 1):
                rep = await ex.run(sid, force=True)
                stats["windows"] += len(rep.results)
                stats["events"] += rep.events
                if i % 10 == 0:
                    print(f"  抽取 {i}/{len(sessions)} 会话，事件 {stats['events']}", flush=True)
            for sid in sessions:
                if get_digest(store, sid):
                    continue
                if await build_digest(store, sid, cfg, client, KEY):
                    save_meta(store, sid, idx=store.last_turn_idx(sid), at=0)
    return stats


def measure(cfg: Config, mapped: list[dict]) -> dict:
    on = off = 0
    rows = []
    with Store.open(DB, cfg) as store:
        for item in mapped:
            q, ans = item["question"], norm(item["answer"])
            cfg.retrieve.raw_turns = False
            off_text = pack(store, item["new_session"], q, cfg=cfg, use_cache=False).text
            cfg.retrieve.raw_turns = True
            on_text = pack(store, item["new_session"], q, cfg=cfg, use_cache=False).text
            hit_off, hit_on = ans in norm(off_text), ans in norm(on_text)
            off += hit_off
            on += hit_on
            if hit_on and not hit_off:
                rows.append({"q": q[:30], "a": item["answer"][:30], "session": item["new_session"]})
    cfg.retrieve.raw_turns = True
    return {"total": len(mapped), "strict_off": off, "strict_on": on, "newly_hit": rows}


async def main() -> int:
    if not CORPUS.exists():
        print("缺语料，先跑 scripts/build-corpus.py")
        return 2
    by = load_corpus()
    mapped = map_labels(by)
    print(f"标注映射成功 {len(mapped)}/{sum(1 for x in LABELS if x['session'].startswith('real-'))}", flush=True)
    if not mapped:
        return 1
    cfg = Config()
    cfg.db_path = str(DB)
    cfg.extract.retries = 0
    stats = await prepare(cfg, by, mapped)
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    res = measure(cfg, mapped)
    out = {**stats, **{k: v for k, v in res.items() if k != "newly_hit"}}
    print(json.dumps(out, ensure_ascii=False), flush=True)
    print("新增命中样例:", json.dumps(res["newly_hit"][:8], ensure_ascii=False), flush=True)
    (STRESS / "report-ab-raw.json").write_text(
        json.dumps({**out, "newly_hit": res["newly_hit"]}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
