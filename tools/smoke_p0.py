"""P0 Gate 端到端冒烟：20 轮完整链路。

和 `tools/smoke.py` 的分工：
  - smoke.py    验「代理有没有把请求转对」（2 轮，快）
  - smoke_p0.py 验「20 轮之后库里的东西对不对」（20 轮，慢，P0 出闸用）

默认用桩上游（零成本）。加 `--live` 走真 DeepSeek。

    python -m tools.smoke_p0
    DEEPSEEK_API_KEY=sk-... python -m tools.smoke_p0 --live
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from tools.smoke import free_port, wait_port

ROOT = Path(__file__).resolve().parent.parent
DIALOG = ROOT / "tests" / "fixtures" / "smoke_dialog.jsonl"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="真连 DeepSeek（花钱）")
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--quiet-log", action="store_true", help="关掉 hippo 日志，测纯延迟")
    args = ap.parse_args(argv)

    if not DIALOG.exists():
        print(f"缺少对话素材：{DIALOG}", file=sys.stderr)
        return 2
    lines = [json.loads(l) for l in DIALOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    lines = lines[: args.rounds]

    up_port, proxy_port = free_port(), free_port()
    work = Path(tempfile.mkdtemp(prefix="hippo-p0-"))
    procs: list[subprocess.Popen] = []

    logfile = open(work / "run.log", "a", encoding="utf-8")

    def spawn(cmd: list[str]) -> subprocess.Popen:
        p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=logfile,
                             stderr=subprocess.STDOUT,
                             env={**os.environ, "PYTHONUNBUFFERED": "1"})
        procs.append(p)
        return p

    try:
        if not args.live:
            spawn([sys.executable, "-m", "tools.fake_upstream", str(up_port)])
            wait_port(up_port)

        spawn([sys.executable, "-m", "hippo.cli", "init", "--dir", str(work)]).wait()
        cfg = work / "config.yaml"
        if not args.live:
            cfg.write_text(
                cfg.read_text(encoding="utf-8").replace(
                    "base_url: https://api.deepseek.com",
                    f"base_url: http://127.0.0.1:{up_port}"),
                encoding="utf-8")

        os.environ["HIPPO_LOG"] = "off" if args.quiet_log else "on"
        spawn([sys.executable, "-m", "hippo.cli", "serve", "--dir", str(work),
               "--port", str(proxy_port), "--log-level", "warning"])
        wait_port(proxy_port)

        base = f"http://127.0.0.1:{proxy_port}"
        key = os.environ.get("DEEPSEEK_API_KEY", "") if args.live else "sk-stub"
        headers = {"authorization": f"Bearer {key}", "X-Hippo-Session": "p0"}

        history: list[dict] = []
        ttfts: list[float] = []
        errors = 0
        ok = 0

        with httpx.Client(timeout=120) as c:
            for i, item in enumerate(lines, 1):
                history.append({"role": "user", "content": item["text"]})
                reply = []
                t0 = time.perf_counter()
                first = None
                try:
                    with c.stream("POST", f"{base}/v1/chat/completions",
                                  json={"model": "deepseek-chat", "stream": True,
                                        "messages": history},
                                  headers=headers) as r:
                        if r.status_code != 200:
                            raise RuntimeError(f"HTTP {r.status_code}: {r.read()[:200]}")
                        for line in r.iter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                            except ValueError:
                                continue
                            for ch in chunk.get("choices") or []:
                                piece = (ch.get("delta") or {}).get("content")
                                if isinstance(piece, str) and piece:
                                    if first is None:
                                        first = time.perf_counter()
                                    reply.append(piece)
                    ok += 1
                    if first:
                        ttfts.append((first - t0) * 1000)
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    print(f"  round {i} FAILED: {exc}")
                history.append({"role": "assistant", "content": "".join(reply)})
                print(f"  round {i}/{len(lines)} ok  ttft={(first - t0) * 1000 if first else -1:.0f}ms")

        from hippo.core.db import connect

        conn = connect(work / "mem.db")
        n_turns = conn.execute("SELECT count(*) FROM turns WHERE session='p0'").fetchone()[0]
        n_metrics = conn.execute("SELECT count(*) FROM metrics").fetchone()[0]
        bad_cost = conn.execute(
            "SELECT count(*) FROM metrics WHERE prompt_tokens = 0").fetchone()[0]
        conn.close()
        avg = sum(ttfts) / len(ttfts) if ttfts else 0

        print()
        print(f"round {ok}/{len(lines)} ok | turns={n_turns} | "
              f"avg_ttft={avg:.0f}ms | errors={errors} | metrics={n_metrics}")

        checks = {
            "全部成功": ok == len(lines),
            "turns=2*轮数": n_turns == 2 * len(lines),
            "平均TTFT<900ms": avg < 900,
            "无异常": errors == 0,
            "usage已记录": bad_cost == 0,
        }
        for k, v in checks.items():
            print(f"  {'PASS' if v else 'FAIL'}  {k}")

        all_ok = all(checks.values())
        if not all_ok:
            print(f"\n--- 现场：{work} ---")
            print((work / "run.log").read_text(encoding="utf-8")[-3000:])
        print("\n" + ("P0 GATE PASS" if all_ok else "P0 GATE FAIL"))
        return 0 if all_ok else 1
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    raise SystemExit(main())
