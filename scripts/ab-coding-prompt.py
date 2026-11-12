# -*- coding: utf-8 -*-
"""A/B：代码窗口附加「工程记忆」提示词 有没有用。

在真实语料里挑代码味最重的会话，两种配置各抽一遍，比较：
- 事件条数
- 事件里含"代码痕迹"（路径/命令/报错）的比例
- 抽样看摘要是否更像工程记录
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, r"C:\h\hippo")

import httpx  # noqa: E402

from hippo.config import Config  # noqa: E402
from hippo.core.encode import Encoder  # noqa: E402
from hippo.core.extractor import Extractor, coding_window  # noqa: E402
from hippo.core.store import Store  # noqa: E402
from hippo.core.turns import Window, slice_windows  # noqa: E402

STRESS = Path(r"C:\h\stress")
CORPUS = STRESS / "real-corpus.jsonl"
KEY = os.environ.get("HIPPO_TEST_KEY", "")
CODE_HINT = re.compile(
    r"(?:[A-Za-z]:\\\\|/)?(?:[\w.-]+[/\\\\])+[\w.-]+\."
    r"(?:py|js|mjs|ts|tsx|go|rs|java|sql|ya?ml|json|toml|sh|ps1)\b"
    r"|\b(?:npm|pnpm|pip|git|docker|kubectl)\s"
    r"|Traceback \(most recent call last\)|Error:", re.IGNORECASE)


def load_sessions() -> dict[str, list[tuple[str, str]]]:
    by: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for line in CORPUS.open(encoding="utf-8"):
        line = line.strip()
        if line:
            row = json.loads(line)
            by[row["session"]].append((row["role"], row["text"]))
    return dict(by)


def code_sessions(by, want: int = 10) -> list[str]:
    scored = []
    for sid, turns in by.items():
        users = [t for r, t in turns if r == "user"]
        if len(users) < 3:
            continue
        blob = "\n".join(users)
        score = len(CODE_HINT.findall(blob)) + blob.count("```")
        if score >= 2:
            scored.append((score, sid))
    scored.sort(reverse=True)
    return [sid for _s, sid in scored[:want]]


async def run_config(cfg: Config, by, sessions: list[str], *, coding_on: bool) -> dict:
    db = STRESS / f"ab-coding-{'on' if coding_on else 'off'}.db"
    if db.exists():
        db.unlink()
    cfg.db_path = str(db)
    cfg.extract.coding_prompt = coding_on
    cfg.extract.retries = 0
    with Store.open(db, cfg) as store:
        for sid in sessions:
            kept, users = [], 0
            for role, text in by[sid]:
                if users >= 10:
                    break
                kept.append((role, text, 0))
                if role == "user":
                    users += 1
            store.add_turns(sid, kept, ts=1_760_000_000)
    async with httpx.AsyncClient(timeout=120) as client:
        with Store.open(db, cfg) as store:
            ex = Extractor(store, cfg, Encoder(cfg, client, KEY))
            for sid in sessions:
                await ex.run(sid, force=True)
            rows = store.conn.execute(
                "SELECT summary, raw, keywords FROM events WHERE kind='event'").fetchall()
    total = len(rows)
    with_code = sum(1 for r in rows if CODE_HINT.search(
        " ".join(str(r[k] or "") for k in ("summary", "raw", "keywords"))))
    samples = [" / ".join([str(r["summary"])[:60], str(r["keywords"] or "")[:40]]) for r in rows[:8]]
    return {"db": db.name, "events": total, "events_with_code_marks": with_code,
            "ratio": round(with_code / total, 3) if total else 0, "samples": samples}


async def main() -> int:
    if not CORPUS.exists():
        print("缺语料")
        return 2
    by = load_sessions()
    sessions = code_sessions(by, 10)
    print(f"选中 {len(sessions)} 个代码会话: {', '.join(sessions[:6])}...", flush=True)
    cfg = Config()
    off = await run_config(cfg, by, sessions, coding_on=False)
    on = await run_config(cfg, by, sessions, coding_on=True)
    out = {"sessions": len(sessions), "off": off, "on": on}
    print(json.dumps({k: v for k, v in out.items() if k != "samples"}, ensure_ascii=False))
    print("--- off 样例 ---")
    for s in off["samples"][:4]:
        print("  ", s)
    print("--- on 样例 ---")
    for s in on["samples"][:4]:
        print("  ", s)
    (STRESS / "report-ab-coding.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
