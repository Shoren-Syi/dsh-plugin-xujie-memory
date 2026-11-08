# -*- coding: utf-8 -*-
"""浸泡测试：10 万条写入 + 定期采样 RSS / 句柄，看是否持续增长。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import httpx

ROOT = r"C:\h\hippo"
PY = r"C:\h\hippo\runtime\win32-x64\python.exe"
PORT = 8850
DB = Path(r"C:\h\stress\soak.db")
TOTAL = int(os.environ.get("SOAK_TOTAL", "100000"))
CONC = int(os.environ.get("SOAK_CONC", "8"))


def rss_mb(pid: int) -> float:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
                         capture_output=True, text=True).stdout
    lines = [l for l in out.splitlines() if str(pid) in l]
    if len(lines) < 2:
        return -1
    parts = lines[1].split('","')
    try:
        return round(int(parts[-2].replace('"', "").replace(",", "")) / 1024, 1)
    except Exception:  # noqa: BLE001
        return -1


def handles(pid: int) -> int:
    script = f"(Get-Process -Id {pid}).HandleCount"
    out = subprocess.run(["pwsh", "-NoProfile", "-Command", script],
                         capture_output=True, text=True).stdout.strip()
    try:
        return int(out)
    except ValueError:
        return -1


async def main() -> int:
    if DB.exists():
        DB.unlink()
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8", HIPPO_LOG="off")
    proc = subprocess.Popen([PY, "-m", "hippo.dsh_kernel", "--db", str(DB), "--port", str(PORT)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples, errors = [], []
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=30) as c:
            for _ in range(100):
                try:
                    if (await c.get("/health", timeout=2)).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            sem = asyncio.Semaphore(CONC)
            stop = False

            async def sampler():
                while not stop:
                    await asyncio.sleep(30)
                    samples.append({"t": round(time.time() - t0), "rss": rss_mb(proc.pid),
                                    "handles": handles(proc.pid)})

            async def one(i: int):
                async with sem:
                    try:
                        r = await c.post("/event", json={
                            "session": f"soak-{i % 40}", "role": "user" if i % 2 == 0 else "assistant",
                            "text": f"浸泡第 {i} 条：今天在测长期运行的稳定性，编号 {i}，记录一下细节。"})
                        if r.status_code != 200:
                            errors.append({"i": i, "status": r.status_code})
                    except Exception as exc:  # noqa: BLE001
                        errors.append({"i": i, "error": repr(exc)[:100]})

            t0 = time.time()
            task = asyncio.create_task(sampler())
            await asyncio.gather(*[one(i) for i in range(TOTAL)])
            stop = True
            await task
            elapsed = time.time() - t0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    import sqlite3
    conn = sqlite3.connect(DB)
    turns = conn.execute("select count(*) from turns").fetchone()[0]
    conn.close()
    print(json.dumps({
        "total": TOTAL, "errors": len(errors), "samples": errors[:5],
        "elapsed_s": round(elapsed, 1), "rps": round(TOTAL / elapsed, 1),
        "turns_in_db": turns, "mem": samples,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
