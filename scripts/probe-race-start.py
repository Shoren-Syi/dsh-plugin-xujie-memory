# -*- coding: utf-8 -*-
"""两个内核同时在一个**新库**上启动：迁移竞态会不会让其中一个起不来。"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx

ROOT = r"C:\h\hippo"
PY = r"C:\h\hippo\runtime\win32-x64\python.exe"
DB = Path(r"C:\h\stress\race-start.db")
PORTS = (8861, 8862)


def main() -> int:
    if DB.exists():
        DB.unlink()
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8", HIPPO_LOG="off")
    procs = {}
    for port in PORTS:
        procs[port] = subprocess.Popen(
            [PY, "-m", "hippo.dsh_kernel", "--db", str(DB), "--port", str(port)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(6)
    report = {"alive": {}, "health": {}, "stderr": {}}
    with httpx.Client(timeout=3) as c:
        for port in PORTS:
            p = procs[port]
            alive = p.poll() is None
            report["alive"][port] = alive
            if alive:
                try:
                    report["health"][port] = c.get(f"http://127.0.0.1:{port}/health").json().get("instance")
                except Exception as exc:  # noqa: BLE001
                    report["health"][port] = repr(exc)[:80]
    for port in PORTS:
        p = procs[port]
        p.terminate()
        try:
            _o, err = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            _o, err = p.communicate()
        tail = err.decode("utf-8", "ignore").strip().splitlines()
        report["stderr"][port] = tail[-6:]
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
