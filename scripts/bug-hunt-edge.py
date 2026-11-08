# -*- coding: utf-8 -*-
"""找 bug 第五批：首次使用与异常输入。

1. 空库（第一次装、一条记忆都没有）面板与接口是否正常
2. HTTP 异常输入：空 text、超长 text、非 JSON、缺字段、未知路由
3. 配置文件损坏（写一段坏 YAML）还能不能启动
4. 预算并发预留：会不会超发（真金白银的路径）
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = r"C:\h\hippo"
PY = r"C:\h\hippo\runtime\win32-x64\python.exe"
PORT = 8881


def start(env_extra: dict | None = None) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8", HIPPO_LOG="off")
    env.update(env_extra or {})
    return subprocess.Popen([PY, "-m", "hippo.dsh_kernel", "--db", str(DB), "--port", str(PORT)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


DB = Path(r"C:\h\stress\edge.db")


async def edge_http() -> dict:
    out = {}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=20) as c:
        cases = {
            "event_empty_text": ("/event", {"session": "s", "role": "user", "text": ""}),
            "event_no_session": ("/event", {"role": "user", "text": "你好"}),
            "event_bad_role": ("/event", {"session": "s", "role": "system", "text": "你好"}),
            "assemble_empty": ("/assemble", {"session": "s", "text": ""}),
            "assemble_unknown_session": ("/assemble", {"session": "nope", "text": "我住哪"}),
            "budget_status_unknown": ("/qq/budget/status?scope_id=nobody", None),
        }
        for name, (path, body) in cases.items():
            try:
                if body is None:
                    r = await c.get(path)
                else:
                    r = await c.post(path, json=body)
                out[name] = r.status_code
            except Exception as exc:  # noqa: BLE001
                out[name] = f"EXC {repr(exc)[:80]}"
        # 非 JSON
        r = await c.post("/event", content=b"not json", headers={"content-type": "application/json"})
        out["event_non_json"] = r.status_code
        # 超长
        r = await c.post("/event", json={"session": "s", "role": "user", "text": "长" * 200_000})
        out["event_200k_text"] = r.status_code
        # 未知路由
        out["unknown_route"] = (await c.get("/nope")).status_code
        # 空库时面板与数据接口
        board = await c.get("/board")
        data = await c.get("/board/data?tab=events&limit=5")
        out["empty_board"] = board.status_code
        out["empty_board_data"] = data.status_code
        try:
            payload = data.json()
            out["empty_board_counts"] = payload.get("counts", {}).get("events")
        except Exception:  # noqa: BLE001
            out["empty_board_counts"] = "parse-fail"
        stats = await c.get("/stats")
        out["stats_empty"] = stats.status_code
    return out


async def budget_race() -> dict:
    """20 个并发预留，看会不会超过每日预算限制。"""
    out = {}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=20) as c:
        configured = await c.post("/qq/budget/configure", json={
            "scope_id": "race", "per_request": 100_000, "daily": 1_000_000,
            "monthly": 10_000_000, "input_price": 1, "output_price": 1,
            "daily_calls": 5,
        })
        out["configure_status"] = configured.status_code
        async def one(i: int):
            try:
                r = await c.post("/qq/budget/reserve", json={
                    "scope_id": "race", "activity": "chat", "input_limit": 1000, "output_limit": 100,
                })
                return r.status_code
            except Exception as exc:  # noqa: BLE001
                return f"EXC {repr(exc)[:40]}"
        codes = await asyncio.gather(*[one(i) for i in range(20)])
        ok = sum(1 for x in codes if x == 200)
        denied = sum(1 for x in codes if x == 429 or x == 400)
        return {"ok": ok, "denied": denied, "codes": sorted(set(codes)), "out": out}


def main() -> int:
    report = {}
    if DB.exists():
        DB.unlink()
    proc = start()
    try:
        with httpx.Client(timeout=3) as c:
            for _ in range(100):
                try:
                    if c.get(f"http://127.0.0.1:{PORT}/health").status_code == 200:
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.2)
        report["http"] = asyncio.run(edge_http())
        report["budget_race"] = asyncio.run(budget_race())
    finally:
        proc.terminate()
        try:
            _o, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _o, err = proc.communicate()
        report["stderr_tail"] = err.decode("utf-8", "ignore")[-500:]

    # 损坏配置文件时启动
    cfg = Path(r"C:\Users\27203\.hippo\config.yaml")
    backup = cfg.read_bytes() if cfg.exists() else None
    try:
        cfg.write_text("deepseek: [unclosed\n  bad: : :\n", encoding="utf-8")
        DB.unlink(missing_ok=True)
        proc2 = start()
        alive = False
        with httpx.Client(timeout=3) as c:
            for _ in range(80):
                try:
                    if c.get(f"http://127.0.0.1:{PORT}/health").status_code == 200:
                        alive = True
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.2)
        report["bad_config_starts"] = alive
        proc2.terminate()
        try:
            _o2, err2 = proc2.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc2.kill()
            _o2, err2 = proc2.communicate()
        report["bad_config_stderr"] = err2.decode("utf-8", "ignore").strip().splitlines()[-3:]
    finally:
        if backup is not None:
            cfg.write_bytes(backup)
        else:
            cfg.unlink(missing_ok=True)

    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
