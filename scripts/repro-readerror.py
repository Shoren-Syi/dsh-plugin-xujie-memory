# -*- coding: utf-8 -*-
"""复现 ReadError：并发 vs 串行，看错误是否只在并发下出现。"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = r"C:\h\hippo"
PY = r"C:\h\hippo\runtime\win32-x64\python.exe"


async def run_case(concurrency: int, total: int, port: int, tag: str) -> dict:
    db = Path(rf"C:\h\stress\repro-{tag}.db")
    if db.exists():
        db.unlink()
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8", HIPPO_LOG="off")
    proc = subprocess.Popen([PY, "-m", "hippo.dsh_kernel", "--db", str(db), "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    errors = []
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as c:
            for _ in range(100):
                try:
                    if (await c.get("/health", timeout=2)).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            sem = asyncio.Semaphore(concurrency)
            lat = []

            async def one(i: int):
                async with sem:
                    t0 = time.perf_counter()
                    try:
                        r = await c.post("/event", json={"session": "s", "role": "user",
                                                         "text": f"第 {i} 条：我在测试连接稳定性，编号 {i}"})
                        lat.append((time.perf_counter() - t0) * 1000)
                        if r.status_code != 200:
                            errors.append((i, r.status_code))
                    except Exception as exc:  # noqa: BLE001
                        errors.append((i, repr(exc)[:120]))

            t0 = time.perf_counter()
            await asyncio.gather(*[one(i) for i in range(total)])
            elapsed = time.perf_counter() - t0
    finally:
        proc.kill()
        _out, err = proc.communicate(timeout=10)
    return {"tag": tag, "concurrency": concurrency, "total": total, "errors": len(errors),
            "samples": errors[:5], "elapsed": round(elapsed, 1),
            "stderr_tail": err.decode("utf-8", "ignore")[-400:] if err else ""}


async def main() -> int:
    a = await run_case(1, 600, 8841, "serial")
    b = await run_case(6, 600, 8842, "concurrent6")
    for r in (a, b):
        print(json_dump(r))
    return 0


def json_dump(d: dict) -> str:
    import json
    return json.dumps(d, ensure_ascii=False)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
