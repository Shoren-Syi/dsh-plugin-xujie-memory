"""对比 thinking 开/关的抽取质量（M5.5）。

探针发现同样一段对话，关思考抽 5 条、开思考抽 9 条。
差 4 条是"漏抽"还是"多编"，决定默认用哪个模式，必须看清楚。

做法：同一窗口跑 N 次 × 两种模式，把完整输出存成 jsonl，
再用 anchor_raw 逐条校验 raw 锚定率 —— 这是"编没编"的客观判据。

用法：
    python3 -m tools.probe_quality --runs 2
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx

from hippo.config import load_config
from hippo.core.schema import anchor_raw, parse_result, render_prompt, ParseError

BASE = "https://api.xiaomimimo.com/v1"
OUT = Path("reports/data/probe_quality.jsonl")

# 三个风格不同的窗口：日常叙事 / 偏好密集 / 情绪密集。
# 单窗口调参没有意义，三种都要看。
WINDOWS = {
    "narrative": """user: 我昨天下午搬家了，从朝阳搬到海淀，房租涨了两千，累死了
assistant: 搬完最累的是收拾吧，新家离公司近吗？
user: 近多了，走路二十分钟。对了老王说他下周要去深圳出差，让我帮他喂猫
assistant: 老王经常出差啊，喂猫这事你答应了吗？
user: 答应了，但我其实有点怕猫，从小被挠过一次
assistant: 那你还答应，真是够意思。
user: 没办法，他人挺好的。我下周三要交季度报告，还没开始写
assistant: 那这周得抓紧了。
user: 嗯，我这个人就是拖，每次都要到最后两天才动手
""",
    "preference": """user: 我吃不了辣，一点都行不了，上次吃火锅辣得我眼泪直流
assistant: 那以后点菜注意点，微辣也别碰？
user: 微辣还行，中辣就不行了。另外我不喝冰的，胃不好
assistant: 记住了，常温的。
user: 还有我特别讨厌香菜，闻到就难受
assistant: 香菜确实争议大。
user: 对了我一般晚上十一点睡，别那个点找我
""",
    "emotion": """user: 今天被老板当众骂了一顿，说我方案做得像垃圾
assistant: 当着多少人？
user: 整个组十几个人都在。我当时脸都烧起来了，恨不得钻地缝
assistant: 那确实很难受。
user: 最气的是他自己上周还夸过这个方案，说方向对
assistant: 前后不一致确实让人窝火。
user: 我现在就想辞职，但房贷还有二十年
""",
}

KNOWN = "- 老王（别名：王哥）\n- 小李"


def run_one(key: str, model: str, conv: str, thinking: str, max_tokens: int) -> dict:
    prompt = render_prompt(conv, known_entities=KNOWN, now_hint="2026-11-04（周三）晚上")
    t0 = time.perf_counter()
    with httpx.Client(timeout=180) as c:
        r = c.post(f"{BASE}/chat/completions",
                   json={"model": model,
                         "messages": [{"role": "user", "content": prompt}],
                         "max_tokens": max_tokens, "temperature": 0.2,
                         "response_format": {"type": "json_object"},
                         "thinking": {"type": thinking}, "stream": False},
                   headers={"authorization": f"Bearer {key}"})
    ms = int((time.perf_counter() - t0) * 1000)
    if r.status_code >= 400:
        return {"ok": False, "err": r.text[:300], "ms": ms}
    d = r.json()
    content = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    # finish_reason 是判断"截断"的唯一可靠信号。completion_tokens == max_tokens
    # 时也可能是恰好用满，但 length 一定是被截。
    finish = (d.get("choices") or [{}])[0].get("finish_reason")
    u = d.get("usage") or {}
    return {"ok": True, "content": content, "ms": ms, "finish": finish,
            "pt": u.get("prompt_tokens") or 0,
            "ct": u.get("completion_tokens") or 0,
            "rt": (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0,
            "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
            "raw_usage": u}


def score(res: dict, conv: str) -> dict:
    """质量打分。只算客观量，不做主观判断。"""
    if not res.get("ok"):
        return {"parse": False, "n": 0, "anchor_ok": 0, "anchor_fail": 0}
    try:
        r = parse_result(res["content"])
    except ParseError as exc:
        return {"parse": False, "n": 0, "anchor_ok": 0, "anchor_fail": 0,
                "parse_err": str(exc)[:120]}
    ok = fail = 0
    for ev in r.events:
        (ok := ok + 1) if anchor_raw(ev.raw, conv) else (fail := fail + 1)
    return {"parse": True, "n": len(r.events), "anchor_ok": ok,
            "anchor_fail": fail, "facts": len(r.facts), "traits": len(r.traits),
            "plans": len(r.plans), "doubts": len(r.doubts),
            "kw": sum(len(e.keywords) for e in r.events),
            "kinds": [len(r.events), len(r.facts), len(r.traits),
                      len(r.plans), len(r.doubts)]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--model", default="mimo-v2.6-flash")
    args = ap.parse_args()

    cfg = load_config()
    key = cfg.deepseek.api_key
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = []

    # 预算闸：超出就停。9 块钱的家底，不能跑飞。
    spent = 0.0
    BUDGET = 1.0

    for name, conv in WINDOWS.items():
        for thinking in ("disabled", "enabled"):
            for i in range(args.runs):
                if spent > BUDGET:
                    print(f"⚠️ 已达预算闸 ¥{BUDGET}，停止")
                    break
                mt = 1500 if thinking == "disabled" else 3000
                res = run_one(key, args.model, conv, thinking, mt)
                s = score(res, conv) if res.get("ok") else {"parse": False, "n": 0}
                cost = ((res.get("pt", 0) - res.get("cached", 0)) * 1.0
                        + res.get("cached", 0) * 0.02
                        + res.get("ct", 0) * 2.0) / 1_000_000
                spent += cost
                row = {"window": name, "thinking": thinking, "run": i,
                       "content": res.get("content", ""),
                       "ms": res.get("ms"), "finish": res.get("finish"),
                       "pt": res.get("pt"), "ct": res.get("ct"),
                       "rt": res.get("rt"), "cached": res.get("cached"),
                       "cost": round(cost, 6), **s}
                rows.append(row)
                print(f"{name:<11} {thinking:<9} run{i} "
                      f"{res.get('ms', 0) / 1000:5.1f}s "
                      f"ev={s.get('n', 0):<2} anchor={s.get('anchor_ok', 0)}/"
                      f"{s.get('anchor_fail', 0)} "
                      f"f/t/p/d={s.get('facts', 0)}/{s.get('traits', 0)}/"
                      f"{s.get('plans', 0)}/{s.get('doubts', 0)} "
                      f"rt={res.get('rt', 0)} finish={res.get('finish')} "
                      f"¥{cost:.5f}")

    # 完整输出留档：调 prompt 时要能回头看它到底吐了什么
    with OUT.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n累计花费 ≈ ¥{spent:.5f}   明细 → {OUT}")
    for th in ("disabled", "enabled"):
        sub = [r for r in rows if r["thinking"] == th]
        if not sub:
            continue
        n = len(sub)
        tr = sum(r.get("rt", 0) for r in sub) / n
        print(f"  {th:<9} 均 events {sum(r.get('n', 0) for r in sub) / n:4.1f}  "
              f"锚定失败 {sum(r.get('anchor_fail', 0) for r in sub)}  "
              f"推理token {tr:6.0f}  "
              f"均耗时 {sum(r.get('ms', 0) for r in sub) / n / 1000:5.1f}s  "
              f"均成本 ¥{sum(r.get('cost', 0) for r in sub) / n:.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
