# -*- coding: utf-8 -*-
"""大样本复现：按语料顺序灌 3000 条，抓内核 stderr 和失败样本。"""
from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import time

import httpx

ROOT = r"C:\h\hippo"
PY = r"C:\h\hippo\runtime\win32-x64\python.exe"
PORT = 8843


def load_rows(n: int) -> list[dict]:
    rows = []
    for p in (r"C:\h\stress\real-corpus.jsonl", r"C:\h\stress\gen-e.jsonl"):
        for line in io.open(p, encoding="utf-8"):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
            if len(rows) >= n:
                return rows
    return rows


async def main() -> int:
    rows = load_rows(3000)
    db = r"C:\h\stress\repro3000.db"
    if os.path.exists(db):
        os.remove(db)
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8", HIPPO_LOG="off")
    proc = subprocess.Popen([PY, "-m", "hippo.dsh_kernel", "--db", db, "--port", str(PORT)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    errors, lat = [], []
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=30) as c:
            for _ in range(100):
                try:
                    if (await c.get("/health", timeout=2)).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            sem = asyncio.Semaphore(6)

            async def one(i: int, row: dict):
                async with sem:
                    t0 = time.perf_counter()
                    try:
                        r = await c.post("/event", json={"session": row["session"],
                                                         "role": row["role"], "text": row["text"]})
                        lat.append((time.perf_counter() - t0) * 1000)
                        if r.status_code != 200:
                            errors.append({"i": i, "status": r.status_code, "body": r.text[:120]})
                    except Exception as exc:  # noqa: BLE001
                        errors.append({"i": i, "error": repr(exc)[:160]})

            t0 = time.perf_counter()
            await asyncio.gather(*[one(i, r) for i, r in enumerate(rows)])
            elapsed = time.perf_counter() - t0
    finally:
        proc.terminate()
        try:
            _out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _out, err = proc.communicate()
    lat.sort()
    print(json.dumps({
        "rows": len(rows), "errors": len(errors), "samples": errors[:5],
        "elapsed": round(elapsed, 1),
        "p95": round(lat[int(len(lat) * 0.95)], 1) if lat else 0,
        "stderr_tail": err.decode("utf-8", "ignore")[-600:] if err else "",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
