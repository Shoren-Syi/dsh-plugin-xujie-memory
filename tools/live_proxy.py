"""真机全链路演练（M5.5）：真客户端 → 真代理 → 真模型 → 真库 → 真记忆。

这是唯一能回答"即插即用到底能不能用"的测试：
客户端只改 base_url，其余一行不动，然后看库里有没有长出记忆。

和前几个工具的分工：
  probe_mimo.py     —— 问上游有什么能力（花钱最少）
  probe_quality.py  —— 问 prompt 抽得好不好（同一窗口反复跑）
  live_extract.py   —— 问抽取层指标（绕过代理，只测抽取）
  live_proxy.py     —— 问**整合后能不能跑**（本文件）

用法：
    python3 -m tools.live_proxy --rounds 6
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from hippo.core.store import Store

# 一段会触发抽取的自然对话。刻意混入寒暄，看预筛会不会在真实链路里生效。
CHAT = [
    ("你好", "你好！有什么可以帮你的？"),
    ("我下周要去趟成都出差，帮我看看要准备什么", "成都这个季节多雨，带把伞，另外注意饮食偏辣。"),
    ("我吃不了太辣的", "那点菜时说微辣，或者选不辣的馆子。"),
    ("行，我们公司出差报销要提前审批", "那记得提前走流程。"),
    ("对了，我养的那只猫叫年糕，得找人喂", "年糕多大了？找朋友帮忙喂几天比较稳妥。"),
    ("三岁了，我一般让老王帮忙", "老王靠谱的话就没问题。"),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=len(CHAT))
    ap.add_argument("--model", default="mimo-v2.6-flash")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="hippo-proxy-"))
    db = tmp / "mem.db"
    port = free_port()

    # 用 CLI 建库，保证"用户会走的那条路"和测试走的是同一条
    r = subprocess.run([sys.executable, "-m", "hippo.cli", "init", "--dir", str(tmp)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("init 失败:", r.stderr[-500:])
        return 1

    # ★ `hippo init --dir X` 会在 X 下写一份**默认（DeepSeek）**配置，
    #   而 serve --dir X 优先读它。所以要换上游，必须显式覆盖 base_url。
    #   第一次跑没加这两行，401 报的是"key 无效"——而真正的错因是端点是
    #   api.deepseek.com。这类错误信息会把人往 key 上引，很费时间。
    from hippo.config import load_config
    cfg = load_config()
    env = dict(os.environ)
    env["HIPPO_DEEPSEEK__EXTRACT_MODEL"] = args.model
    env["HIPPO_DEEPSEEK__THINKING"] = "disabled"
    env["HIPPO_DEEPSEEK__BASE_URL"] = cfg.deepseek.base_url
    env["HIPPO_DEEPSEEK__API_KEY"] = cfg.deepseek.api_key
    env["HIPPO_PRICING__INPUT_MISS_PER_M"] = str(cfg.pricing.input_miss_per_m)
    env["HIPPO_PRICING__INPUT_HIT_PER_M"] = str(cfg.pricing.input_hit_per_m)
    env["HIPPO_PRICING__OUTPUT_PER_M"] = str(cfg.pricing.output_per_m)

    srv = subprocess.Popen(
        [sys.executable, "-m", "hippo.cli", "serve", "--dir", str(tmp),
         "--port", str(port), "--host", "127.0.0.1"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            print("代理没起来")
            return 1

        # 客户端必须填**上游的** key：代理是透传，不做 key 兑换。
        # 换上游时要同时改客户端和配置，这是"透传"这个设计的必然代价，
        # 也是第一次跑就 401 的原因（我一开始在这传了个占位串）。
        api_key = cfg.deepseek.api_key
        if not api_key:
            print("配置里没有 api_key")
            return 2

        messages: list[dict[str, str]] = []
        t_all = time.perf_counter()
        for i, (u, _a) in enumerate(CHAT[:args.rounds]):
            messages.append({"role": "user", "content": u})
            t0 = time.perf_counter()
            with httpx.Client(timeout=180) as c:
                resp = c.post(f"{base}/v1/chat/completions",
                              json={"model": args.model, "messages": messages,
                                    "stream": False, "temperature": 1.0},
                              headers={"authorization": f"Bearer {api_key}",
                                       "X-Hippo-Session": "demo"})
            ms = int((time.perf_counter() - t0) * 1000)
            if resp.status_code != 200:
                print(f"第 {i} 轮 HTTP {resp.status_code}: {resp.text[:200]}")
                return 1
            d = resp.json()
            ans = (d["choices"][0]["message"].get("content") or "")
            messages.append({"role": "assistant", "content": ans})
            print(f"  轮{i} {ms:>6}ms  ← {u[:24]}")
            print(f"          → {ans[:40].replace(chr(10), ' ')}")

        # 等他抽完（抽取是 fire-and-forget 的后台任务）
        print("\n等后台抽取完成…")
        deadline = time.time() + 180
        while time.time() < deadline:
            with Store.open(db) as s:
                pend = s.pending_turn_count("demo")
                if pend == 0:
                    break
            time.sleep(5)
        wall = int((time.perf_counter() - t_all) * 1000)

        # ---- 验收：库里长出什么 ----
        with Store.open(db) as s:
            counts = s.counts()
            stats = s.run_stats("demo")
            detail = s.window_detail("demo")
            turns = s.get_turns("demo")
            evs = s.raws_of_session("demo")

        print("\n" + "=" * 62)
        print(f"代理全链路 {args.rounds} 轮，总耗时 {wall / 1000:.1f}s")
        print(f"落库 turns={counts['turns']}  events={counts['events']}  "
              f"entities={counts['entities']}  facts={counts['facts']}  "
              f"traits={counts['card_traits']}")
        print(f"抽取 {stats.get('attempted', 0)} 次，成功 {stats.get('ok', 0)}，"
              f"跳过 {stats.get('skipped', 0)}，成本 ¥{stats.get('cost', 0):.4f}，"
              f"缓存命中 {stats.get('cache_hit_rate', 0):.2%}")
        print(f"待抽 turn 剩余 {len([t for t in turns if t.extracted_at is None])}")
        print("\n记住了：")
        for e in evs:
            print(f"  · {e[:70]}")
        for f in detail["facts"]:
            print(f"  fact  {f['entity']}/{f['key']} = {f['value']}")
        for p in detail["plans"]:
            print(f"  plan  {p['content']}")

        ok = (counts["turns"] >= args.rounds * 2 - 1
              and counts["events"] >= 1
              and stats.get("ok", 0) >= 1)
        print("\n" + ("✅ 全链路通过" if ok else "❌ 全链路未通过"))
        Path("reports/data/live_proxy.json").parent.mkdir(parents=True, exist_ok=True)
        Path("reports/data/live_proxy.json").write_text(json.dumps({
            "rounds": args.rounds, "wall_ms": wall, "counts": counts,
            "stats": stats, "events": evs, "detail": detail,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if ok else 1
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()


if __name__ == "__main__":
    raise SystemExit(main())
