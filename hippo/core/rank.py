"""检索 L2：分数与截断（M10.2）。

L1 做减法（8000 → 几百），L2 做排序（几百 → 8）。这一层是全案唯一
"同时受相关性和重要性影响"的地方，所以它的每一个权重都会在 M26 被追问
"为什么是这个数"。

## 打分为什么不用"本次结果内归一化"

手册写的是 `0.55 × normalize(kw_hit)`。`normalize` 如果指"除以本次命中的
最大值"，同一条事件在不同候选集里会得到不同分数：

    查询 A 命中 {3,3,3}  → 第一条的 kw 分 = 1.0
    查询 B 命中 {3,3,1}  → 同一条的 kw 分 = 1.0
    查询 C 命中 {3}      → 还是 1.0，但它其实只命中了 1 个词

更要命的是**不可复现**：候选集稍微变一点（比如 cap 从 800 调到 801），
排序就可能翻。测试写不了，M26 调参也调不明白。

所以用一个饱和函数（D63）：

    kw_score = 1 - 0.5 ** hit

    1 个词 0.50  2 个 0.75  3 个 0.875  4 个 0.9375

单调、有上界、只依赖这一条事件自己。代价是"命中数和相关性不是线性关系"，
但那本来就是对的：命中 3 个词和命中 4 个词的区别，远小于 0 个和 1 个。

## 为什么保留"无关键词命中"的兜底

M9 量出来 21 条真机查询里有 16 条认不出实体，其中"我几点睡""我吃辣吗"
这类连关键词都命中不了（库里的关键词是"晚11点睡""不吃辣"）。
如果 L2 在没有命中时返回空，AI 会说"不记得"——而事实就在库里。

所以命中为 0 时按 weight + recency 兜底，并在结果上标 `degraded`。
这不是"降级"，是**承认这次检索没找到线索，那就按重要性给点东西**。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..log import get_logger
from .lexicon import MAX_QUERY_TOKENS, query_tokens
from .synonyms import expand as expand_synonyms
from .synonyms import to_simplified
from .models import Event
from .retrieve import L1Result, Query, l1_candidates, parse_query
from .turns import estimate_tokens

log = get_logger("rank")

__all__ = ["MemoryItem", "L2Result", "l2_search", "score_event", "explain",
           "clip_tokens"]

_TIER_NAME = {1: "hot", 2: "warm", 3: "cold"}


@dataclass
class MemoryItem:
    """一条要进 prompt 的记忆。`text` 已经按单条 token 上限截过。

    `raw_text` 只有 M11 下钻过才有值（空串 = 没下钻）。`depth` 是给 M13 装配
    用的：装了原文就是 raw，只有摘要是 summary，预算极紧时能退到 kw。
    """

    event: Event
    score: float
    kw_hit: int
    matched: list[str] = field(default_factory=list)
    text: str = ""
    tokens: int = 0
    pinned: bool = False
    raw_text: str = ""
    raw_tokens: int = 0
    depth: str = "summary"

    @property
    def id(self) -> int:
        return self.event.id

    @property
    def total_tokens(self) -> int:
        """这条记忆实际占的 token：摘要 + 原文（下钻过才有）。"""
        return self.tokens + self.raw_tokens


@dataclass
class L2Result:
    items: list[MemoryItem]
    candidates: int            # L1 给了多少条
    matched: int               # 关键词命中了多少条
    total_tokens: int
    elapsed_ms: float
    query: Query | None = None
    degraded: str = ""         # 非空 = 走了兜底，值说明原因
    budget: int = 0

    def __len__(self) -> int:
        return len(self.items)


# ------------------------------------------------------------------ 打分
def _recency(event: Event, now_ts: float, half_life: dict[str, float]) -> float:
    """指数衰减。半衰期按 tier 取（hot 3 天 / warm 30 天 / cold 180 天）。

    未来事件（`event_time > now`，抽取器把"明年"记成了事件时间）算 1.0
    而不是负数：它的"新近度"确实最高，只是时间不对。时间不对这件事
    M8.3 已经用 `time_guessed` 标了，不该在排序层再罚一次。
    """
    hl = float(half_life.get(_TIER_NAME.get(event.tier, "warm"), 30.0))
    age_days = max(0.0, (now_ts - event.event_time) / 86400.0)
    return 0.5 ** (age_days / hl)


def score_event(
    event: Event,
    *,
    kw_hit: int,
    now_ts: float,
    half_life: dict[str, float],
    w: dict[str, float],
    soft_since: int | None = None,
) -> float:
    """四因子加权（+ M9 留下的 soft_since）。"""
    kw = 1.0 - 0.5 ** kw_hit if kw_hit > 0 else 0.0
    rec = _recency(event, now_ts, half_life)
    pin = 1.0 if event.pinned else 0.0
    # M9 的 D61 把"最近"从硬过滤降级成软信号，这里就是它的落点。
    # 不实现这一项的话，D61 只是"少砍了一批候选"，排序上没有任何区别——
    # 那用户问"最近"和问"以前"会拿到同样的 8 条。
    since = 1.0 if (soft_since is not None and event.event_time >= soft_since) else 0.0
    return (w["keyword"] * kw
            + w["weight"] * float(event.weight)
            + w["recency"] * rec
            + w["pinned"] * pin
            + w["soft_since"] * since)


def clip_tokens(text: str, max_tokens: int) -> str:
    """把一段摘要截到 max_tokens 以内。按标点优先断，断不了才硬切。

    `estimate_tokens` 是故意偏高的估算，所以这里用二分而不是精确计数
    （精确计数要引入 tokenizer，纯 Python 的会慢 100 倍）。

    不负责加省略号：`clip_tokens` 只是"截"，加不加 "…" 由调用方决定，
    因为不同深度（原文/摘要）对"截了"的表达不一样。
    """
    if not text or estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    # 尽量在标点处断开，否则"…他辞职了因"这种半句会被当成完整信息
    for sep in ("。", "；", "，", ",", " "):
        i = cut.rfind(sep)
        if i >= len(cut) * 0.6:
            return cut[: i + 1]
    return cut


def _policy(store: Any, limit: int | None) -> dict[str, Any]:
    """把配置读成一份扁平策略。`getattr` 全在这里，l2_search 里不再出现。"""
    cfg = getattr(store, "retrieve_cfg", None)
    rank_cfg = getattr(store, "rank_cfg", None)
    pack_cfg = getattr(store, "pack_cfg", None)
    decay_cfg = getattr(store, "decay_cfg", None)
    memory_tokens = int(getattr(pack_cfg, "memory_tokens", 600))
    ratio = float(getattr(rank_cfg, "memory_budget_ratio", 0.6))
    return {
        "max_items": int(limit if limit is not None
                         else getattr(cfg, "max_summaries", 8)),
        "per_tokens": int(getattr(rank_cfg, "summary_max_tokens", 80)),
        "budget": int(memory_tokens * ratio),
        "weights": {
            "keyword": float(getattr(rank_cfg, "w_keyword", 0.55)),
            "weight": float(getattr(rank_cfg, "w_weight", 0.25)),
            "recency": float(getattr(rank_cfg, "w_recency", 0.10)),
            "pinned": float(getattr(rank_cfg, "w_pinned", 0.10)),
            "soft_since": float(getattr(rank_cfg, "w_soft_since", 0.05)),
        },
        "half_life": dict(getattr(decay_cfg, "half_life_days", {})
                          or {"hot": 3, "warm": 30, "cold": 180}),
    }


def _fill(ordered: list[MemoryItem], max_items: int,
          budget: int) -> tuple[list[MemoryItem], int]:
    """按分数装填。pinned 先占位，其余装到预算用完。"""
    picked: list[MemoryItem] = []
    used = 0
    for it in [i for i in ordered if i.pinned] + [i for i in ordered if not i.pinned]:
        if len(picked) >= max_items:
            break
        if it.pinned:
            # 钉住的不受预算约束。它不是"更重要的记忆"，是用户明确说
            # "这个必须记得"的东西；预算不够时该挤掉的是别的。
            picked.append(it)
            used += it.tokens
            continue
        if used + it.tokens > budget:
            continue          # 跳过，继续找能装下的短条目（不是 break）
        picked.append(it)
        used += it.tokens
    return picked, used


# ------------------------------------------------------------------ 主流程
def l2_search(
    store: Any,
    session: str,
    text: str = "",
    *,
    query: Query | None = None,
    limit: int | None = None,
    now: datetime | None = None,
    tz: str = "Asia/Shanghai",
    candidates: list[int] | None = None,
) -> L2Result:
    """L2：L1 候选 → 关键词打分 → top-N。

    `candidates` 可以外部传入（M12 话题切换已经算过一次 L1 时复用），
    不传就自己调 L1。
    """
    t0 = time.perf_counter()
    pol = _policy(store, limit)
    max_items, per_tokens = pol["max_items"], pol["per_tokens"]
    budget, weights, half_life = pol["budget"], pol["weights"], pol["half_life"]

    q = query
    if q is None:
        q = parse_query(text, session, store, now=now, tz=tz)
    if candidates is None:
        l1: L1Result | None = l1_candidates(store, session, q, now=now, tz=tz)
        cand = l1.event_ids
    else:
        l1 = None
        cand = candidates

    now_ts = (now or datetime.now()).timestamp()
    qtext = q.text

    # ---- 关键词命中
    # 同义词排在 n-gram 前面：它们更可能正好是库里的词。
    # 截断时优先丢 n-gram（垃圾片段本来就命中不了，丢了不心疼）。
    # 先转简体再分词：库里的关键词是抽取器写的，全是简体。
    # 用户拿繁体问（"臺灣""這個"）时，字面重叠一个都没有，倒排全空。
    plain = to_simplified(qtext)
    toks = list(dict.fromkeys(
        [*expand_synonyms(plain), *query_tokens(plain)]))[:MAX_QUERY_TOKENS]
    hits: dict[int, tuple[int, list[str]]] = {}
    if toks and cand:
        rows = store.kw_search(
            session, toks, limit=max(40, max_items * 4),
            candidates=cand, include_historical=q.include_historical)
        hits = {ev.id: (h, m) for ev, h, m in rows}

    # ---- 钉住的必须进（手册验收 #4）
    pinned = store.list_pinned(session, include_historical=q.include_historical)
    pinned = [e for e in pinned if not cand or e.id in set(cand)]

    degraded = ""
    pool: list[tuple[Event, int, list[str]]] = []
    if hits:
        evs = store.fetch_events(
            list(hits), session, include_historical=q.include_historical)
        pool = [(e, *hits[e.id]) for e in evs]
    if not hits:
        # 没有关键词命中：按 weight/recency 兜底（见模块开头）。
        # 注意这里**不做全库扫描**——只在 L1 候选集里挑，候选集已经是
        # "这句话可能相关的那几百条"，比全库可靠。
        degraded = "no_keyword_hit" if toks else "empty_query"
        pool = [(e, 0, []) for e in store.top_events(
            session, cand, limit=max_items * 2,
            include_historical=q.include_historical)]

    scored: dict[int, MemoryItem] = {}
    for ev, h, m in pool:
        s = score_event(ev, kw_hit=h, now_ts=now_ts, half_life=half_life,
                        w=weights, soft_since=q.soft_since)
        text_clip = clip_tokens(ev.summary or ev.raw, per_tokens)
        scored[ev.id] = MemoryItem(
            event=ev, score=round(s, 6), kw_hit=h, matched=m,
            text=text_clip, tokens=estimate_tokens(text_clip),
            pinned=bool(ev.pinned))
    for ev in pinned:
        if ev.id in scored:
            scored[ev.id].pinned = True
            continue
        text_clip = clip_tokens(ev.summary or ev.raw, per_tokens)
        scored[ev.id] = MemoryItem(
            event=ev, score=round(score_event(
                ev, kw_hit=0, now_ts=now_ts, half_life=half_life,
                w=weights, soft_since=q.soft_since), 6),
            kw_hit=0, matched=[], text=text_clip,
            tokens=estimate_tokens(text_clip), pinned=True)

    # 排序：分数 → 时间新 → id 大。后两级保证**同输入同输出**（验收 #2），
    # 没有它们，同分事件的顺序取决于 SQLite 的返回顺序，测试会随机红。
    ordered = sorted(
        scored.values(),
        key=lambda it: (-it.score, -it.event.event_time, -it.event.id))

    picked, used = _fill(ordered, max_items, budget)

    return L2Result(
        items=picked, candidates=len(cand), matched=len(hits),
        total_tokens=used, elapsed_ms=(time.perf_counter() - t0) * 1000,
        query=q, degraded=degraded, budget=budget,
    )


# ------------------------------------------------------------------ 调试
def explain(res: L2Result, drilled: Any = None) -> str:
    """一行解释 L2 干了什么。CLI `recall` 用它。

    `drilled` 传 M11 的 DrillResult 时，把原文一起打出来——
    "为什么它引用了这句话"和"它引用的到底是我哪句话"是两个问题。
    """
    q = res.query
    head = (f"候选 {res.candidates} 条 → 命中 {res.matched} 条 → "
            f"装 {len(res.items)} 条 / {res.total_tokens} token（预算 {res.budget}）"
            f"  {res.elapsed_ms:.2f}ms")
    if res.degraded:
        head += f"\n降级：{res.degraded}"
    if q is not None and q.soft_since:
        head += f"\n软下限：{datetime.fromtimestamp(q.soft_since):%Y-%m-%d}（只加权）"
    if drilled is not None:
        head += (f"\n原文层：{len(drilled.drilled)} 条 / {drilled.used} token"
                 f"（可用 {drilled.budget}）")
    lines = [head]
    for i, it in enumerate(res.items, 1):
        pin = "📌" if it.pinned else "  "
        why = f"kw={it.kw_hit}"
        if it.matched:
            why += f"({','.join(it.matched[:3])})"
        lines.append(f"{pin}{i}. [{it.score:.3f} {why}] {it.text}")
        if it.raw_text:
            lines.append(f'      原话 "{it.raw_text}"')
    return "\n".join(lines)
