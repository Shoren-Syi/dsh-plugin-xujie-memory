# -*- coding: utf-8 -*-
"""双实例同库并发写：SQLite 锁竞争会不会丢数据或报 500。"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys

import httpx

ROOT = r"C:\h\hippo"
PY = r"C:\h\hippo\runtime\win32-x64\python.exe"
DB = r"C:\h\stress\multi-isolated.db"
PORTS = (8851, 8852)


def start(port: int) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8", HIPPO_LOG="off",
               HIPPO_KERNEL_INSTANCE=f"probe-{port}")
    return subprocess.Popen([PY, "-m", "hippo.dsh_kernel", "--db", DB, "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


async def main() -> int:
    if os.path.exists(DB):
        os.remove(DB)
    procs = [start(p) for p in PORTS]
    errors = []
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            for port in PORTS:
                for _ in range(100):
                    try:
                        if (await c.get(f"http://127.0.0.1:{port}/health", timeout=2)).status_code == 200:
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.2)

            ids = []
            for port in PORTS:
                h = (await c.get(f"http://127.0.0.1:{port}/health", timeout=3)).json()
                ids.append(h.get("instance"))
            if len(set(ids)) != 2:
                raise SystemExit(f"两个实例 id 相同或缺失，说明打到了同一个进程：{ids}")

            async def writer(port: int, tag: str, n: int):
                for i in range(n):
                    try:
                        r = await c.post(f"http://127.0.0.1:{port}/event",
                                         json={"session": f"multi-{tag}", "role": "user",
                                               "text": f"{tag} 第 {i} 条：两个实例同时写同一个库"})
                        if r.status_code != 200:
                            errors.append({"port": port, "i": i, "status": r.status_code,
                                           "body": r.text[:120]})
                    except Exception as exc:  # noqa: BLE001
                        errors.append({"port": port, "i": i, "error": repr(exc)[:120]})

            await asyncio.gather(writer(PORTS[0], "a", 150), writer(PORTS[1], "b", 150))
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    c = sqlite3.connect(DB)
    rows = c.execute("select session, count(*) from turns group by session").fetchall()
    total = sum(n for _s, n in rows)
    c.close()
    print(json.dumps({"errors": len(errors), "samples": errors[:5],
                      "turns_by_session": dict(rows), "total": total,
                      "expected": 300}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
