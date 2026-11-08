"""检索 L3 原话层（M11）。

L2 给的是摘要，一条 80 token 封顶。摘要能回答"你有没有搬过家"，回答不了
"你当时是怎么说的"。后者要的是原文，而原文贵——贵到必须**有理由才拉**。

## 三道闸门，缺一不下钻（手册 M11.1）

    1. 该事件排进 top-2        —— 排在后面的，用户根本没看它在说什么
    2. 用户在问细节 / 事件被钉住 —— 没人问细节时拉原文是纯浪费
    3. 剩余预算 ≥ 200           —— 拉了装不下，等于没拉

三条都过了才动。这不是保守，是**成本可控的唯一办法**：原话层一旦敞开，
每轮多 300~500 token，按 20 轮/天算就是 1 万 token/天，比抽取还贵。

## 为什么"问细节"要看疑问语境（与手册的一处差异）

手册给的是 5 个子串：怎么说的 / 原话 / 具体 / 细节 / 当时。直接子串匹配会
把陈述句当成提问：

    "当时我不在家"        —— 在陈述，不是要原话
    "具体地址我忘了"      —— 在陈述，不是要细节

这类误触发不会报错，只会让原文悄悄多拉一次。所以标了两档：

    强标记  原话/一字不差/怎么说的/说了什么/具体说 —— 命中即触发
    弱标记  具体/细节/当时/怎么说/详细            —— 要和疑问语境同时出现

疑问语境的判定是"这句话在问东西"：？/吗/呢/什么/哪/怎么/为什么/告诉我。
"告诉我原话"命中强标记，不需要疑问词；"具体地址我忘了"啥都不命中。

代价写在明处：**"当时"开头的陈述句现在不触发了**（原来会）。如果哪天用户
抱怨"我说了当时它也不看原文"，把 `当时` 提到强标记里就行，一行的事。

## 为什么 raw 截到 400 token 而不是全给

一条 raw 真给全，可能是 800 token 的一段对话。原话层的价值在前 400 token
（事件本身说完），后面的通常是对话的余波（"嗯""然后呢"）。截断点加 "…"，
让模型知道后面还有、但没给——它就会说"你当时大概是这么说的"，而不是
把半句话当完整信息用。

## 预算怎么算

`memory_tokens(600) - L2 已用`。L2 的 `memory_budget_ratio=0.6` 只约束 L2
自己（360），L3 吃剩下的 240。真机上 L2 通常只用 48~100 token（摘要短），
所以 L3 实得 500 上下，够一条 400 token 的原文。

**手册里 240 装不下两条 312 的数字不自洽**，本轮实测 raw 平均只有 ~50 token
（我们的 raw 是抽取器锚定后的单句，不是整段窗口），所以 max_raws=2 反而
用得上。见 R-11-Gate 第二节。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .models import Event
from .rank import L2Result, MemoryItem, clip_tokens
from .turns import estimate_tokens

__all__ = [
    "DETAIL_STRONG", "DETAIL_WEAK", "DEPTHS",
    "DEPTH_KW", "DEPTH_SUMMARY", "DEPTH_RAW",
    "wants_detail", "format_event", "drill", "DrillResult", "SKIP_TEXT",
    "TOP_N", "RAW_MAX_TOKENS", "SUMMARY_MAX_TOKENS", "MIN_RAW_TAIL_TOKENS",
]

DEPTH_KW = "kw"
DEPTH_SUMMARY = "summary"
DEPTH_RAW = "raw"
DEPTHS = (DEPTH_KW, DEPTH_SUMMARY, DEPTH_RAW)

# 只下钻前 2 条。第 3 条之后的原文，用户问的已经不是它了。
TOP_N = 2

RAW_MAX_TOKENS = 400
SUMMARY_MAX_TOKENS = 80

# 第二条原文的准入门槛。第一条要过 `raw_min_budget`(200)，因为"这轮到底
# 值不值得拉原文"是个大决定；第一条拉完就已经证明值得了，第二条只是把
# 剩下的预算用在该用的地方，不该再过一次 200 的门。
#
# 为什么是 60 而不是更小：60 token ≈ 30 个汉字 ≈ 一句话。低于这个数，
# 原文只剩半句，信息量还不如一条 80 token 的摘要——那就让位给摘要。
MIN_RAW_TAIL_TOKENS = 60

# 命中即触发。
DETAIL_STRONG = (
    "原话", "一字不差", "原原本本", "怎么说的", "说了什么",
    "具体说", "详细说", "展开说", "原文",
)

# 要配合疑问语境才触发（见模块开头）。
DETAIL_WEAK = ("具体", "细节", "当时", "怎么说", "详细", "展开")

# "这句话在问东西"。
_QUESTION = (
    "?", "？", "吗", "呢", "什么", "哪", "几", "多少", "怎么",
    "为什么", "是否", "有没有", "告诉我", "说说", "讲讲",
)

_SRC = {"user": "用户", "ai": "AI", "third": "第三方"}

# ---------------------------------------------------------------- 档位（M16）
#
# 手册 §5.3 的三种注入块。**这是全项目唯一一处决定"一条记忆长什么样"的代码**，
# 装配（M13）、下钻（M11）、CLI 预览都调它。以前这三个地方各写一遍，
# 改一处忘两处，症状是"CLI 看着对，prompt 里不对"。
TIER_HOT, TIER_WARM, TIER_COLD = 1, 2, 3
_TIER_NAME = {1: "hot", 2: "warm", 3: "cold"}

# 单条记忆的 token 区间（手册 §5.3）。只当**上限**用：
# 下限不是约束——为了凑够 15 token 而给 cold 补话，是花钱买噪声。
TIER_BUDGET = {1: (60, 90), 2: (50, 70), 3: (15, 25)}

# 关键词条数上限。档位越冷给得越少：cold 一条 8 个词就 40 token，
# 直接顶穿 25 的预算。
KW_MAX = {1: 8, 2: 6, 3: 4}

# hot 档 pinned 追加的原文引一句就够。给全段 raw 会把一条记忆从 90 token
# 顶到 400——pinned 的意义是"必须在场"，不是"必须念全文"。
RAW_QUOTE_TOKENS = 40

# 有原文时，摘要少于这个额度就不给了（见 format_event）。
MIN_SUMMARY_WITH_RAW = 20

_LABEL = {DEPTH_KW: "关键词", DEPTH_SUMMARY: "记忆", DEPTH_RAW: "原话"}


def tier_name(e: Event) -> str:
    """事件的档位名。未知 tier 一律当 warm——宁可多给摘要，不要少给。"""
    try:
        t = int(e.tier)
    except (TypeError, ValueError):
        return "warm"
    return _TIER_NAME.get(t, "warm")


# ------------------------------------------------------------------ 判据
def wants_detail(text: str) -> bool:
    """这句话是不是在要细节。见模块开头"为什么看疑问语境"。"""
    t = unicodedata.normalize("NFKC", text or "")
    if not t:
        return False
    if any(m in t for m in DETAIL_STRONG):
        return True
    if not any(m in t for m in DETAIL_WEAK):
        return False
    return any(q in t for q in _QUESTION)


# ------------------------------------------------------------------ 格式
def _fmt_time(e: Event, tz: str) -> str:
    """按 `time_precision` 决定写到哪一位。

    写满 `%Y-%m-%d` 是在**编造精度**："去年冬天搬的家"被记成 2024-11-01，
    模型会拿它去算"过了多久"，误差半年。宁可写 `2024-11`。
    """
    try:
        dt = datetime.fromtimestamp(e.event_time, ZoneInfo(tz or "Asia/Shanghai"))
    except (OverflowError, OSError, ValueError):
        return "-"

    p = (e.time_precision or "day").lower()
    # fuzzy（M17.1）单独走一条路：人生阶段 / 事件锚的区间跨度从几天到几十年，
    # 通用格式会把"小时候"写成 "1999-01-01~2008-12-31?"——
    # **1 月 1 日这个日子是编的**，编出来的精度比粗粒度更糟。
    if p == "fuzzy":
        return _fmt_fuzzy(e, tz)

    allow_range = False
    if p == "minute":
        s = dt.strftime("%Y-%m-%d %H:%M")
    elif p == "hour":
        s = dt.strftime("%Y-%m-%d %H时")
    elif p == "month":
        s = dt.strftime("%Y-%m")
    elif p == "year":
        s = dt.strftime("%Y")
    else:
        s = dt.strftime("%Y-%m-%d")
        allow_range = p in ("day", "week")

    # 区间：只在"日"及更粗的粒度上显示，否则 "14:05~15:05" 会挤掉正文
    end = e.event_time_end
    if allow_range and end and end > e.event_time:
        try:
            d2 = datetime.fromtimestamp(end, ZoneInfo(tz or "Asia/Shanghai"))
        except (OverflowError, OSError, ValueError):
            d2 = None
        if d2 is not None:
            # 库里的 end 是**半开边界**（"11-04 这一天" = [11-04 09:00, 11-05 00:00)）。
            # 直接显示会写成 "11-04~11-05"，看起来是两天。落在 0 点的 end 减一秒，
            # 变成"最后一天"，再比较日期——这样"一天"不会被写成"两天"。
            if (d2.hour, d2.minute, d2.second) == (0, 0, 0):
                d2 -= timedelta(seconds=1)
            if d2.date() > dt.date():
                s += "~" + (d2.strftime("%m-%d") if d2.year == dt.year
                            else d2.strftime("%Y-%m-%d"))

    # 抽取器猜的时间打 ?：模型看到问号会加"大概"，看不到就会当事实断言。
    if e.time_guessed:
        s += "?"
    return s


def _fmt_fuzzy(e: Event, tz: str) -> str:
    """fuzzy 精度的展示：**宽度代替精度**。

    区间跨度 ≥2 年 → "约1999~2008"（人生阶段、事件锚）
    区间跨天     → "10-30~11-04"（"前几天"这类近期模糊）
    同一天       → 当普通日期

    "约"这个字是格式的一部分，不是修饰：它告诉模型这个时间只能用来
    算量级（"十年前"），不能拿来精确比较。
    """
    start = datetime.fromtimestamp(e.event_time, ZoneInfo(tz or "Asia/Shanghai"))
    end = e.event_time_end
    if not end or end <= e.event_time:
        s = start.strftime("%Y-%m-%d")
        return s + "?" if e.time_guessed else s

    d2 = datetime.fromtimestamp(end, ZoneInfo(tz or "Asia/Shanghai"))
    # 半开边界减一秒，"一天"不会被写成"两天"（与 _fmt_time 同一约定）
    if (d2.hour, d2.minute, d2.second) == (0, 0, 0):
        d2 -= timedelta(seconds=1)

    if d2.year - start.year >= 2:
        s = f"约{start.year}~{d2.year}"
    elif d2.date() > start.date():
        tail = (d2.strftime("%m-%d") if d2.year == start.year
                else d2.strftime("%Y-%m-%d"))
        s = start.strftime("%Y-%m-%d") + "~" + tail
    else:
        s = start.strftime("%Y-%m-%d")
    return s + "?" if e.time_guessed else s


def _one_line(text: str) -> str:
    """压成一行。原文里的换行会破坏注入格式（一条记忆变成两条）。"""
    return " ".join((text or "").split())


def _cut(text: str, limit: int) -> str:
    """截到 limit token 以内，截了就在末尾加 "…"。"""
    if limit <= 0:
        return "…"
    if estimate_tokens(text) <= limit:
        return text
    # 给省略号留 2 token（estimate_tokens("…") == 2）
    return clip_tokens(text, max(1, limit - 2)) + "…"


# `estimate_tokens` 带 1.3 倍安全系数（宁可高估，见 turns.py）。用于
# **窗口预算**时这个偏保守是对的；但手册 §5.3 的 60~90 token 说的是
# "模型看到的 token"，拿带安全系数的数去比，会让 hot 档只用掉 2/3 的额度。
# 所以内容预算统一换算成"真实 token"再比，换算系数就是那个 1.3。
_TOKEN_SAFETY = 1.3


def _real(text: str) -> float:
    return estimate_tokens(text) / _TOKEN_SAFETY


def _cut_real(text: str, limit: float) -> str:
    """按**真实 token** 截断。内部仍走 `_cut`（它按估算值工作）。"""
    if limit <= 0:
        return "…"
    if _real(text) <= limit:
        return text
    return _cut(text, max(2, int(limit * _TOKEN_SAFETY)))


def _values_line(e: Event) -> str:
    """关键值一行。单独给，不塞摘要——数字/日期是"以后要查得到"的东西。"""
    vals = getattr(e, "value_list", None) or []
    if not vals:
        return ""
    return "值：" + " ".join(str(v) for v in vals[:6])



def format_event(
    e: Event,
    depth: str | None = None,
    *,
    tz: str = "Asia/Shanghai",
    max_tokens: int | None = None,
    body: str | None = None,
    summary: str | None = None,
    tier: int | None = None,
) -> str:
    """一条记忆的注入文本。按 tier 分档，格式对齐手册 §5.3：

        [记忆 · 2026-03-14 · hot]
        关键词：花生 过敏 急救
        摘要：用户对花生严重过敏，曾过敏性休克送医，随身带肾上腺素笔。

        [记忆 · 2026-03-14 · hot]        ← pinned 时多一行引号原文
        关键词：花生 过敏 急救
        摘要：……
        原文："我对花生过敏，上次休克过"

        [记忆 · 2024-06 · cold]
        关键词：搬家 杭州

    `depth` 保留成显式覆盖，只给两个调用方用：
        DEPTH_RAW  下钻后强制带原文（M11 的 `drill` 已经决定"该拉"了）
        DEPTH_KW   预算最紧时只要关键词（不传 tier 时才会用到）
    不传 `depth` 就是"按 tier 自动分档"，这是正常路径。

    `body` 是**原文**（下钻拿到的），`summary` 是**摘要**（L2 裁剪过的）。
    两个分开是因为它们来源不同、上限也不同：把裁剪后的摘要当成原文塞进
    引号里，模型会以为那是用户的原话。

    ## 为什么 cold 只有一行、而 hot 有三行

    这不是审美，是成本。D 段 600 token 里通常要塞 8 条，平均 75 token。
    cold 事件的价值是"提醒有这么回事"，给它摘要等于用 54 token 换一个
    "大概是什么"——不如把这份预算让给 hot 档。

    ## 为什么来源（用户/AI/第三方）只在非用户时出现

    §5.3 的头部只有三个字段：`[记忆 · 时间 · tier]`。M11 的旧格式第四个
    字段恒为来源，本轮按 DoD「与 §5.3 完全一致」改成**默认不写**——
    用户自己说的话是绝大多数，每条多 4~6 token × 8 条 × 每轮不划算。

    但"恒定不写"会丢一个真信息：AI 自己说过的话和用户说的话混在一起，
    模型会把前者当后者用（"你上次说你想辞职" —— 那可能是它自己推测的）。
    所以非 `user` 的来源仍然追加：`[记忆 · 2026-03-14 · hot · AI]`。
    §5.3 的样例全是用户视角，这样它的三种格式仍然逐字一致。
    """
    if depth is not None and depth not in DEPTHS:
        raise ValueError(f"depth 只能是 {DEPTHS}，收到 {depth!r}")

    t = int(tier if tier is not None else (e.tier or 2))
    if t not in _TIER_NAME:
        t = 2
    head = f"[记忆 · {_fmt_time(e, tz)} · {_TIER_NAME[t]}]"
    src = (e.source or "user").lower()
    if src != "user":
        head = head[:-1] + f" · {_SRC.get(src, e.source)}]"

    kws = " ".join(e.keyword_list[:KW_MAX[t]])
    # 关键词一个都没有时退回摘要首句。空条目占着位置却不给信息，比不给更糟。
    kw_fallback = _one_line(e.summary or e.raw)[:24]

    # 要不要原文：下钻拿了（body 传了）、显式要（DEPTH_RAW）、或者是 pinned
    want_raw = body is not None or depth == DEPTH_RAW or bool(e.pinned)

    # ---- 最低成本档：只要关键词
    if depth == DEPTH_KW or (t == TIER_COLD and not want_raw):
        extra = _values_line(e)
        return f"{head}\n关键词：{kws or kw_fallback}" + (f"\n{extra}" if extra else "")

    if t == TIER_COLD:
        # 冷事件被下钻了：给原文，不给摘要。摘要要现裁，原文是现成的，
        # 而且用户问的就是"当时怎么说的"。
        lines = [head, f"关键词：{kws or kw_fallback}"]
        extra = _values_line(e)
        if extra:
            lines.append(extra)
        lines.append('原文："' + _quote_escape(_pick_raw(e, body, RAW_QUOTE_TOKENS)) + '"')
        return "\n".join(lines)

    # ---- hot / warm：关键词 + 摘要（+ pinned 原文）
    text = _one_line(summary if summary is not None else (e.summary or e.raw))
    # `max_tokens` 是**整块**的上限（真实 token）。M11 时它只卡正文，
    # 结果是"摘要刚好卡满 80 + 头部 + 关键词 = 110"，超出去的正是包边。
    # 关键值单独占额度：它不该抢摘要的空间（否则摘要被截、答案照样丢）。
    _extra = _values_line(e)
    cap = max_tokens if max_tokens is not None else TIER_BUDGET[t][1]
    if _extra:
        cap += _real(_extra)
    if e.pinned and max_tokens is None:
        # pinned 是用户明说"这个必须记住"的东西，为它多花 40 token 值得。
        # 上限不是无限的：pinned 数量由 M16.2 的 C 段 200 token 提醒兜住。
        cap += RAW_QUOTE_TOKENS
    used = _real(head) + _real(f"关键词：{kws or kw_fallback}")
    lines = [head, f"关键词：{kws or kw_fallback}"]
    if _extra:
        lines.append(_extra)
        used += _real(_extra)

    # 原文先按实际长度占额度，不是按固定 40 预留：一句 10 个字的原文
    # 被预留 40 token，摘要就被白挤掉 30。
    piece = ""
    raw_need = 0.0
    if want_raw:
        piece = _pick_raw(e, body, RAW_QUOTE_TOKENS)
        raw_need = _real('原文：""') + _real(piece)
        if used + raw_need > cap:
            # 原文太长，按剩余额度裁；裁完不到一句话就干脆不给。
            allow = cap - used - _real('原文：""')
            piece = _cut_real(piece, allow) if allow >= 8 else ""
            raw_need = (_real('原文：""') + _real(piece)) if piece else 0.0

    room = cap - used - raw_need - _real("摘要：")
    # 摘要在"有原文"时是次要的：原文才是用户当时说的话。给不到 20 token
    # （约 10 个汉字）就干脆不给——"用户下周从字…"这种半句比没有更糟，
    # 它会让模型以为摘要本身就残缺。
    if room >= (MIN_SUMMARY_WITH_RAW if piece else 10):
        lines.append("摘要：" + _cut_real(text, room))
    elif not want_raw:
        # 连摘要都装不下：退化成冷档格式（只有关键词），别硬塞一行"摘要：…"。
        # 占着位置却只写省略号，模型会以为这条记忆的摘要就是"不知道"。
        return "\n".join(lines)

    if piece:
        lines.append('原文："' + _quote_escape(piece) + '"')
    return "\n".join(lines)


def _pick_raw(e: Event, body: str | None, cap: float) -> str:
    """挑一段原文。`body` 是下钻已经裁好的，直接用；否则从 raw 现裁。

    `cap` 是**真实 token** 额度。`body` 传了就不再裁——drill 已经按剩余
    预算裁过一次，这里再裁一次，两处上限不一致时会出现"预算里装得下、
    注入的却是半句"。
    """
    if body is not None:
        return _one_line(body)
    text = _one_line(e.raw) or _one_line(e.summary)
    return _cut_real(text, cap)


def _quote_escape(text: str) -> str:
    """引号转义。原文里有 `"` 会把 `原文："…"` 的边界搞乱。

    规则只有一条：双引号一律变全角右引号。宁可少一个字符的准确性，
    也不要让模型看到一个没法解析的引号结构。
    """
    return (text or "").replace('"', "”").replace("“", "”")


# ------------------------------------------------------------------ 下钻
@dataclass
class DrillResult:
    """一次下钻的结果。`skipped` 带原因，是为了测试能断言"为什么没下钻"。"""

    drilled: list[MemoryItem] = field(default_factory=list)
    skipped: list[tuple[MemoryItem, str]] = field(default_factory=list)
    used: int = 0
    budget: int = 0
    detail: bool = False       # 这句话本身是不是在要细节

    @property
    def fired(self) -> bool:
        return bool(self.drilled)

    @property
    def rate_hint(self) -> str:
        if self.drilled:
            return "raw"
        return self.skipped[0][1] if self.skipped else "empty"


SKIP_TEXT = {
    "not_detail": "没问细节且没钉住",
    "degraded": "检索没命中，拉到的会是兜底结果",
    "max_raws": "已达 max_raws",
    "budget": "剩余预算不足",
    "no_raw": "无原文",
}


def drill(
    store: Any,
    res: L2Result,
    *,
    text: str = "",
    budget: int | None = None,
    tz: str = "Asia/Shanghai",
) -> DrillResult:
    """给 `res.items` 的前 2 条挂上原文（就地改 MemoryItem）。

    就地改而不是返回新对象：M13 装配时拿到的是同一份 items，
    如果这里返回副本，pack 那边要自己再对齐一次 id，多一处能错的地方。
    """
    cfg = getattr(store, "retrieve_cfg", None)
    max_raws = int(getattr(cfg, "max_raws", 2))
    raw_max = int(getattr(cfg, "raw_max_tokens", RAW_MAX_TOKENS))
    raw_min = int(getattr(cfg, "raw_min_budget", 200))
    pack_cfg = getattr(store, "pack_cfg", None)
    mem_tokens = int(getattr(pack_cfg, "memory_tokens", 600))

    if budget is None:
        # pinned 不受预算约束（M10 的 _fill），所以这里可能为负——
        # 负预算 = 已经超了，原文一条都不给，别让它雪上加霜。
        budget = max(0, mem_tokens - res.total_tokens)

    ask = text or (res.query.text if res.query is not None else "")
    out = DrillResult(budget=budget, detail=wants_detail(ask))
    left = budget

    for it in res.items[:TOP_N]:
        if not out.detail and not it.pinned:
            out.skipped.append((it, "not_detail"))
            continue
        # 关键词一个都没命中时，items 是按 weight 兜底的 8 条——它们和这句话
        # 没关系。给它们贴"原话"标签比不给更糟：模型会把无关的原文当成答案。
        # pinned 除外，钉住的事件本来就与"这次问什么"无关。
        if res.degraded and not it.pinned:
            out.skipped.append((it, "degraded"))
            continue
        if len(out.drilled) >= max_raws:
            out.skipped.append((it, "max_raws"))
            continue
        # 第一条过 200 的门，后续按"还装不装得下一句话"判（见 MIN_RAW_TAIL_TOKENS）
        gate = raw_min if not out.drilled else MIN_RAW_TAIL_TOKENS
        if left < gate:
            out.skipped.append((it, "budget"))
            continue
        raw = (it.event.raw or "").strip()
        if not raw:
            out.skipped.append((it, "no_raw"))
            continue

        cap = min(left, raw_max)
        piece = _cut(_one_line(raw), cap)
        it.raw_text = piece
        it.raw_tokens = estimate_tokens(piece)
        it.depth = DEPTH_RAW
        left -= it.raw_tokens
        out.used += it.raw_tokens
        out.drilled.append(it)

    return out
