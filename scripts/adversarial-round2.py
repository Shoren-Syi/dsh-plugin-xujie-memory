# -*- coding: utf-8 -*-
"""第二轮对抗：真机也会遇到、但普通压测测不出来的情况。

1. 重复投递幂等：同一条消息被重复 POST（DSH 重发/插件重试），会不会写两遍。
2. 同会话并发：10 个并发同时写同一个 session，idx 会不会重号、顺序会不会乱。
3. 会话名边界：超长 session 名、带路径/控制字符的 session 名。
4. 极短/极长交替：1 字消息和 2 万字消息交替写，看窗口与索引是否还自洽。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DB = Path("C:/h/stress/adv2.db")
PORT = 8815
PY = Path(sys.executable)


def start() -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", HIPPO_LOG="off")
    env.pop("HIPPO_EXTRACT_API_KEY", None)  # 这轮不抽，只看写入层
    return subprocess.Popen([str(PY), "-m", "hippo.dsh_kernel", "--db", str(DB), "--port", str(PORT)],
                            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def wait(client: httpx.AsyncClient) -> None:
    for _ in range(200):
        try:
            if (await client.get("/health", timeout=2)).status_code == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.3)
    raise RuntimeError("kernel not up")


def counts() -> dict:
    import sqlite3
    c = sqlite3.connect(str(DB))
    out = {
        "turns": c.execute("select count(*) from turns").fetchone()[0],
    }
    out["dup_content_same_session"] = c.execute("""
        select coalesce(sum(n-1), 0) from (
          select session, content, count(*) n from turns group by session, content having n > 1)""").fetchone()[0]
    out["idx_collision"] = c.execute("""
        select coalesce(sum(n-1), 0) from (
          select session, idx, count(*) n from turns group by session, idx having n > 1)""").fetchone()[0]
    out["sessions"] = c.execute("select count(distinct session) from turns").fetchone()[0]
    c.close()
    return out


async def main() -> int:
    if DB.exists():
        DB.unlink()
    proc = start()
    report: dict = {}
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=60) as c:
            await wait(c)

            # 1) 重复投递：同一条消息连发 5 次
            for _ in range(5):
                await c.post("/event", json={"session": "dup-s", "role": "user",
                                             "text": "我住在杭州拱墅区，做后端"})
            report["dup_post"] = counts()

            # 2) 同会话并发 12 条
            async def one(i: int):
                await c.post("/event", json={"session": "conc-s", "role": "user",
                                             "text": f"并发消息 {i}：我在改第 {i} 个文件"})
            await asyncio.gather(*[one(i) for i in range(12)])
            report["concurrent"] = counts()

            # 3) 会话名边界
            names = ["x" * 300, "../../etc/passwd", "a\x00b", "会话 带 空格/斜杠",
                     "😀" * 40, "s" * 64]
            bad = []
            for nm in names:
                try:
                    r = await c.post("/event", json={"session": nm, "role": "user", "text": "测试会话名"})
                    if r.status_code != 200:
                        bad.append({"name": nm[:24], "status": r.status_code})
                except Exception as exc:  # noqa: BLE001
                    bad.append({"name": nm[:24], "error": repr(exc)[:120]})
            report["weird_session_names"] = {"cases": len(names), "bad": bad}

            # 4) 极短/极长交替
            for i in range(6):
                await c.post("/event", json={"session": "mix-s", "role": "user",
                                             "text": "嗯" if i % 2 == 0 else "我" * 15000})
            report["mix"] = counts()

            # 5) 板子接口在脏数据后还能不能出数据
            b = await c.get("/board/data", timeout=60)
            report["board"] = {"status": b.status_code,
                               "nodes": len((b.json() or {}).get("nodes") or [])}
    finally:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    report["final_dup"] = report.get("dup_post", {}).get("dup_content_same_session")
    report["final_idx_collision"] = counts()["idx_collision"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
