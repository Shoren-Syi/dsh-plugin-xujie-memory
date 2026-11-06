# -*- coding: utf-8 -*-
"""M26.2 · 六项指标，一条命令。

    python -m hippo.tools.eval_all --session eval-200 --report
    python -m hippo.tools.eval_all --mode none
    python -m hippo.tools.eval_all --mode full

## 先说清楚这套评测在测什么

**它测的不是模型，是记忆通路。** 环境里没有可调用的大模型（预算外），
所以"抽取"用 gold：语料自己标了 50 条针，评测按标注直接写库。
这样做的代价是抽不出来——那是 M5 的事，M26 不管；好处是**变量只剩一个**：
同样的记忆，能不能在需要的时候被找回来。

"召回率"因此定义为**上下文可达率**：第 200 轮后逐针提问，走正常 pack，
答案值是否出现在这段上下文里。它不是"模型答对率"，但它是答对率的**上界**——
上下文里没有的东西，任何模型都答不出来。

## 三种模式的成本口径

手册的示意表里 none=1.0x、hippo=1.15x，暗含的基准是"一次普通对话"。
这里实测口径不同，必须写明白，否则数字对不上会被当成 bug：

    cost_vs_none = 每轮上下文 token / none 模式每轮 token
    cost_vs_full = 每轮上下文 token / full 模式每轮 token   ← 主指标

主指标用 `cost_vs_full`：DoD 写"≤15% 基线"，M26.3 写"C ≤ B 的 1/5"，
两处都指向"相对全量重读"。`full` 全量重读在 200 轮规模下是几万 token，
而 pack 恒定在 1k 上下——这个比值才是"省了多少"的真实答案。

## TTFT 口径

真实首字延迟要打模型，测不了。这里测**装配耗时**（pack 的 p50 毫秒），
它正是代理相对直连的可归因增量：网络和模型时间两边一样，差的就是这一坨。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hippo.config import load_config  # noqa: E402
from hippo.core.db import connect  # noqa: E402
from hippo.core.migrate import migrate  # noqa: E402
from hippo.core.pack import _persona, pack  # noqa: E402
from hippo.core.persist import persist_result  # noqa: E402
from hippo.core.schema import (  # noqa: E402
    DoubtOut, EntityRef, EventOut, ExtractResult, FactOut, PlanOut,
)
from hippo.core.store import Store  # noqa: E402
from hippo.core.turns import Window  # noqa: E402
from hippo.core.turns import estimate_tokens  # noqa: E402

FIXTURE = Path("tests/fixtures/dialog_200.jsonl")
MODES = ("none", "full", "hippo")
FULL_CAP = 8000          # full 模式的"超限截断"上限（token）
NONE_TURNS = 5           # none 模式只带最近 5 条

# gold 疑问：语料里这几轮指代不明，真实抽取器会在这里提疑问。
# 写死是为了让 ask_rate 有分母可算——不标的话它恒等于 0，
# 而"0 次追问"和"从不追问"是两件完全不同的事。
GOLD_DOUBTS: dict[int, str] = {
    61: "「那边」指的是哪家公司？",
    106: "「那个手术」是白内障手术吗？",
    125: "阿May 住的酒店在哪个区？",
    138: "「大福」是狗的名字吗？",
    196: "「老王那边」是指他给的那个机会吗？",
}


# ------------------------------------------------------------------ 归一化
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
_PUNCT = " \t\n，。、；：？！“”‘’（）()《》—…·,.;:?!\"'"


_CN_NUM = re.compile(r"[零一二两三四五六七八九十]+")


def _cn_piece(s: str) -> str:
    """单个中文数字串 → 阿拉伯数字（≤99）。

    "三十"→30、"十四"→14、"二十三"→23。再大的数（"一百五"）不处理：
    语料里最长的就是"二十万"，而"二十万"会被切成"二十"+"万"，
    得到 "20万" —— 两边都这样切，比对仍然成立。
    """
    if "十" not in s:
        return "".join(str(_CN_DIGIT[c]) for c in s if c in _CN_DIGIT)
    if s.count("十") == 1:
        a, _, b = s.partition("十")
        tens = _CN_DIGIT.get(a, 1) if a else 1
        ones = _CN_DIGIT.get(b, 0) if b else 0
        return str(tens * 10 + ones)
    return s


def _cn2num(text: str) -> str:
    """把 ≤99 的中文数字换成阿拉伯数字。

    不换的话 `3月14` 和 `三月十四` 是两个字符串，评测会把"答对"记成"没答"。
    这个坑很隐蔽：错的是评测器，看起来却像系统记不住。
    """
    return _CN_NUM.sub(lambda m: _cn_piece(m.group(0)), text)


def norm(text: str) -> str:
    s = _cn2num(text or "")
    s = "".join(c for c in s if c not in _PUNCT)
    return s.lower()


# ------------------------------------------------------------------ gold 抽取
def _keywords(pin: dict) -> list[str]:
    """关键词取语料标注的 `kw`，不是摘要切分。

    第一版直接拿摘要按逗号劈开，得到的是 "我挺喜欢吃川菜" 这种**整句**。
    检索打分对关键词是二值命中（1-0.5^hits），整句和查询永远碰不上，
    实测 score 从 0.47 掉到 0.20 —— 50 条记忆里 26 条捞不回来。
    真实抽取器给的就是 2~4 个短词，这里必须一样，否则测的是
    "关键词写得好不好"，不是"记忆通路通不通"。
    """
    kw = str(pin.get("kw", "")).split()
    if kw:
        return kw
    val = str(pin.get("value", ""))
    return [w for w in val.replace("，", " ").split() if w][:3] or [str(pin.get("a", ""))]


def _entity_of(pin_id: str, pin: dict) -> EntityRef | None:
    """关系类针要带实体，否则检索时只剩一句没有主语的摘要。"""
    val = str(pin.get("value", ""))
    # 别名要覆盖用户的口语说法："我妹"和"妹妹"是同一个人。
    # 不做这一步，P42（妹妹结婚）的实体会被漏掉，L1 按实体过滤时
    # 直接把它排除在候选外——检索层再好也捞不回来。
    pairs = (("老王", "person"), ("小李", "person"), ("张姐", "person"),
             ("阿May", "person"), ("陈医生", "person"), ("舅舅", "person"),
             ("妹妹", "person"), ("我妹", "person"), ("妈妈", "person"),
             ("我妈", "person"), ("爸爸", "person"), ("我爸", "person"),
             ("豆豆", "thing"), ("大福", "thing"), ("邻居", "person"))
    for name, typ in pairs:
        if name in val:
            canon = {"我妹": "妹妹", "我妈": "妈妈", "我爸": "爸爸"}.get(name, name)
            return EntityRef(name=canon, type=typ)
    return None


def gold_result(pin: dict, text: str, turn: int) -> ExtractResult | None:
    kind = pin["kind"]
    if kind == "fact":
        return ExtractResult(facts=[FactOut(
            key=pin["key"], value=pin["value"], entity="用户", confidence=0.85)])
    if kind == "plan":
        # 计划和事件**同时**产出，这是真实抽取器的样子：
        # "我下个月要搬家"既是一条未来计划（要定时提醒），也是一件
        # 说过的事（用户问"我什么时候搬家"要答得出来）。
        # 只写 plans 的话，计划表只有"到点注入"一条出口（pack 只注入
        # 10 分钟内 fired 或已到点的），远期计划平时根本检索不到——
        # 实测 8 条计划里 7 条召回失败。
        return ExtractResult(
            plans=[PlanOut(content=pin["value"], due_hint=_due_hint(pin))],
            events=[EventOut(raw=text, summary=pin["value"],
                             keywords=_keywords(pin),
                             entities=[e] if (e := _entity_of("", pin)) else [])])
    if kind in ("rel", "pref", "seed"):
        ent = _entity_of("", pin)
        return ExtractResult(events=[EventOut(
            raw=text, summary=pin["value"], keywords=_keywords(pin),
            entities=[ent] if ent else [])])
    return None


def _due_hint(pin: dict) -> str:
    return {
        "P38": "下个月十号", "P39": "年底", "P40": "十月",
        "P41": "", "P42": "明年五月", "P43": "下周三",
        "P44": "下个月", "P45": "两年后",
    }.get(str(pin.get("id", "")), "")


# ------------------------------------------------------------------ 上下文
def _recent(store: Store, session: str, n: int) -> str:
    rows = store.get_turns(session, limit=n)
    return "\n".join(f"{r.role}: {r.content}" for r in rows)


def _clip_head(text: str, cap: int) -> str:
    """从**头部**砍。全量重读的截断一定是砍最早的——这和真实实现一致。"""
    if estimate_tokens(text) <= cap:
        return text
    lines = text.splitlines()
    while lines and estimate_tokens("\n".join(lines)) > cap:
        lines.pop(0)          # 砍最早的一行
    return "\n".join(lines)


def clone_store(store: Store, cfg: Any) -> Store:
    """把库复制一份，给单个问题用。

    提问阶段**每个问题必须从同一份状态出发**。第一版没做隔离，50 个问题
    在同一个库上连着跑：每次 pack 都会 touch_events 抬权重、写 last_pack_d，
    于是第 19 个问题看到的是"被前 18 个问题改动过"的记忆。实测 P19
    （我爸现在做什么）单独问命中，排队问到就漏——而它跟问题本身毫无关系。

    这种偏差最坏的地方是它稳定：重跑一百次结果一样，看着"可重复"，
    其实测的是提问顺序。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row      # 和 db.connect() 一致，否则 row["x"] 全崩
    store.conn.backup(conn)
    s = Store(conn)
    s.attach_config(cfg)
    return s


def build_context(mode: str, store: Store, session: str, msg: str,
                  now: datetime, cfg: Any) -> tuple[str, int, float]:
    """返回 (上下文, token, 耗时毫秒)。三条路径唯一的差别就在这个函数里。"""
    t0 = time.perf_counter()
    # 三种模式都带人设。基线必须包含"每轮都必须付的那部分"，
    # 否则算出来的倍数没有可比性：只带 5 轮原始对话的 none 是 195 token，
    # 而 pack 里光人设就 300——拿这两个比，等于说"不装插件就不用写系统提示"。
    # 手册示例表里 none=1.0x / hippo=1.15x 正是"人设 + 历史"的口径。
    persona = _persona(cfg)
    if mode == "none":
        text = persona + "\n" + _recent(store, session, NONE_TURNS)
    elif mode == "full":
        rows = store.get_turns(session, limit=2000)
        body = _clip_head("\n".join(f"{r.role}: {r.content}" for r in rows), FULL_CAP)
        text = persona + "\n" + body
    else:
        text = pack(store, session, msg, cfg=cfg, now=now,
                    use_cache=False).text
    ms = (time.perf_counter() - t0) * 1000
    return text, estimate_tokens(text), ms


# ------------------------------------------------------------------ 指标
@dataclass
class RunResult:
    mode: str
    recall: float = 0.0
    hits: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    contra: float = 0.0
    tokens_per_turn: float = 0.0
    tokens_total: int = 0
    ttft_p50: float = 0.0
    ttft_p95: float = 0.0
    ask_per_10: float = 0.0
    asked: int = 0
    turns: int = 0
    by_kind: dict[str, list[int]] = field(default_factory=dict)


def run(mode: str, *, fixture: Path = FIXTURE, cfg: Any = None,
        verbose: bool = False) -> RunResult:
    cfg = cfg or load_config()
    rows = [json.loads(line) for line in
            fixture.read_text(encoding="utf-8").splitlines() if line.strip()]
    pins = json.loads(fixture.with_suffix(".pins.json").read_text(encoding="utf-8"))

    conn = connect(":memory:")
    migrate(conn)
    store = Store(conn)
    store.attach_config(cfg)
    session = "eval-200"

    # 时间轴：最后一轮落在"现在"，往前每轮一小时。用虚拟时间而不是
    # 墙钟，是为了让 200 轮在 8 天里自然演进（计划会到点、疑问会过期）。
    now_end = time.time()
    t0 = now_end - len(rows) * 3600

    first_turn = {pid: m["first_turn"] for pid, m in pins.items()}
    pin_at: dict[int, list[str]] = {}
    for pid, ft in first_turn.items():
        pin_at.setdefault(ft, []).append(pid)

    res = RunResult(mode=mode, turns=len(rows))
    lat: list[float] = []
    toks: list[int] = []

    for r in rows:
        ts = int(t0 + r["turn"] * 3600)
        store.add_turns(session, [(r["role"], r["text"], 0)], ts=ts)
        now = datetime.fromtimestamp(ts)

        # 只有 hippo 写记忆。none/full 是"没有记忆"和"只有原始历史"。
        if mode == "hippo" and r["role"] == "user":
            for pid in pin_at.get(r["turn"], []):
                pin = {**pins[pid], "id": pid}
                gr = gold_result(pin, r["text"], r["turn"])
                if gr is None:
                    continue
                turns = store.get_turns(session, limit=4)
                w = Window(session=session, idxs=[t.idx for t in turns],
                           token_est=0, has_pending=False, turns=turns)
                persist_result(store, session, w, gr, now=ts)
            if r["turn"] in GOLD_DOUBTS:
                turns = store.get_turns(session, limit=4)
                w = Window(session=session, idxs=[t.idx for t in turns],
                           token_est=0, has_pending=False, turns=turns)
                persist_result(store, session, w, ExtractResult(
                    doubts=[DoubtOut(question=GOLD_DOUBTS[r["turn"]],
                                     confidence=0.4, impact=0.6, cost=0.3)]),
                    now=ts)

        text, tok, ms = build_context(mode, store, session, r["text"], now, cfg)
        toks.append(tok)
        lat.append(ms)
        if mode == "hippo" and ("[存疑]" in text or "[想问]" in text):
            res.asked += 1
        if verbose and r["turn"] % 50 == 0:
            print(f"  ...{r['turn']}/{len(rows)} tok={tok} {ms:.1f}ms")

    # ---- 逐针提问（每条一个克隆库，互不影响）
    q_now = datetime.fromtimestamp(now_end)
    for pid in sorted(pins):
        pin = pins[pid]
        probe = clone_store(store, cfg)
        ctx, _, _ = build_context(mode, probe, session, pin["q"], q_now, cfg)
        ok = norm(pin["a"]) in norm(ctx)
        if os.environ.get("HIPPO_EVAL_DEBUG") == pid:
            from hippo.core.topic import should_retrieve
            from hippo.core.rank import l2_search
            print(f"[debug] {pid} q={pin['q']} a={pin['a']} hit={ok}")
            print("  trig:", should_retrieve(probe, session, pin["q"]))
            print("  l2:", [(round(i.score, 3), i.event.summary[:20])
                            for i in l2_search(probe, session, pin["q"]).items[:3]])
            print("  ctx_tail:", repr(ctx[-300:]))
        probe.close()
        res.by_kind.setdefault(pin["kind"], [0, 0])
        res.by_kind[pin["kind"]][0 if ok else 1] += 1
        (res.hits if ok else res.misses).append(pid)

    res.recall = len(res.hits) / max(1, len(pins))
    res.tokens_total = sum(toks)
    res.tokens_per_turn = res.tokens_total / max(1, len(toks))
    res.ttft_p50 = statistics.median(lat)
    res.ttft_p95 = statistics.quantiles(lat, n=20)[-1] if len(lat) > 20 else max(lat)
    res.ask_per_10 = res.asked / max(1, len(rows)) * 10
    res.contra = _contradiction(store, session)
    store.close()
    return res


def _contradiction(store: Store, session: str) -> float:
    """同一 key 同时 active 的比例。

    只在 `card=single` 的 key 上有定义——`diet` 可以有五种忌口，
    它同时 active 五条不是矛盾，是正确。所以分母也只算 single 的 key。
    """
    counts = store.fact_active_counts(session)
    if not counts:
        return 0.0
    from hippo.core.store import fact_keys
    table = fact_keys()
    single = 0
    bad = 0
    for key, n in counts.items():
        k = table.get(key)
        if k is None or getattr(k, "card", "single") != "single":
            continue
        single += 1
        if n > 1:
            bad += 1
    return bad / single if single else 0.0


def idle_cost() -> int:
    """挂机 24 小时的模型调用数。用虚拟时钟跑 1440 轮，不真等。"""
    from hippo.core.heartbeat import Heartbeat

    conn = connect(":memory:")
    migrate(conn)
    store = Store(conn)
    cfg = load_config()
    hb = Heartbeat(store, cfg=cfg)
    base = int(time.time())
    for i in range(1440):
        hb.tick(now=base + i * 60)
    n = hb.counts.get("llm_calls", 0)
    store.close()
    return n


# ------------------------------------------------------------------ 输出
def table(results: list[RunResult]) -> str:
    head = (f"{'mode':<7}{'recall':>8}{'contra':>9}{'tok/turn':>10}"
            f"{'cost':>8}{'ttft_p50':>10}{'ask/10':>9}")
    lines = [head, "-" * len(head)]
    base = next((r for r in results if r.mode == "none"), results[0])
    for r in results:
        lines.append(
            f"{r.mode:<7}{r.recall:>8.3f}{r.contra:>9.3f}"
            f"{r.tokens_per_turn:>10.0f}"
            f"{r.tokens_per_turn / max(1, base.tokens_per_turn):>7.2f}x"
            f"{r.ttft_p50:>9.0f}ms{r.ask_per_10:>9.2f}")
    return "\n".join(lines)


def detail(res: RunResult, pins: dict, cap: int = 12) -> str:
    lines = [f"[{res.mode}] 命中 {len(res.hits)}/{len(res.hits) + len(res.misses)}"]
    by = []
    for kind, (hit, miss) in sorted(res.by_kind.items()):
        by.append(f"{kind} {hit}/{hit + miss}")
    lines.append("  分类：" + "  ".join(by))
    if res.misses:
        lines.append("  漏：" + " ".join(
            f"{pid}({pins[pid]['q']})" for pid in res.misses[:cap]))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="M26.2 六项指标")
    ap.add_argument("--mode", choices=MODES, default="hippo")
    ap.add_argument("--session", default="eval-200")
    ap.add_argument("--fixture", default=str(FIXTURE))
    ap.add_argument("--all", action="store_true", help="三种模式都跑，出对照表")
    ap.add_argument("--report", action="store_true", help="出完整表 + 明细")
    ap.add_argument("--idle", action="store_true", help="顺带跑 24h 空转验证")
    ap.add_argument("--json", metavar="PATH", default=None, help="把结果写成 json")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    pins = json.loads(Path(args.fixture).with_suffix(".pins.json")
                      .read_text(encoding="utf-8"))
    modes = list(MODES) if args.all else [args.mode]
    results = []
    for m in modes:
        t = time.perf_counter()
        r = run(m, fixture=Path(args.fixture), cfg=cfg, verbose=args.verbose)
        results.append(r)
        if not args.all:
            print(detail(r, pins))
        if args.verbose:
            print(f"  {m} 用时 {time.perf_counter() - t:.1f}s")

    print()
    print(table(results))

    if args.report:
        print()
        tail = results[-1]
        full = next((r for r in results if r.mode == "full"), results[-1])
        print(f"recall={tail.recall:.2f} contradiction={tail.contra:.3f} "
              f"cost_ratio={tail.tokens_per_turn / max(1.0, full.tokens_per_turn):.3f} "
              f"ttft_delta={tail.ttft_p50:.0f}ms "
              f"ask_rate={tail.ask_per_10:.1f} idle_cost={idle_cost() if args.idle else 0}")
        print(f"（cost_ratio = 本方案每轮 token / 全量重读每轮 token；"
              f"相对不装插件为 {tail.tokens_per_turn / max(1.0, results[0].tokens_per_turn):.2f}x）")

    if args.json:
        Path(args.json).write_text(json.dumps([
            {"mode": r.mode, "recall": round(r.recall, 4),
             "contra": round(r.contra, 4),
             "tok_per_turn": round(r.tokens_per_turn, 1),
             "ttft_p50": round(r.ttft_p50, 1),
             "ttft_p95": round(r.ttft_p95, 1),
             "ask_per_10": round(r.ask_per_10, 2),
             "hits": r.hits, "misses": r.misses,
             "by_kind": r.by_kind}
            for r in results], ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
