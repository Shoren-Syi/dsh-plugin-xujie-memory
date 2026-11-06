"""mimo 能力探针（M5.5 前置）。

在写适配层之前，用最少的钱问清楚四件事：
  1. `thinking: {"type": "disabled"}` 是否真的关掉了思考
  2. `response_format: {"type": "json_object"}` 是否被支持
  3. 缓存命中的字段名是什么（DeepSeek 是 prompt_cache_hit_tokens，
     mimo 未知；usage.prompt_tokens_details 在小样本里是 null）
  4. 真实抽取 prompt 有多少 token、要花多少钱、要等多久

不测做的事：不写库、不改配置、不碰 proxy。纯 HTTP。

用法：
    python3 -m tools.probe_mimo            # 全部四组
    python3 -m tools.probe_mimo --only cache
"""

from __future__ import annotations

import argparse
import json
import time

import httpx

from hippo.config import load_config
from hippo.core.schema import render_prompt

BASE = "https://api.xiaomimimo.com/v1"

# 一段真实的、有多样性的对话：有实体、有偏好、有事实、有计划、有情绪。
# 用它而不是"你好"，是因为 token 数必须反映真实分布。
CONVERSATION = """user: 我昨天下午搬家了，从朝阳搬到海淀，房租涨了两千，累死了
assistant: 搬完最累的是收拾吧，新家离公司近吗？
user: 近多了，走路二十分钟。对了老王说他下周要去深圳出差，让我帮他喂猫
assistant: 老王经常出差啊，喂猫这事你答应了吗？
user: 答应了，但我其实有点怕猫，从小被挠过一次
assistant: 那你还答应，真是够意思。
user: 没办法，他人挺好的。我下周三要交季度报告，还没开始写
assistant: 那这周得抓紧了。
user: 嗯，我这个人就是拖，每次都要到最后两天才动手
"""

KNOWN = "- 老王（别名：王哥）\n- 小李"


def _usage_brief(usage: dict) -> dict:
    """只留判断需要的字段，避免打印一大坨。"""
    keep = {}
    for k, v in (usage or {}).items():
        if "token" in k or "cache" in k:
            keep[k] = v
    return keep


def call(key: str, *, model: str, thinking: str | None, json_mode: bool,
         messages: list[dict], temperature: float = 0.2,
         max_tokens: int = 1500, tag: str = "") -> dict:
    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if thinking:
        payload["thinking"] = {"type": thinking}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    t0 = time.perf_counter()
    with httpx.Client(timeout=180) as c:
        r = c.post(f"{BASE}/chat/completions", json=payload,
                   headers={"authorization": f"Bearer {key}"})
    ms = int((time.perf_counter() - t0) * 1000)

    out = {"tag": tag, "status": r.status_code, "ms": ms, "thinking": thinking,
           "json_mode": json_mode, "model": model}
    if r.status_code >= 400:
        out["error"] = r.text[:400]
        return out
    d = r.json()
    msg = (d.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content")
    out["usage"] = _usage_brief(d.get("usage") or {})
    out["reasoning_chars"] = len(reasoning) if reasoning else 0
    out["content_chars"] = len(content)
    out["content_head"] = content[:120].replace("\n", " ")
    out["json_parse_ok"] = False
    try:
        json.loads(content)
        out["json_parse_ok"] = True
    except ValueError:
        # 带围栏也算失败——我们要的就是"直接给裸 JSON"
        pass
    out["events_n"] = None
    if out["json_parse_ok"]:
        try:
            out["events_n"] = len(json.loads(content).get("events") or [])
        except Exception:  # noqa: BLE001
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="probe / json / cache / long")
    ap.add_argument("--model", default="mimo-v2.6-flash")
    args = ap.parse_args()
    only = args.only

    cfg = load_config()
    key = cfg.deepseek.api_key
    if not key:
        print("配置里没有 api_key")
        return 2

    short = [{"role": "user", "content": "只回复两个字：收到"}]
    dump: list[dict] = []

    if not only or only == "probe":
        # 1. 思考开关：同一条短 prompt，对比开/关
        for t in ("disabled", "enabled"):
            dump.append(call(key, model=args.model, thinking=t, json_mode=False,
                             messages=short, max_tokens=200,
                             tag=f"thinking={t}"))

    if not only or only == "json":
        # 2. json_object：关思考 + json 模式，看能不能吐裸 JSON
        dump.append(call(key, model=args.model, thinking="disabled", json_mode=True,
                         messages=short, max_tokens=200, tag="json+no-think"))
        dump.append(call(key, model=args.model, thinking="enabled", json_mode=True,
                         messages=short, max_tokens=200, tag="json+think"))

    if not only or only == "long":
        # 3. 真实抽取 prompt（这是唯一花钱稍多的，约 0.003 元）
        prompt = render_prompt(CONVERSATION, known_entities=KNOWN,
                               now_hint="2026-11-04（周三）晚上")
        msg = [{"role": "user", "content": prompt}]
        dump.append(call(key, model=args.model, thinking="disabled", json_mode=True,
                         messages=msg, max_tokens=1500, tag="extract+no-think"))
        dump.append(call(key, model=args.model, thinking="enabled", json_mode=True,
                         messages=msg, max_tokens=3000, tag="extract+think"))
        print(f"prompt 字符数 = {len(prompt)}")

    if not only or only == "cache":
        # 4. 缓存：同前缀连发两次，看第二次的 usage 有没有 cache 字段
        prompt = render_prompt(CONVERSATION, known_entities=KNOWN,
                               now_hint="2026-11-04（周三）晚上")
        msg = [{"role": "user", "content": prompt}]
        for i in (1, 2):
            dump.append(call(key, model=args.model, thinking="disabled",
                             json_mode=True, messages=msg, max_tokens=800,
                             tag=f"cache-run{i}"))

    for row in dump:
        print(json.dumps(row, ensure_ascii=False))

    # 顺手算一下这轮探针自己花了多少钱
    total = 0.0
    for row in dump:
        u = row.get("usage") or {}
        pt = u.get("prompt_tokens") or 0
        ct = u.get("completion_tokens") or 0
        hit = u.get("prompt_cache_hit_tokens") or u.get("cache_hit_tokens") or 0
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        hit = max(hit, cached)
        total += ((pt - hit) * 1.0 + hit * 0.02 + ct * 2.0) / 1_000_000
    print(f"\n探针本轮花费 ≈ ¥{total:.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
