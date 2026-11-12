# -*- coding: utf-8 -*-
"""找 bug：坏 key 下的摘要循环 + 怪异会话名。

A. 用一个无效 key 启动内核，灌几轮对话，等 90 秒：
   - 内核必须还活着
   - 摘要循环失败要静默重试，不能崩、不能刷屏
B. 会话名 fuzz：空格/中文/emoji/超长/路径分隔符，/event 与 /assemble 都要正常
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx

PY = r"C:\h\hippo\runtime\win32-x64\python.exe"
PORT = 8897
DB = Path(r"C:\h\stress\badkey.db")


async def main() -> int:
    if DB.exists():
        DB.unlink()
    env = dict(os.environ, PYTHONPATH=r"C:\h\hippo", PYTHONIOENCODING="utf-8",
               HIPPO_LOG="off", HIPPO_EXTRACT_API_KEY="sk-invalid-key-for-test")
    proc = subprocess.Popen([PY, "-m", "hippo.dsh_kernel", "--db", str(DB), "--port", str(PORT)],
                            cwd=r"C:\h\hippo", env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = {}
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=30) as c:
            for _ in range(100):
                try:
                    if (await c.get("/health", timeout=2)).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            out["health_before"] = (await c.get("/health")).json().get("ok")

            # A. 灌几轮，让抽取/摘要去撞无效 key
            for i in range(6):
                await c.post("/event", json={"session": "badkey", "role": "user",
                                             "text": f"第 {i} 条：我在测试无效 key 时内核会不会崩"})
            await c.post("/extract?force=true")
            await asyncio.sleep(60)
            r = await c.get("/health")
            out["alive_after_60s"] = r.status_code == 200 and r.json().get("ok") is True
            conn = sqlite3.connect(DB)
            out["digests"] = conn.execute(
                "select count(*) from session_state where key='session_digest'").fetchone()[0]
            bad = conn.execute(
                "select count(*) from extract_runs where status in ('error','degraded')").fetchone()[0]
            out["failed_runs"] = bad
            conn.close()

            # B. 会话名 fuzz
            names = ["有 空格 的会话", "中文会话名", "emoji🐋会话", "a" * 200,
                     "..\\..\\evil", "s/slash", "tab\there", ""]
            codes = []
            for n in names:
                r = await c.post("/event", json={"session": n, "role": "user",
                                                 "text": f"会话名测试：{n[:10]} 内容"})
                codes.append(r.status_code)
                r2 = await c.post("/assemble", json={"session": n, "text": "测试"})
                codes.append(r2.status_code)
            out["fuzz_codes"] = sorted(set(codes))
    finally:
        proc.terminate()
        try:
            _o, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            _o, err = proc.communicate()
        tail = err.decode("utf-8", "ignore").strip().splitlines()[-5:]
        out["stderr_tail"] = tail
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
