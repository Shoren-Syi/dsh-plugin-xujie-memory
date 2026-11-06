# -*- coding: utf-8 -*-
"""20 题人工标注的判定版：模型作答 + 同型号判定，输出聚合数字。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, r"C:\h\hippo")

import httpx  # noqa: E402

from hippo.config import Config  # noqa: E402
from hippo.core.pack import pack  # noqa: E402
from hippo.core.store import Store  # noqa: E402
from hippo.tools.eval_all import clone_store, norm  # noqa: E402

DB = Path(r"C:\h\stress\humanlab.db")
LABELS = json.loads(Path(r"C:\h\stress\human-labels-20.json").read_text(encoding="utf-8"))
KEY = os.environ.get("HIPPO_TEST_KEY", "")
BASE = "https://api.deepseek.com"


async def ask(client: httpx.AsyncClient, messages: list[dict], max_tokens: int = 160) -> str:
    r = await client.post(f"{BASE}/chat/completions",
                          headers={"Authorization": f"Bearer {KEY}"},
                          json={"model": "deepseek-flash", "messages": messages,
                                "temperature": 0, "max_tokens": max_tokens,
                                "thinking": {"type": "disabled"}})
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    return str(msg.get("content") or msg.get("reasoning_content") or "")


async def main() -> int:
    if not KEY:
        print("缺 HIPPO_TEST_KEY"); return 2
    cfg = Config()
    cfg.db_path = str(DB)
    rows = []
    async with httpx.AsyncClient(timeout=90) as client:
        with Store.open(DB, cfg) as store:
            for item in LABELS:
                probe = clone_store(store, cfg)
                try:
                    ctx = pack(probe, item["session"], item["question"], cfg=cfg, use_cache=False).text
                finally:
                    probe.close()
                answer = await ask(client, [
                    {"role": "system", "content": "仅依据下面的记忆回答问题；没有依据就回答「不知道」。\n" + ctx},
                    {"role": "user", "content": item["question"]}])
                verdict = await ask(client, [
                    {"role": "system", "content": "你是判卷人。判断「回答」是否包含「期望信息」的意思（同义即可，不要求逐字）。只输出一个词：correct / partial / missing。"},
                    {"role": "user", "content": f"期望信息：{item['answer']}\n回答：{answer}"}], max_tokens=12)
                v = verdict.strip().lower()
                kind = "correct" if v.startswith("correct") else ("partial" if v.startswith("partial") else "missing")
                rows.append({"q": item["question"][:20], "expected": item["answer"],
                             "answer": answer[:80], "verdict": kind})
    counts = {k: sum(1 for r in rows if r["verdict"] == k) for k in ("correct", "partial", "missing")}
    ok = counts["correct"] + counts["partial"]
    print(json.dumps({"questions": len(rows), "judged": counts, "correct_or_partial": ok,
                      "rate": round(ok / len(rows), 3)}, ensure_ascii=False))
    Path(r"C:\h\stress\report-human20-judged.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
