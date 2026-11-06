"""缓存命中率实测（M13.3）。

**这是 P2 阶段唯一的真机指标，也是成本模型成不成立的裁决点。**

跑法：
    python -m tools.probe_cache --rounds 30          # 真上游，约 ¥0.02
    python -m tools.probe_cache --offline            # 不调 API，只算字节前缀

两个模式测的东西不一样：
    真机   DeepSeek 到底命中了多少（usage.prompt_cache_hit_tokens）
    离线   我们的 pack 有没有"稳定前缀"（连续两轮的最长公共字符前缀）

离线模式的价值：它不需要调 API、不花钱，能进 CI。而且它测的是**原因**——
真机命中率掉了，离线会先掉；离线没掉而真机掉了，说明是上游的问题。

## 为什么用代理而不是直接调 API

直接调 API 只能测"上游缓存工作不工作"。走代理才能测"我们的 pack 有没有
破坏前缀"——而后者才是我们能改的那部分。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from hippo.config import load_config

DIALOG = Path("tests/fixtures/dialog_100.jsonl")


def _longest_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    lo, hi = 0, n
    # 二分找最长公共前缀：O(log n) 次比较，8KB 的文本也不到 1ms
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def offline(rounds: int) -> int:
    """不调 API：跑 pack，算相邻两轮的字节前缀重合率。"""
    import tempfile

    from hippo.core.db import connect
    from hippo.core.migrate import migrate
    from hippo.core.pack import inject, pack
    from hippo.core.store import Store

    db = Path(tempfile.mkdtemp()) / "cache.db"
    cfg = load_config()
    cfg.db_path = str(db)
    conn = connect(db)
    migrate(conn)
    store = Store(conn)
    store.attach_config(cfg)

    base = int(datetime(2026, 6, 1).timestamp())
    with store.tx():
        for i in range(240):
            store.add_event(
                session="c", raw=f"第{i}条记忆：关于搬家、房租、通勤的事",
                summary=f"用户第{i}次提到生活琐事", keywords="搬家 房租 通勤",
                event_time=base + i * 3600, weight=0.4, tier=1 + (i % 3))

    rows = [json.loads(x) for x in DIALOG.read_text(encoding="utf-8").splitlines()
            if x.strip()][:rounds]
    now = datetime(2026, 11, 5, 14, 0)
    prev, rates = "", []
    hist: list[dict] = [{"role": "system", "content": "你是阿岚。"}]
    for i, r in enumerate(rows):
        hist.append({"role": "user", "content": r["cur"]})
        text = pack(store, "c", r["cur"], cfg=cfg,
                    now=now + timedelta(minutes=i)).text
        msgs = inject(hist, text)
        # 整条 prompt 的字节（角色也拼进去：换 role 也算变）
        flat = "".join(f"[{m['role']}]{m['content']}" for m in msgs)
        if prev:
            rates.append(_longest_prefix(prev, flat) / max(1, len(prev)))
        prev = flat
        hist.append({"role": "assistant", "content": "好的。"})
    store.close()

    print("turn  hit_rate  " + "0%".ljust(10) + "100%")
    for i, v in enumerate(rates, 2):
        bar = "█" * int(v * 40)
        print(f"{i:4d}  {v:7.0%}  {bar}")
    if rates:
        warm = rates[2:] or rates
        print(f"\n稳定期（第 4 轮起）平均 {sum(warm) / len(warm):.0%}  "
              f"最低 {min(warm):.0%}")
    return 0


async def live(rounds: int, session: str) -> int:
    import httpx

    from hippo.adapters.proxy import create_app
    from hippo.core.db import connect
    from hippo.core.migrate import migrate

    cfg = load_config()
    db = Path(cfg.db_path).expanduser()
    conn = connect(db)
    migrate(conn)
    conn.close()

    app = create_app(cfg, store_path=db)
    rows = [json.loads(x) for x in DIALOG.read_text(encoding="utf-8").splitlines()
            if x.strip()][:rounds]

    history: list[dict] = []
    out: list[tuple[int, int, int]] = []
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hippo",
                                     timeout=120) as c:
            for i, r in enumerate(rows, 1):
                history.append({"role": "user", "content": r["cur"]})
                resp = await c.post("/v1/chat/completions", json={
                    "model": cfg.deepseek.model,
                    "messages": history, "max_tokens": 40, "stream": False,
                }, headers={"X-Hippo-Session": session,
                            "authorization": f"Bearer {cfg.deepseek.api_key}"})
                if resp.status_code >= 400:
                    print(f"  第{i}轮失败 {resp.status_code}: {resp.text[:160]}")
                    break
                d = resp.json()
                usage = d.get("usage") or {}
                hit = int((usage.get("prompt_tokens_details") or {}).get(
                    "cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0)
                prompt = int(usage.get("prompt_tokens") or 0)
                out.append((i, hit, prompt))
                history.append({"role": "assistant",
                                "content": d["choices"][0]["message"]["content"]})

    print("turn  hit/prompt       rate")
    rates = []
    for i, hit, prompt in out:
        rate = hit / prompt if prompt else 0.0
        rates.append(rate)
        bar = "█" * int(rate * 40)
        print(f"{i:4d}  {hit:5d}/{prompt:<5d}  {rate:7.0%}  {bar}")
    warm = rates[5:] or rates
    print(f"\n稳定期（第 6 轮起）平均 {sum(warm) / len(warm):.0%}  "
          f"最低 {min(warm):.0%}")
    zero = [i for i, v in enumerate(rates, 1) if v == 0 and i > 3]
    if zero:
        print(f"掉零轮次：{zero}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=30)
    ap.add_argument("--session", default="cache-test")
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    if args.offline:
        return offline(args.rounds)
    return asyncio.run(live(args.rounds, args.session))


if __name__ == "__main__":
    sys.exit(main())
