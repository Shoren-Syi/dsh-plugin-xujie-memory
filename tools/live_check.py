"""真连 DeepSeek 的一次性体检（会花钱，约 ¥0.001）。

M2 验收里"真连一次"这条，靠桩是验不出来的：桩不会 429、不会
返回你没想到的 usage 形状、不会因为 stream_options 报 400。

用法：
    DEEPSEEK_API_KEY=sk-... python -m tools.live_check
"""

from __future__ import annotations

import os
import sys
import time

import httpx

from hippo.adapters.proxy import create_app
from hippo.config import Config


def main() -> int:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("需要 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2

    import uvicorn

    cfg = Config()
    cfg.deepseek.api_key = key
    cfg.db_path = os.path.expanduser("~/.hippo/live-check.db")

    config = uvicorn.Config(create_app(cfg), host="127.0.0.1", port=8791, log_level="error")
    server = uvicorn.Server(config)
    import threading

    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)

    ok = True
    try:
        with httpx.Client(timeout=90) as c:
            r = c.post("http://127.0.0.1:8791/v1/chat/completions",
                       json={"model": "deepseek-chat", "max_tokens": 20,
                             "messages": [{"role": "user", "content": "只回复两个字：收到"}]},
                       headers={"authorization": f"Bearer {key}"})
            print(f"unary   {r.status_code} {r.text[:160]}")
            ok &= r.status_code == 200

            t0 = time.perf_counter()
            stamps = []
            with c.stream("POST", "http://127.0.0.1:8791/v1/chat/completions",
                          json={"model": "deepseek-chat", "stream": True, "max_tokens": 30,
                                "messages": [{"role": "user", "content": "数到十"}]},
                          headers={"authorization": f"Bearer {key}"}) as s:
                for line in s.iter_lines():
                    if line:
                        stamps.append((time.perf_counter() - t0) * 1000)
            print(f"stream  {len(stamps)} 行 / 首字节 {stamps[0]:.0f}ms / 结束 {stamps[-1]:.0f}ms")
            ok &= len(stamps) > 3
    finally:
        server.should_exit = True

    print("LIVE PASS" if ok else "LIVE FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
