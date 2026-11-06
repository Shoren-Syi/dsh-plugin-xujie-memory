"""M27.2 成本核算：把"每轮多少钱"算成一个确定的数。

    python -m hippo.tools.cost_report --db ~/.hippo/mem.db --eval-json /tmp/eval26.json

## 口径（三个数，缺一个就会算出假账）

    输入 P   pack 之后整段上下文的 token（M26 实测 tok/turn）
    命中 C   DeepSeek 前缀缓存命中的部分，按 1/4 价
    输出 O   模型说的话

    cost = (P-C)×miss价 + C×hit价 + O×out价

## 为什么不直接用 metrics 里的 cost 列求和

那列是**代理记的账**，只覆盖对话请求；抽取是后台任务，成本只落在
`extract_runs`。而且它只回答"过去花了多少"，回答不了"一天聊 100 轮要多少"。
这里用同一套公式重算一遍，顺便校验两边的差——差超过 5% 就是记账口径变了。

## 缓存命中率为什么不是直接拿 metrics 的实测值

本环境的真机流量是 mimo，**不做前缀累积缓存**（R-13.3 三次对照证明），
实测 13.9% 是"系统提示固定"带来的偶然命中，不是 pack 的效果。
DeepSeek 官方的前缀缓存按 1/4 计价，典型命中 70%+；R-13.3 离线测出
我们的前缀稳定率 95%（结构上限）。所以默认取 0.72，并给出 0/0.5/0.95
的敏感性表——谁的上游命多少，自己对号入座。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from hippo.config import load_config
from hippo.core.store import Store

TIERS = [("轻度", 20), ("中度", 100), ("重度", 500)]
DAYS = 30


def _tokens_from_eval(path: str) -> float | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # 优先 hippo：json 里 full 排在前面，第一版直接取了 full（4412），
    # 算出来的成本是方案的 8 倍——这种错不会报错，只会让报告离谱。
    for want in ("hippo", "full"):
        for row in data:
            if row.get("mode") == want and row.get("tok_per_turn"):
                return float(row["tok_per_turn"])
    return None


def _usage(store: Store, session: str | None, since: int) -> dict[str, Any]:
    row = store.token_sum(since, session=session)
    return {"n": row["calls"], "p": row["prompt_tokens"],
            "o": row["completion_tokens"], "c": row["cached_tokens"],
            "cost": row["cost"]}


def _extract(store: Store, session: str | None, since: int) -> dict[str, Any]:
    row = store.extract_token_sum(since, session=session)
    return {"n": row["calls"], "p": row["prompt_tokens"],
            "o": row["completion_tokens"], "cost": row["cost"]}


def _cost(p: float, c: float, o: float, price: dict[str, float]) -> float:
    miss = max(0.0, p - c) * price["input_miss_per_m"] / 1e6
    hit = c * price["input_hit_per_m"] / 1e6
    out = o * price["output_per_m"] / 1e6
    return miss + hit + out


def _money(x: float) -> str:
    return f"¥{x:,.2f}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M27.2 成本核算")
    ap.add_argument("--db", default=str(Path.home() / ".hippo" / "mem.db"))
    ap.add_argument("--config", default="",
                    help="价格取自这份配置（默认用用户自己的 config.yaml）")
    ap.add_argument("--session", default="")
    ap.add_argument("--days", type=int, default=30, help="回看多少天的用量")
    ap.add_argument("--eval-json", default="", help="eval_all --json 的结果，取 tok/turn")
    ap.add_argument("--prompt-tokens", type=float, default=0,
                    help="每轮输入 token（默认 0 = 从 metrics 实测）")
    ap.add_argument("--completion-tokens", type=float, default=0,
                    help="每轮输出 token（默认 0 = 从 metrics 实测）")
    ap.add_argument("--cache-hit", type=float, default=0.72)
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    cfg = load_config(args.config or None)
    price = cfg.pricing.as_dict()
    store = Store.open(args.db, cfg)
    try:
        since = int(__import__("time").time()) - args.days * 86400
        chat = _usage(store, args.session or None, since)
        ext = _extract(store, args.session or None, since)
    finally:
        store.close()

    if not chat["n"]:
        print(f"没有可用的用量记录（{args.db}）。先聊几轮，或给 --prompt-tokens。")
        return 1

    n = chat["n"]
    p = args.prompt_tokens or chat["p"] / n
    o = args.completion_tokens or chat["o"] / n
    measured_hit = chat["c"] / chat["p"] if chat["p"] else 0.0

    eval_tokens = _tokens_from_eval(args.eval_json) if args.eval_json else None
    source = "metrics 实测"
    if eval_tokens:
        p = eval_tokens
        source = f"M26 实测 tok/turn（{args.eval_json}）"

    # 校验：用同一套公式重算历史账单，和代理记的 cost 比
    recomputed = _cost(chat["p"], chat["c"], chat["o"], price)
    drift = abs(recomputed - chat["cost"]) / chat["cost"] * 100 if chat["cost"] else 0.0

    per_turn = _cost(p, p * args.cache_hit, o, price)
    # 抽取摊销：用实测抽取成本 / 对话轮数；没有抽取记录就按配置估
    if ext["n"]:
        ext_per_turn = ext["cost"] / n
        ext_note = f"实测 {ext['n']} 次抽取 / {n} 轮"
    else:
        window = float(getattr(cfg.extract, "window_tokens", 1200))
        every = max(1, int(getattr(cfg.extract, "every_n_turns", 5)))
        ext_per_turn = _cost(window, window * args.cache_hit, 400, price) / every
        ext_note = f"无抽取记录，按 window={window:.0f} / 每 {every} 轮估算"

    print(f"样本 {n} 轮（近 {args.days} 天）  实测缓存命中 {measured_hit:.1%}")
    print(f"每轮输入 {p:.0f} token（{source}）  输出 {o:.0f} token")
    cache_save = 1 - (1 - args.cache_hit + args.cache_hit * price["input_hit_per_m"]
                      / price["input_miss_per_m"])
    print(f"缓存 {args.cache_hit:.0%} 命中 → 输入单价"
          f" ¥{price['input_miss_per_m'] * (1 - cache_save):.2f}/M"
          f"（比不缓存省 {cache_save:.0%}）")
    print(f"每轮 ¥{per_turn + ext_per_turn:.5f}"
          f" = 对话 ¥{per_turn:.5f} + 记忆写入 ¥{ext_per_turn:.5f}"
          f"（{ext_note}）")
    print(f"账单校验：重算 ¥{recomputed:.4f} vs 记账 ¥{chat['cost']:.4f}"
          f"  偏差 {drift:.2f}%")
    if drift > 5:
        print("  ↑ 账单是历史价目表记的，偏差来自价格而不是公式。"
              "用记账时的同一份 --config 重跑，偏差应为 0。")
    print()
    print(f"{'档位':<6}{'轮/天':>6}{'月 token':>12}{'月成本':>10}")
    print("-" * 36)
    rows = []
    for label, turns in TIERS:
        monthly_tokens = (p + o) * turns * DAYS
        cost = (per_turn + ext_per_turn) * turns * DAYS
        rows.append({"tier": label, "turns": turns,
                     "tokens": round(monthly_tokens), "cost": round(cost, 2)})
        print(f"{label:<6}{turns:>6}{monthly_tokens:>12,.0f}{_money(cost):>10}")
    print()
    print("缓存敏感性（中度 100 轮/天）")
    for h in (0.0, 0.5, args.cache_hit, 0.95):
        c = _cost(p, p * h, o, price) + ext_per_turn
        print(f"  命中 {h:4.0%}   {_money(c * 100 * DAYS)}/月")

    out = {"sample_turns": n, "prompt_tokens": round(p), "output_tokens": round(o),
           "cache_hit": args.cache_hit, "cache_saved": round(cache_save, 4),
           "per_turn": round(per_turn, 6), "extract_per_turn": round(ext_per_turn, 6),
           "drift_pct": round(drift, 2), "tiers": rows}
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
