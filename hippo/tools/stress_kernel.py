"""DSH 内核压测（M29）：并发写事件 + 组装，看 SQLite 与延迟表现。

用法（先起内核）：

    python -m hippo.tools.stress_kernel http://127.0.0.1:8793 --concurrency 20 --requests 30
"""

from __future__ import annotations

import asyncio
import sys
import time

import httpx


async def worker(client: httpx.AsyncClient, base: str, idx: int, n: int,
                 errors: list[str]) -> list[float]:
    lat: list[float] = []
    session = f"stress-{idx}"
    for j in range(n):
        t0 = time.perf_counter()
        try:
            r = await client.post(f"{base}/event", json={
                "session": session, "role": "user",
                "text": f"第{idx}个用户第{j}条：我住在城市{idx}，喜欢东西{j}，预算{j * 100}元"})
            r.raise_for_status()
            r = await client.post(f"{base}/assemble", json={
                "session": session, "text": "我住哪？预算多少？"})
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        lat.append(time.perf_counter() - t0)
    return lat


async def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else "http://127.0.0.1:8793"
    concurrency = int(argv[2]) if len(argv) > 2 else 20
    per = int(argv[3]) if len(argv) > 3 else 30

    limits = httpx.Limits(max_connections=concurrency + 5, max_keepalive_connections=concurrency)
    errors: list[str] = []
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        groups = await asyncio.gather(*[
            worker(client, base, i, per, errors) for i in range(concurrency)])
    elapsed = time.perf_counter() - started
    lat = sorted(x for g in groups for x in g)
    if not lat:
        print("全部失败：", errors[:5])
        return 1

    def pct(p: float) -> float:
        return lat[min(len(lat) - 1, int(len(lat) * p))] * 1000

    total = len(lat)
    print(f"requests   {total}（{concurrency} 并发 × {per} 轮 × 2 次调用）")
    print(f"elapsed    {elapsed:.1f}s")
    print(f"throughput {total / elapsed:.1f} req/s")
    print(f"p50        {pct(0.50):.1f}ms")
    print(f"p95        {pct(0.95):.1f}ms")
    print(f"p99        {pct(0.99):.1f}ms")
    print(f"errors     {len(errors)}")
    for e in errors[:5]:
        print("  -", e)
    return 0 if len(errors) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
