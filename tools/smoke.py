"""端到端 smoke：桩上游 + 真代理 + 真 TCP。

单元测试再好，也测不出"curl 能不能通"。
这个脚本就是把 M2 验收里那句 `curl -s localhost:8787/health`
变成一条可重复执行的命令：

    python -m tools.smoke

它验四件事：
  1. 非流式请求原样返回
  2. 流式请求**逐块**到达（不是攒完一次吐）
  3. usage 落进 metrics
  4. 换 session 头，metrics 里是两个会话
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"端口 {port} 在 {timeout}s 内没起来")


def main() -> int:
    up_port, proxy_port = free_port(), free_port()
    work = Path(tempfile.mkdtemp(prefix="hippo-smoke-"))
    procs: list[subprocess.Popen] = []

    def spawn(args: list[str], **kw) -> subprocess.Popen:
        p = subprocess.Popen(args, cwd=str(ROOT), stdout=kw.pop("stdout", subprocess.DEVNULL),
                             stderr=kw.pop("stderr", subprocess.STDOUT), **kw)
        procs.append(p)
        return p

    try:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        spawn([sys.executable, "-m", "tools.fake_upstream", str(up_port)], env=env)
        wait_port(up_port)

        spawn([sys.executable, "-m", "hippo.cli", "init", "--dir", str(work)],
              env=env, stdout=subprocess.PIPE).wait()

        cfg = work / "config.yaml"
        cfg.write_text(
            cfg.read_text(encoding="utf-8").replace(
                "base_url: https://api.deepseek.com",
                f"base_url: http://127.0.0.1:{up_port}"),
            encoding="utf-8")

        spawn([sys.executable, "-m", "hippo.cli", "serve",
               "--dir", str(work), "--port", str(proxy_port), "--log-level", "warning"], env=env)
        wait_port(proxy_port)

        base = f"http://127.0.0.1:{proxy_port}"
        headers = {"authorization": "Bearer sk-smoke", "X-Hippo-Session": "smoke"}
        ok = True

        with httpx.Client(timeout=30) as c:
            h = c.get(f"{base}/health").json()
            print(f"health        {h}")
            ok &= h["status"] == "ok"

            r = c.post(f"{base}/v1/chat/completions",
                       json={"model": "deepseek-chat",
                             "messages": [{"role": "user", "content": "你好"}]},
                       headers=headers)
            text = r.json()["choices"][0]["message"]["content"]
            print(f"unary         {r.status_code} {text!r}")
            ok &= r.status_code == 200

            t0 = time.perf_counter()
            stamps: list[float] = []
            with c.stream("POST", f"{base}/v1/chat/completions",
                          json={"model": "deepseek-chat", "stream": True,
                                "messages": [{"role": "user", "content": "讲故事"}]},
                          headers={**headers, "X-Hippo-Session": "smoke-stream"}) as s:
                accel = s.headers.get("x-accel-buffering")
                for line in s.iter_lines():
                    if line:
                        stamps.append((time.perf_counter() - t0) * 1000)
            print(f"stream        {len(stamps)} 行 / {stamps[-1]:.0f}ms / "
                  f"首字节 {stamps[0]:.0f}ms / X-Accel-Buffering={accel}")
            # 8 个 delta + usage + DONE = 10 行；分块到了才说明没被缓冲
            ok &= len(stamps) >= 10 and stamps[0] < stamps[-1] * 0.5

        from hippo.core.db import connect
        conn = connect(work / "mem.db")
        rows = [dict(r) for r in conn.execute(
            "SELECT session, prompt_tokens, cached_tokens, t2-t0 AS ttfb, t3-t0 AS total, cost "
            "FROM metrics ORDER BY id").fetchall()]
        conn.close()
        for r in rows:
            print(f"metrics       {r['session']:<14} ptok={r['prompt_tokens']} "
                  f"hit={r['cached_tokens']} ttfb={r['ttfb']}ms total={r['total']}ms ¥{r['cost']}")
        ok &= len(rows) == 2 and all(r["prompt_tokens"] == 100 for r in rows)
        ok &= {r["session"] for r in rows} == {"smoke", "smoke-stream"}

        print("\n" + ("SMOKE PASS" if ok else "SMOKE FAIL"))
        return 0 if ok else 1
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
