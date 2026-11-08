# -*- coding: utf-8 -*-
"""找 bug 第二组：摘要循环是否真的跑、QQ 接口的坏输入怎么处理。"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

import httpx

ROOT = r"C:\h\hippo"
PY = r"C:\h\hippo\runtime\win32-x64\python.exe"
KEY = os.environ.get("HIPPO_TEST_KEY", "")
PORT = 8844
DB = r"C:\h\stress\digest-loop.db"


async def main() -> int:
    if os.path.exists(DB):
        os.remove(DB)
    shutil.copyfile(r"C:\h\stress\humanlab.db", DB)
    # 清掉已有摘要，确保循环必须自己生成
    c = sqlite3.connect(DB)
    c.execute("DELETE FROM session_state WHERE key LIKE 'session_digest%'")
    c.commit()
    c.close()

    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8",
               HIPPO_LOG="off", HIPPO_EXTRACT_API_KEY=KEY)
    proc = subprocess.Popen([PY, "-m", "hippo.dsh_kernel", "--db", DB, "--port", str(PORT)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = {}
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=30) as c:
            for _ in range(100):
                try:
                    if (await c.get("/health", timeout=2)).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            h = (await c.get("/health")).json()
            out["health_extract"] = h.get("extract")

            # 1) 摘要循环：最多等 150 秒
            deadline = time.time() + 150
            digests = 0
            while time.time() < deadline:
                await asyncio.sleep(10)
                conn = sqlite3.connect(DB)
                digests = conn.execute(
                    "SELECT count(*) FROM session_state WHERE key='session_digest'").fetchone()[0]
                conn.close()
                if digests:
                    break
            out["digest_created"] = digests

            # 2) QQ 坏输入
            cases = {}
            r = await c.post("/qq/settings", content=b"{not json",
                             headers={"content-type": "application/json"})
            cases["non_json_settings"] = r.status_code
            r = await c.post("/qq/budget/reserve", json={"scope_id": "10001"})
            cases["missing_fields_reserve"] = r.status_code
            r = await c.get("/qq/budget/status")
            cases["status_without_scope"] = r.status_code
            r = await c.post("/qq/settings", json={"selfId": "1", "wsUrl": "ws://x", "httpUrl": "http://x",
                                                   "provider": "p", "model": "m", "maxTokens": 10,
                                                   "budgetScope": "1",
                                                   "schedule": {"proactive": {"enabled": True,
                                                                              "intervalMinutes": 99999,
                                                                              "prompt": "x", "target": "1"}}})
            cases["interval_too_large"] = r.status_code
            r = await c.post("/qq/settings", json={"selfId": "1", "wsUrl": "http://bad", "httpUrl": "http://x",
                                                   "provider": "p", "model": "m", "maxTokens": 10, "budgetScope": "1"})
            cases["bad_ws_scheme"] = r.status_code
            out["qq_bad_inputs"] = cases

            # 3) 状态页字段
            st = (await c.get("/stats")).json()
            out["stats_keys"] = sorted(st.keys())[:12]
    finally:
        proc.terminate()
        try:
            _o, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _o, err = proc.communicate()
        out["stderr_tail"] = err.decode("utf-8", "ignore")[-400:] if err else ""
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
