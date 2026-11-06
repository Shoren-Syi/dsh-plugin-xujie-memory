# -*- coding: utf-8 -*-
"""给 4 个真实会话生成会话摘要（真 key），然后重测 20 题。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, r"C:\h\hippo")

import httpx  # noqa: E402

from hippo.config import Config  # noqa: E402
from hippo.core.digest import build_digest, get_digest  # noqa: E402
from hippo.core.encode import Encoder  # noqa: E402
from hippo.core.pack import pack  # noqa: E402
from hippo.core.store import Store  # noqa: E402
from hippo.tools.eval_all import clone_store, norm  # noqa: E402

DB = Path(r"C:\h\stress\humanlab.db")
LABELS = json.loads(Path(r"C:\h\stress\human-labels-20.json").read_text(encoding="utf-8"))
KEY = os.environ.get("HIPPO_TEST_KEY", "")


async def make_digests(cfg: Config) -> dict[str, str]:
    out = {}
    async with httpx.AsyncClient(timeout=90) as client:
        with Store.open(DB, cfg) as store:
            for session in sorted({x["session"] for x in LABELS}):
                before = get_digest(store, session)
                if before:
                    out[session] = before
                    continue
                text = await build_digest(store, session, cfg, client, KEY)
                out[session] = text
    return out


def measure(cfg: Config) -> tuple[int, list[str]]:
    hits, missed = 0, []
    with Store.open(DB, cfg) as store:
        for item in LABELS:
            ans = norm(item["answer"])
            probe = clone_store(store, cfg)
            try:
                text = pack(probe, item["session"], item["question"], cfg=cfg, use_cache=False).text
            finally:
                probe.close()
            if ans in norm(text):
                hits += 1
            else:
                missed.append(item["question"][:16])
    return hits, missed


async def main() -> int:
    if not KEY:
        print("缺 HIPPO_TEST_KEY"); return 2
    cfg = Config()
    cfg.db_path = str(DB)
    digests = await make_digests(cfg)
    for s, d in digests.items():
        print(f"[{s}] {d[:100]}", flush=True)
    hits, missed = measure(cfg)
    print(json.dumps({"questions": len(LABELS), "strict_hits": hits,
                      "rate": round(hits / len(LABELS), 3),
                      "digest_chars": {k: len(v) for k, v in digests.items()},
                      "missed": missed}, ensure_ascii=False))
    Path(r"C:\h\stress\report-digest-human20.json").write_text(json.dumps(
        {"strict_hits": hits, "total": len(LABELS), "digests": digests, "missed": missed},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
