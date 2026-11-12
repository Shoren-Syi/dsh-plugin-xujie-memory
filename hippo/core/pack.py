"""上下文装配（M13）—— 全场唯一的出口。

记忆、预算、注入格式、顺序，全部只在这一个文件里。调"AI 记性好不好"
只改这里，不用碰检索和抽取。

## 为什么顺序不能改（这不是审美）

DeepSeek 的 KV 缓存是**按前缀字节匹配**的，命中部分约 1/10 价格。
所以稳定内容必须在前、易变内容必须在后：

    A 人设 500        ← 装完就不动
    B 角色卡 400      ← 人变了才动（换版本号）
    C 钉住 200        ← 用户手动钉的，很少动
    D 记忆块 600      ← 话题不变则原样复用
    E 动态 150        ← 每轮可能变
    时间锚 20         ← 每分钟都变，必须最后

时间锚放开头的话，每轮前缀都不同，缓存 100% 失效，成本直接翻十倍。
`test_m13_2_order.py::test_anchor_is_last` 专门守这条——防止后人重构时
"顺手"把它挪到 system 开头。

## 决策 D79：1100 是硬上限，分项预算是"上限的上限"

手册的分项加起来 1870（500+400+200+600+150+20），DoD 却写"每轮 ≤1100"。
两者不可能同时成立。

我的处理：**1100 是硬约束**，分项数字是"这一段最多能占多少"。
实际装配时先按分项装，总超 1100 就从优先级最低的块开始砍：

    D 记忆 → E 动态 → B 角色卡 → C 钉住 → A 人设 → 锚

顺序的依据是"没了它，这段对话还成不成立"：D 砍掉两条摘要，AI 少知道两件事；
A 砍掉，它就不是那个人了。

## 决策 D80：结果级复用，而不是"话题级判断"

M12 量出来触发率 45%——如果每轮都重新装 D 段，缓存命中率会很难看
（D 占 600/1100）。

但"重新检索"不等于"内容变了"。所以这里比较的是**检索结果本身**：

    新一轮的 8 条 id 和上一轮的 8 条重合 ≥ 60% → 直接复用上一轮的 D 段字节

这是唯一不看语义、只看事实的判据。语义会猜错，id 不会。

## 确定性是怎么保证的

同样的输入必须产出同样的字节，否则缓存永远不命中，而且"缓存命中率掉了"
这种故障会变成玄学。三条纪律：

1. 不用 `json.dumps(dict)` 拼任何东西（dict 顺序在 3.7+ 是插入序，但
   一旦有人改成 `set` 就完了）。全部手写模板。
2. 所有浮点数不进 prompt。分数、置信度都是内部排序用的，写进去只会
   因为 `0.7000000001` 这种问题让字节变来变去。
3. 时间锚只写到**分钟**。写秒的话每分钟都在变，虽然 D 段没变，
   整条串还是新的。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..log import get_logger
from .affect import BASELINE, MOODS, TAU_MINUTES, decay_moods, render_line
from .card import render_traits
from .curiosity import note_user_turn, speak as speak_curiosity
from .giveup import GIVEUP_PROTOCOL
from .doubt import format_doubt, pick_doubt
from .drill import DrillResult, drill, format_event
from .ledger import recall as recall_lesson
from .rank import L2Result, MemoryItem, l2_search
from .topic import Trigger, save_keywords, should_retrieve
from .turns import estimate_tokens
from .store import fact_keys

log = get_logger("pack")

__all__ = ["Block", "PackResult", "pack", "render", "TOTAL_BUDGET", "BLOCKS"]

TOTAL_BUDGET = 1100

# 稳定段版本号的占位符。先按最终长度占位、等裁剪落地后再换成真值，
# 是为了让预算计算和实际注入看到同一份字节（详见 pack() 里那段注释）。
_VSLOT = "[ctx-v0000]"

# 顺序即优先级（从高到低）。裁剪按**反序**进行。
BLOCKS = ("persona", "card", "pinned", "memory", "dynamic", "anchor")

# 钉住条数的提醒下限。1~2 条即使很长也不提——那不是失控，是用户就是想记两件事。
# 提醒的价值在"你攒了 12 条，每条都在每轮花钱"这个量级上。
PIN_WARN_MIN = 5

_LABEL = {
    "persona": "A 人设",
    "card": "B 角色卡",
    "pinned": "C 钉住",
    "memory": "D 记忆",
    "dynamic": "E 动态",
    "anchor": "时间锚",
}

_SEP = "\n---\n"

# 没配置人设文件时的兜底。和 `hippo init` 生成的一致（init 之后就走文件了）。
_DEFAULT_PERSONA = """# 我是谁

你是一个记得住事的对话伙伴。你记得这个人说过的话，但不会把记忆背出来，
只在合适的时候自然地用上。

## 行为准则

- 不确定的事，问一次就好，问完不再纠结。
- 不知道就说不知道。人也会忘事，这不是缺陷。
- 做不到的事可以直说，不要硬编。"""



@dataclass
class Block:
    name: str
    text: str = ""
    tokens: int = 0
    budget: int = 0
    dropped: list[str] = field(default_factory=list)

    @property
    def used(self) -> bool:
        return bool(self.text.strip())


@dataclass
class PackResult:
    text: str
    blocks: list[Block]
    total_tokens: int
    dropped: list[str] = field(default_factory=list)
    trigger: Trigger | None = None
    items: list[MemoryItem] = field(default_factory=list)
    drilled: DrillResult | None = None
    reused: bool = False           # D 段是否是复用上一轮的
    digest: str = ""               # 全文 sha1 前 12 位，观测缓存用
    warnings: list[str] = field(default_factory=list)   # 给用户看的提示（M16.2）
    degraded: bool = False         # M27.3：预算熔断，只剩 A + C + 锚

    @property
    def by_name(self) -> dict[str, Block]:
        return {b.name: b for b in self.blocks}


# ------------------------------------------------------------------ 各段
def _persona(cfg: Any) -> str:
    """A 段。文件不存在就用兜底模板——**不报错**：没写人设不该让服务起不来。

    M22.1：弃权协议**恒定追加**在人设末尾。协议是功能不是风格——
    用户写自己的人设文件时也照样带，否则弃权出口就没了。人设过长时
    先截人设保协议（协议末尾就在，所以截的是人设主体）。
    """
    path = getattr(cfg, "persona_path", "") if cfg is not None else ""
    text = ""
    if not path:
        db = getattr(cfg, "db_path", "") if cfg is not None else ""
        path = str(Path(db).expanduser().parent / "persona.md") if db else ""
    if path:
        p = Path(path).expanduser()
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8").strip()
        except OSError:
            log.warning("persona.unreadable", extra={"fields": {"path": str(p)}})
    if not text:
        text = _DEFAULT_PERSONA
    # 人设预算 500 token（estimate 带 1.3 系数，按字符粗算留 200 字给协议）
    budget_chars = 1500
    if len(text) > budget_chars:
        text = text[:budget_chars].rstrip() + "\n\n（以下从简）"
        log.info("persona.truncated", extra={"fields": {"kept": budget_chars}})
    return f"{text}\n\n{GIVEUP_PROTOCOL}"


def _card(store: Any, session: str, budget: int, *, cross: bool = False) -> tuple[str, list[str]]:
    """B 段。按"特征数多的人优先"排，超预算先丢 inferred，再丢低置信。

    `[ctx-v{n}]` 是版本号：内容变了才递增，用来观测缓存失效点。
    用内容哈希的前 2 位当版本号——不引状态、不用自增计数器，
    而且"内容一样版本就一样"是天然成立的。
    """
    drop: list[str] = []
    rows: list[tuple[int, str, str]] = []
    if cross:
        # 跨会话：特征属于"这个人"，新开会话不该失忆。
        # 事件/计划/存疑仍然严格按会话隔离。
        grouped: dict[str, list[Any]] = {}
        for tr, who in store.list_traits_cross():
            grouped.setdefault(who or _SELF, []).append(tr)
        for idx, (who, traits) in enumerate(grouped.items()):
            lines = render_traits(traits)
            if not lines:
                continue
            rows.append((idx, who, "\n".join(lines)))
    else:
        ents = store.list_entities(session)
        for e in ents:
            traits = store.list_traits(e.id)
            lines = render_traits(traits)
            if not lines:
                continue
            rows.append((e.id, e.canonical, "\n".join(lines)))
    if not rows:
        return "", drop

    rows.sort(key=lambda r: (-len(r[2].splitlines()), r[0]))
    head = "# 我认识的"
    body = "\n".join(f"{name}：{txt.replace(chr(10), '；')}" for _, name, txt in rows)
    text = f"{head}\n{body}"

    # 超预算：从**最后一个人**开始丢（排序已经把"特征多的"放前面了）
    while estimate_tokens(text) > budget and len(rows) > 1:
        _, name, _ = rows.pop()
        drop.append(f"card:{name}")
        body = "\n".join(f"{n}：{t.replace(chr(10), '；')}" for _, n, t in rows)
        text = f"{head}\n{body}"
    if estimate_tokens(text) > budget:
        # 只剩一个人还超：截断而不是丢光——丢光等于不认识任何人
        from .rank import clip_tokens
        text = clip_tokens(text, max(10, budget - 2)) + "…"
    return text, drop


_SELF = "用户"   # 没挂实体的事实算在谁头上


def _pinned(store: Any, session: str, *, highlight: bool = True,
            cross: bool = False) -> tuple[str, list[str], int]:
    """C 段。两类东西：用户钉住的事件，和**每个槽位最新的那条事实**。

    ## 去重必须按 (人, 槽)，不能只按槽

    M13 第一版是 `latest[f.key]`，只按槽去重。真机库上直接出错：

        老王 commute=飞机 / 小李 commute=高铁 / 用户 commute=步行
        → prompt 里变成 "commute=飞机"

    三个人的通勤被压成一条，捡了 `valid_from` 最大的那个（老王的）。
    **数据是对的（facts 表里 entity_id 分明），是渲染把 `who` 丢了。**

    这条教训值得记：M13 当时我看到这个现象，判断成"facts 表里同一个 key
    有三条 active，是 M8 写坏的"。去库里一查才发现三条分属三个 entity、
    各自只有一条 active。**症状在渲染层，我去查了数据层，然后按错误结论
    给 M14 派了一个不需要做的活（给 facts 加 entity 列）。**

    ## 多值槽要全留

    `diet` 是 card=multi：不吃辣、不吃香菜是两件事，压缩成一条就丢信息。
    同一个值重复出现（真机库里两条"不吃辣"，不同事件抽出来的）要去重，
    否则 prompt 里会出现 `diet=不吃辣/不吃辣`——正确但难看，而且白占 token。
    """
    lines: list[str] = []
    for e in store.list_pinned(session):
        lines.append(f"- {e.summary or e.raw}")

    # (谁, 槽) → [(valid_from, 值)]，多值槽留着全部
    latest: dict[tuple[str, str], list[tuple[int, str]]] = {}
    fact_rows = (store.list_facts_cross(include_historical=False) if cross
                 else store.list_facts(session, include_historical=False))
    for f, who in fact_rows:
        if f.valid_from is None:
            continue
        k = (who or _SELF, f.key)
        latest.setdefault(k, []).append((int(f.valid_from), f.value, f.card))

    if latest:
        # 一人一行，行内按槽名排序。一条事实一行会把 C 段从 110 token
        # 顶到 250（16 行 × 行首开销），实测直接超预算、把最后 4 条丢掉。
        per: dict[str, list[str]] = {}
        for who, key in sorted(latest, key=lambda x: (x[0] != _SELF, x[0], x[1])):
            got = sorted(latest[(who, key)], key=lambda x: -x[0])
            card = got[0][2]
            vals: list[str] = []
            for _, v, _c in got:
                if v not in vals:
                    vals.append(v)
            if card != "multi":
                vals = vals[:1]        # 单值槽以最后一次说的为准
            per.setdefault(who, []).append(f"{key}={('、'.join(vals))}")
        lines.append("已知：")
        for who in sorted(per, key=lambda x: (x != _SELF, x)):
            lines.append(f"- {who}：{'；'.join(per[who])}")

    # ---- 重点标注层：最近 + 最重要的几条，每轮固定带
    #
    # 设计里这一层不是"用户手动 pin"，而是"越重要带得越多"：
    # 基础 3 条（按 权重→新近），权重 >= 0.8 的可以多带，最多 6 条。
    # 放在 C 段而不是检索段，因为它不依赖本轮问了什么。
    recent: list[tuple[Event, float]] = []
    try:
        if not highlight:
            raise RuntimeError("degraded")
        already = {e.id for e in store.list_pinned(session)}
        for ev in store.highlight_events(session, limit=8):
            if ev.id in already:
                continue
            weight = float(getattr(ev, "weight", 0) or 0)
            if len(recent) >= 3 and weight < 0.8:
                continue
            if len(recent) >= 6:
                break
            recent.append((ev, weight))
    except Exception:  # noqa: BLE001 - 标注层缺失不该影响装配
        recent = []

    extra_tokens = 0
    if recent:
        block: list[str] = ["最近："]
        for ev, _w in recent:
            day = datetime.fromtimestamp(int(ev.event_time)).strftime("%m-%d")
            kws = " ".join(ev.keyword_list[:3])
            text = (ev.summary or ev.raw or "").strip().replace("\n", " ")[:80]
            block.append(f"- {day} {text}" + (f"（{kws}）" if kws else ""))
        extra_tokens = estimate_tokens("\n".join(block))
        lines.extend(block)

    if not lines:
        return "", [], 0
    return "# 一定要记住的\n" + "\n".join(lines), [], extra_tokens


def _query_bigrams(text: str) -> set[str]:
    """问题里的中文二元组（去掉标点空白）。"""
    import unicodedata
    norm = unicodedata.normalize("NFKC", str(text or "")).lower()
    norm = "".join(c for c in norm if not c.isspace() and c not in "，。、；：！？,.;:!?（）()「」『』\"'“”‘’—…·")
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def _raw_hit(it: MemoryItem, query: str, *, max_chars: int = 60) -> str:
    """问题里的词命中"原话"、却没命中"摘要"时，返回一小段原话。

    为什么只在这时候补：之前无脑给前 K 条加原文，结果原话按档位预算挤掉了摘要，
    gold 集直接掉 3~4 题（答案本来就在摘要里）。这里加的是**条件**：
    只有"问题里的二元组出现在 raw、但摘要里没有"才补，并且单独加预算，
    不占用摘要的额度。
    """
    if not query:
        return ""
    raw = str(getattr(it.event, "raw", "") or "")
    summary = str(it.text or it.event.summary or "")
    if not raw:
        return ""
    grams = _query_bigrams(query)
    if not grams:
        return ""
    hit = [g for g in grams if g in raw and g not in summary]
    if not hit:
        return ""
    # 从第一个命中位置取一小段，保证包含命中的词
    pos = raw.find(hit[0])
    if pos < 0:
        pos = 0
    return raw[max(0, pos - 10):pos + max_chars].strip()


def _raw_excerpt(it: MemoryItem, *, max_chars: int = 60) -> str:
    """取原话开头一小段（不依赖问题词）。"""
    raw = str(getattr(it.event, "raw", "") or "").strip()
    if not raw:
        return ""
    return raw[:max_chars].strip()


def _raw_turn_block(rows: list[dict], *, max_chars: int = 80) -> tuple[str, int]:
    """原话兜底段（L1_RAW）。事件没命中时，给几句原文当线索。

    单独成段而不是混进事件里：它的优先级更低、格式也不同（没有关键词/权重），
    混排会让模型分不清"这是记忆还是原话"。
    """
    if not rows:
        return "", 0
    lines = ["# 原话（没抽成事件，可能是线索）"]
    for row in rows[:3]:
        text = " ".join(str(row.get("text") or "").split())[:max_chars]
        if not text:
            continue
        try:
            day = datetime.fromtimestamp(int(row.get("ts") or 0)).strftime("%m-%d")
        except (TypeError, ValueError, OSError):
            day = "--"
        lines.append(f"- {day} {text}")
    if len(lines) == 1:
        return "", 0
    body = "\n".join(lines)
    return body, estimate_tokens(body)


def _memory_block(items: list[MemoryItem], drilled: DrillResult | None,
                  query: str = "") -> str:
    """D 段。一条记忆长什么样，**只在 `drill.format_event` 里定义**（M16）。

    这里以前自己拼 `[记忆 · 日期 · tier]` + 关键词 + 摘要，`format_event`
    又拼一套 `[原话 · 日期 · 用户]`。两套并存的后果是改一处忘一处，
    而且症状是"CLI 预览看着对、真进 prompt 的不对"——最难查的那种。

    现在只做一件事：把每条 item 交给 `format_event`，它按 tier 分档：
        hot  → 关键词 + 摘要（pinned 再加一行原文）
        warm → 关键词 + 摘要
        cold → 关键词
    """
    if not items:
        return ""
    out: list[str] = ["# 我记得的"]
    for ref in items:
        body = ref.raw_text or None
        budget = None
        if body is None and len(out) <= 3:      # 只给前两条补，控制成本
            # 只在"问题词命中原话、却没进摘要"时补。
            # 试过"前两条无条件给原话"：真实召回只 +1 题，gold 反而掉 1 题
            # （原话按档位预算挤占摘要，而 gold 的答案本来在摘要里），不划算。
            # 只在"问题词命中原话、却没进摘要"时补。
            # 试过"第一条无条件补 50 字原话"：召回 0 提升（答案常不在开头），
            # 白花 token，撤回。
            snippet = _raw_hit(ref, query)
            if snippet:
                body = snippet
                tier = int(getattr(ref.event, "tier", 2) or 2)
                from .drill import TIER_BUDGET
                base = TIER_BUDGET.get(tier, TIER_BUDGET[2])[1]
                # 单独加额度：原话不挤摘要
                budget = base + int(estimate_tokens('原文：""') + estimate_tokens(snippet)) + 4
        # cold 档默认只渲染关键词（省钱）。但**被检索选中的** cold 事件是
        # "本轮问题的候选答案"，只给关键词等于把答案砍掉——实测"恢复密钥
        # 存放在哪"就是这条：选中了，摘要里写着 U 盘，上下文里却只有关键词。
        # 所以选中的 cold 至少按 warm 档渲染摘要头一段。
        depth = None
        if int(getattr(ref.event, "tier", 2) or 2) >= 3 and body is None:
            from .drill import DEPTH_SUMMARY
            depth = DEPTH_SUMMARY
            if budget is None:
                budget = 70
        out.append(format_event(
            ref.event,
            depth=depth,
            summary=ref.text or None,
            body=body,
            max_tokens=budget,
        ))
    return "\n".join(out)


def _targeted_self_fact(store: Any, session: str, question: str) -> str:
    """只对明确问自身单个事实槽的问题，补充当前事实；不猜其他人的身份。"""
    q = re.sub(r"\s+", "", (question or "").lower())
    if not q or any(word in q for word in ("以前", "之前", "过去", "原来", "原先", "当时", "曾经")):
        return ""
    if not any(word in q for word in ("?", "？", "多少", "什么", "哪", "多大", "几", "吗", "呢")):
        return ""
    q = re.sub(r"[?？!！。,.，]", "", q)
    q = re.sub(r"^(?:请问|你还记得|你记得|请告诉我)", "", q)
    if not q.startswith("我"):
        return ""
    q = q[1:]
    # "的"可能出现在时间副词两侧："我的住址" / "我现在的住址" / "我今年的年龄"。
    # 第一版只允许它出现在"现在/今年"前面，于是"我现在的住址是什么"匹配不上，
    # 而"我的住址是什么"能匹配——同一件事换个说法就查不到，纯属正则漏写。
    q = re.sub(r"^(?:的)?(?:今年|现在|目前|平时|当前)?(?:的)?(?:是)?", "", q)
    q = re.sub(r"(?:吗|呢|呀|啊)$", "", q)
    table = fact_keys()
    matches = set()
    for name, key in table.keys.items():
        for alias in (key.cn, *key.aliases):
            token = re.sub(r"\s+", "", alias.lower())
            if len(token) < 2:
                continue
            if q == token or q in (token + "是多少", token + "是什么", token + "多少"):
                matches.add(name)
    if len(matches) != 1:
        return ""
    name = matches.pop()
    rows = [(f, who) for f, who in store.list_facts(session, include_historical=False)
            if f.key == name and (not who or who == _SELF) and f.valid_from is not None]
    if not rows:
        return ""
    rows.sort(key=lambda item: (int(item[0].valid_from or 0), item[0].id), reverse=True)
    spec = table.get(name)
    values = list(dict.fromkeys(re.sub(r"\s+", " ", f.value).strip() for f, _ in rows if f.value.strip()))
    if not values:
        return ""
    if spec and spec.single:
        values = values[:1]
    elif len(values) > 3:
        values = values[:3] + ["等"]
    return f"[当前事实] 用户的{spec.cn if spec else name}：{'、'.join(values)}"


def _fmt_plan(p: Any, now_ts: int) -> str:
    """M19.3 到点注入格式。手册快照：

        [计划] 现在是 08:00，到点了：叫用户起来干活
        原话：「明天8点叫我起来干活」
        （这条晚了 25 分钟）

    原话必须带——"明天8点"四个字是用户自己的表述，比任何转述都
    能唤起"我说过这话"的记忆（原文是唯一真相源，对计划同样成立）。
    missed 的说明只进第一条 line 的下一行，不展开小作文。
    """
    hhmm = datetime.fromtimestamp(now_ts).strftime("%H:%M")
    lines = [f"[计划] 现在是 {hhmm}，到点了：{p.content}"]
    if p.raw_hint:
        lines.append(f"原话：「{p.raw_hint}」")
    if p.missed:
        late = max((now_ts - p.due_at) // 60, 1)
        lines.append(f"（这条晚了 {late} 分钟）")
    return "\n".join(lines)


def _dynamic(store: Any, session: str, now: datetime, cfg: Any = None,
             upcoming_action: str = ""
             ) -> tuple[str, list[str]]:
    """E 段。教训 > 存疑 > 计划 > 情绪，超预算先丢情绪（手册 M13.1）。

    顺序 = 优先级。Block 裁剪是从后往前砍行，所以越靠前越保命。
    教训放最前：它只在你**真要动手**时才出现，而"别重犯上次那个错"
    比"我还有个疑问没问"重要得多。
    """
    parts: list[str] = []
    drop: list[str] = []

    # 0) 教训（M23.3）：只在打算做自主动作时定向检索，常规对话不出现。
    if upcoming_action and hasattr(store, "regrets"):
        rows = recall_lesson(store, session, upcoming_action, limit=1)
        if rows:
            parts.append(f"[教训] {rows[0].summary}")

    # 1) 好奇（M25）：队列里有一条、额度够、不在静默时段，就交出去。
    #    说了好奇就不再塞疑问——同一轮里两个问号，用户只会觉得被盘问。
    now_ts0 = int(now.timestamp())
    asked = speak_curiosity(store, session, now=now_ts0, cfg=cfg)
    if asked:
        parts.append(f"[想问] 这轮回答的开头，先用一句话自然地问我：{asked}")

    # 2) 存疑（M18.2）：限流器挑一条。一轮最多 1 条、8 轮内不重复、
    #    一条疑问最多问 2 次——约束全在 pick_doubt 里，pack 只负责渲染。
    turn = int(store.get_state(session, "turn_no", "0") or 0)
    d = None if asked else pick_doubt(
        store, session, turn, now_ts=int(now.timestamp()), cfg=cfg)
    if d is not None:
        parts.append(format_doubt(d.question))

    # 2) 计划（M19.3）：刚触发的（scheduler 已 mark fired）+ 到点未触发
    #    的（scheduler 没跑时兜底）。窗口 10 分钟：用户不一定秒回，
    #    窗口太窄模型根本没机会提。defer 过的（fired=0 且 due_at 推后）
    #    自动从两个列表里消失，天然不重复注入。
    now_ts = int(now.timestamp())
    fired_plans = store.recent_fired_plans(session, now_ts=now_ts, minutes=10) \
        if hasattr(store, "recent_fired_plans") else []
    pending_plans = [p for p in store.due_plans(now_ts) if p.session == session]
    seen_ids: set[int] = set()
    for p in fired_plans + pending_plans:
        if p.id in seen_ids:
            continue
        seen_ids.add(p.id)
        parts.append(_fmt_plan(p, now_ts))

    # 3) 情绪：一行行为指令（M20.3），lazy 衰减到当前时刻（M20.1）
    a = store.get_affect(session)
    if a is not None:
        moods = {m: getattr(a, m) for m in MOODS}
        base = cfg.affect.baseline if cfg is not None else BASELINE
        tau = cfg.affect.tau_minutes if cfg is not None else TAU_MINUTES
        minutes = (now.timestamp() - a.updated) / 60.0 if a.updated else 0.0
        moods = decay_moods(moods, baseline=base, tau=tau, minutes=minutes)
        parts.append(render_line(moods, baseline=base))

    if not parts:
        return "", drop
    return "\n".join(parts), drop


def _anchor(now: datetime, tz: str) -> str:
    """时间锚。**只到分钟**——带秒的话每分钟都在变，缓存全碎。"""
    try:
        dt = now.astimezone(ZoneInfo(tz or "Asia/Shanghai"))
    except Exception:  # noqa: BLE001
        dt = now
    return f"[现在 {dt:%Y-%m-%d %H:%M}]"


# ------------------------------------------------------------------ 裁剪
def _trim_to(block: Block, limit: int) -> None:
    """把一段砍到 limit token 以内。按行砍，砍掉的记进 dropped。"""
    if block.tokens <= limit:
        return
    lines = block.text.split("\n")
    while lines and estimate_tokens("\n".join(lines)) > limit:
        gone = lines.pop()
        if gone.strip():
            block.dropped.append(f"{block.name}:{gone[:16]}")
    if block.name == "memory":
        # 允许退化为关键词，但不能把没有任何内容的事件标题当作一条记忆。
        if lines and lines[-1].startswith("[记忆 · "):
            block.dropped.append(f"memory:{lines.pop()[:16]}")
        if lines == ["# 我记得的"]:
            lines.clear()
    block.text = "\n".join(lines)
    block.tokens = estimate_tokens(block.text)


def render(p: PackResult) -> str:
    """把各段拼成一个字符串。**固定分隔符，无空行，无随机顺序。**"""
    return _SEP.join(b.text.strip() for b in p.blocks if b.used)


def _pack_degraded(store: Any, session: str, *, cfg: Any, now: datetime,
                   tz: str, budget_of: dict[str, int]) -> PackResult:
    """预算熔断下的最小装配：A 人设 + C 钉住 + 时间锚。

    三件事刻意不做：
      - 不检索（D 段整块不装，省的是每轮最大的一笔）
      - 不读角色卡（B 段要遍历实体和特征，是纯开销）
      - 不写 last_pack_*（避免恢复后复用到一个空记忆块）

    锚留下的理由见 core/budget.py 的模块注释：没有日期，"明天提醒你"
    会直接说错，那不是变笨，是说错。

    `[ctx-v]` 版本号这里只哈希 A+C，和正常模式（A+B+C）不同，
    所以**模式切换那一次必然掉一次缓存**。这是有意的取舍：降级期间
    去读 B 段（遍历实体 + 特征）只为让版本号一致，省下的钱还不够付
    那几毫秒的 CPU。降级是临时状态，一次 miss 可以接受。
    """
    persona = Block("persona", _persona(cfg), budget=budget_of["persona"])
    pin_text, pin_drop, _pin_extra = _pinned(store, session, highlight=False)
    pinned = Block("pinned", pin_text, budget=budget_of["pinned"] + _pin_extra, dropped=pin_drop)
    anchor = Block("anchor", _anchor(now, tz), budget=budget_of["anchor"])

    stable = persona.text + "\x00" + pinned.text
    version = hashlib.sha1(stable.encode("utf-8")).hexdigest()[:4]
    if persona.text.strip():
        persona.text = f"[ctx-v{version}]\n" + persona.text

    blocks = [persona, pinned, anchor]
    for b in blocks:
        b.tokens = estimate_tokens(b.text)
    _trim_to(persona, budget_of["persona"])
    _trim_to(pinned, budget_of["pinned"])
    total = sum(b.tokens for b in blocks)
    if total > TOTAL_BUDGET:
        # 只可能是人设文件 + 钉住本身超标（A 500 + C 200 + 锚 20 远小于 1100，
        # 但用户可以把 persona_tokens 配成 4000）。照 D79 砍人设，保住锚。
        over = total - TOTAL_BUDGET
        _trim_to(persona, max(0, persona.tokens - over))
        total = sum(b.tokens for b in blocks)

    out = PackResult(
        text="", blocks=blocks, total_tokens=total,
        dropped=list(persona.dropped) + list(pinned.dropped),
        trigger=Trigger(False, "budget_degrade", 0.0),
        degraded=True,
    )
    out.text = render(out)
    out.total_tokens = estimate_tokens(out.text)
    out.digest = hashlib.sha1(out.text.encode("utf-8")).hexdigest()[:12]
    return out


# ------------------------------------------------------------------ 主流程
def pack(
    store: Any,
    session: str,
    user_msg: str = "",
    *,
    cfg: Any = None,
    now: datetime | None = None,
    tz: str | None = None,
    use_cache: bool = True,
    upcoming_action: str = "",
    degrade: bool = False,
) -> PackResult:
    """装配这一轮的上下文。

    `use_cache=False` 只给测试用（强制重新检索）。线上永远是 True。
    `upcoming_action` 非空 = 这一轮 AI 打算做自主动作，把同类懊悔
    定向翻出来（M23.3）。常规对话不传，懊悔就不会出现在 prompt 里。
    `degrade=True` = 日预算熔断（M27.3），只装 A/C/锚，不检索、不读卡片。
    """
    cfg = cfg if cfg is not None else getattr(store, "cfg", None)
    now = now or datetime.now()
    tz = tz or getattr(cfg, "timezone", "Asia/Shanghai") or "Asia/Shanghai"

    pack_cfg = getattr(cfg, "pack", None) or getattr(store, "pack_cfg", None)
    budget_of = {
        "persona": int(getattr(pack_cfg, "persona_tokens", 500)),
        "card": int(getattr(pack_cfg, "cards_tokens", 400)),
        "pinned": int(getattr(pack_cfg, "pinned_tokens", 200)),
        "memory": int(getattr(pack_cfg, "memory_tokens", 600)),
        "dynamic": int(getattr(pack_cfg, "dynamic_tokens", 150)),
        "anchor": 20,
    }

    # ---- 0. 用户这一句有没有接上次的追问（M25）
    # 必须放在取问题之前：同一轮里"先认领回应、再问新问题"，
    # 反过来会把刚问出去的那句当成用户已经回应过。
    if user_msg:
        try:
            note_user_turn(store, session, user_msg,
                           now=int(now.timestamp()), cfg=cfg)
        except Exception as exc:  # noqa: BLE001 - 好奇记账失败不该影响打包
            log.warning("curiosity.note.failed", extra={"fields": {"err": str(exc)}})

    # ---- 0.5 预算熔断（M27.3）：只剩 A + C + 锚
    #
    # 放在检索之前，而不是"检索完再决定装不装"：降级要省的是**钱和时间**，
    # 检索一次 L1+L2 是几毫秒加几百条候选的构造，白跑没有意义。
    #
    # 不写 last_pack_d / last_pack_ids：降级那几轮的 D 段是空的，写进去
    # 会把恢复后的第一轮"复用"成一个空记忆块——这正是 M13 反复强调的
    # "状态回填写错一步，下一轮就静默失忆"。
    if degrade:
        return _pack_degraded(store, session, cfg=cfg, now=now, tz=tz,
                              budget_of=budget_of)

    # ---- 1. 要不要检索（M12）
    # 检索用的问题串截断：超长粘贴（5 万字）会让 n-gram 分词吃掉 140ms，
    # 而 4000 字之外的词对"这一轮该想起什么"没有贡献。
    user_msg = str(user_msg or "")[:4000]
    trig = should_retrieve(store, session, user_msg) if user_msg else None
    items: list[MemoryItem] = []
    raw_turns: list[dict] = []
    dr: DrillResult | None = None
    reused = False
    inj_ids: list[int] = []      # 这一轮真正进了 prompt 的事件（M14.3 用）

    if trig is not None and trig.fire:
        res: L2Result = l2_search(store, session, user_msg,
                                   use_raw=bool(getattr(getattr(cfg, "retrieve", None),
                                                        "raw_turns", True)),
                                   use_recent=bool(getattr(getattr(cfg, "retrieve", None),
                                                           "raw_recent", False)))
        res = _drop_regret(res)
        items = res.items
        raw_turns = list(getattr(res, "raw_turns", []) or [])
        dr = drill(store, res, text=user_msg, tz=tz)

        # ---- 结果级复用（D80）
        if use_cache:
            old_ids = _read_ids(store, session)
            new_ids = [it.id for it in items]
            if old_ids and _overlap(old_ids, new_ids) >= 0.6:
                old_d = store.get_state(session, "last_pack_d", "")
                if old_d:
                    reused = True
                    # 复用的是**旧字节**，所以这一轮真正进 prompt 的是 old_ids。
                    # 记下来，M14.3 的召回加固要用——记 new_ids 会加固到
                    # 那些"检索到了但没渲染"的事件上，recall_count 会虚高。
                    inj_ids = list(old_ids)
                    store.set_state(session, "last_pack_ids", " ".join(map(str, new_ids)))
    else:
        # 不检索：直接复用上一轮的 D 段
        if use_cache:
            old_d = store.get_state(session, "last_pack_d", "")
            if old_d:
                reused = True
                inj_ids = _read_ids(store, session)
        trig = trig or Trigger(False, "no_query", 0.0)

    # ---- 2. 各段
    blocks: list[Block] = []
    dropped: list[str] = []

    # A 段先按最终长度占位版本号（和 `[ctx-vxxxx]` 等长），真值等裁剪
    # 全部落地后再填，这样预算数字和实际注入的字节是同一份。
    persona_body = _persona(cfg)
    persona = Block("persona",
                    (f"{_VSLOT}\n" + persona_body) if persona_body.strip() else "",
                    budget=budget_of["persona"])
    # 跨会话范围：事实/特征属于"这个人"，事件/计划/存疑仍按会话隔离
    cross = bool(getattr(getattr(cfg, "retrieve", None), "cross_session", True))
    card_text, card_drop = _card(store, session, budget_of["card"], cross=cross)
    card = Block("card", card_text, budget=budget_of["card"], dropped=card_drop)
    pin_text, pin_drop, pin_extra = _pinned(store, session, cross=cross)
    # 会话摘要放 C 段（必记），不放 D 段：总预算吃紧时 D 段最先被砍，
    # 放那儿等于白写。它是任务型对话里"总是存在"的锚点。
    try:
        # 直接读状态，不走 digest 模块的 import：实测跨模块调用在这里拿不到值
        # （外层能读到 134 字，进了 pack 就是空），换成本地读取后正常。
        _digest = str(store.get_state(session, "session_digest", "") or "")
    except Exception:  # noqa: BLE001 - 摘要缺失不影响装配
        _digest = ""
    _digest_line = f"[会话摘要] {_digest}" if _digest else ""
    if _digest_line:
        pin_text = _digest_line + ("\n" + pin_text if pin_text else "")
    # C 段原来的预算只够放事实；摘要单独占一行、单独给额度，
    # 否则 _trim_to 会把这一行（也是唯一一行）弹掉，摘要等于没注入。
    pin_budget = budget_of["pinned"] + pin_extra + (estimate_tokens(_digest_line) if _digest_line else 0)
    pinned = Block("pinned", pin_text, budget=pin_budget, dropped=pin_drop)

    raw_extra = 0
    if reused:
        mem_text = store.get_state(session, "last_pack_d", "")
    else:
        mem_text = _memory_block(items, dr, query=user_msg or "")
        raw_text, raw_extra = _raw_turn_block(raw_turns)
        if raw_text:
            mem_text = f"{mem_text}\n{raw_text}" if mem_text else raw_text
        # 裁剪是"从后往前砍行"，所以活下来的是前缀。数一下还剩几个条目头，
        # 就知道哪几条真的发出去了——**不能拿 items 全量当注入**，
        # 那会把"考虑过但被砍掉"的也算成被想起（手册 §M14.3 明确禁止）。
        inj_ids = [it.id for it in items[:mem_text.count("[记忆 · ")]]
    memory = Block("memory", mem_text, budget=budget_of["memory"] + raw_extra)
    dyn_text, dyn_drop = _dynamic(store, session, now, cfg=cfg,
                                  upcoming_action=upcoming_action)
    direct_fact = _targeted_self_fact(store, session, user_msg)
    if direct_fact:
        dyn_text = "\n".join(part for part in (direct_fact, dyn_text) if part)
    dynamic = Block("dynamic", dyn_text, budget=budget_of["dynamic"], dropped=dyn_drop)
    anchor = Block("anchor", _anchor(now, tz), budget=budget_of["anchor"])

    for b in (persona, card, pinned, memory, dynamic, anchor):
        b.tokens = estimate_tokens(b.text)
        blocks.append(b)

    # C 段超标提醒（M16.2）。判据是"token 超预算"而不是"条数多"：
    # 3 条 200 字的钉住和 12 条短句一样贵，提醒该按钱算。
    #
    # M27.1：`count_pinned` 挪到 tokens 判断**里面**。之前是无条件先数一遍，
    # 8000 条事件的库上 `count(*) WHERE session=? AND pinned=1` 要 4ms——
    # 占整个 pack 的 20%，而它只服务一条"你钉太多了"的提示。放在短路之后，
    # 语义完全不变（两个条件本来就都要成立），正常用户一次都不再付这个钱。
    warns: list[str] = []
    if pinned.tokens > budget_of["pinned"]:
        n_pin = store.count_pinned(session)
        if n_pin >= PIN_WARN_MIN and store.pin_warn_once(session, n_pin, pinned.tokens):
            warns.append(
                f"你钉了 {n_pin} 条记忆，C 段已经 {pinned.tokens} token"
                f"（预算 {budget_of['pinned']}），要不要清理几条？"
                f"用 `hippo pin --list` 看，`hippo pin --event N --off` 撤销。")

    # ---- 3. 先按分项预算裁
    for b in blocks:
        if b.name == "anchor":
            continue          # 不裁
        _trim_to(b, b.budget)

    # 角色卡是稳定的背景，检索记忆是针对本轮问题的内容。只有总预算
    # 真正不足且有检索结果时，才收缩过长角色卡，并保留至少 200 token。
    # 这样不必在空闲或普通对话中降低角色信息，也避免一张大卡挤掉本轮答案。
    pre_total = sum(b.tokens for b in blocks)
    card_floor = min(200, budget_of["card"])
    if memory.used and pre_total > TOTAL_BUDGET and card.tokens > card_floor:
        target = max(card_floor, card.tokens - (pre_total - TOTAL_BUDGET))
        compact, compact_dropped = _card(store, session, target)
        if estimate_tokens(compact) < card.tokens:
            card.text = compact
            card.tokens = estimate_tokens(compact)
            card.dropped.extend(compact_dropped)
            card.dropped.append("card:budget_compacted")

    # ---- 4. 总预算（D79）：按优先级从低到高砍
    order = ["memory", "dynamic", "card", "pinned", "persona"]
    by = {b.name: b for b in blocks}
    total = sum(b.tokens for b in blocks)
    for name in order:
        if total <= TOTAL_BUDGET:
            break
        b = by[name]
        over = total - TOTAL_BUDGET
        target = max(0, b.tokens - over)
        _trim_to(b, target)
        total = sum(x.tokens for x in blocks)
    if total > TOTAL_BUDGET:
        # 连锚和人设都砍完还超：只可能是人设文件本身超标。截人设，记日志。
        _trim_to(by["persona"], max(0, by["persona"].tokens - (total - TOTAL_BUDGET)))
        total = sum(x.tokens for x in blocks)
        log.warning("pack.over_budget", extra={"fields": {"total": total}})

    # 版本号填真值（手册 M13.2）。裁剪已经落到最终字节，现在算的哈希
    # 才是"实际注入的稳定段"。之前是在裁剪前算，C 段一被裁就对不上：
    # 版本号是缓存的键，键和内容不符时，内容变了还会复用旧前缀。
    if _VSLOT in persona.text:
        body = persona.text.split("\n", 1)[1] if "\n" in persona.text else ""
        stable = body + "\x00" + card.text + "\x00" + pinned.text
        version = hashlib.sha1(stable.encode("utf-8")).hexdigest()[:4]
        # 等长替换，token 数不变，预算结论保持成立。
        persona.text = persona.text.replace(_VSLOT, f"[ctx-v{version}]", 1)

    # 最终总预算裁剪也可能移除事件；只给实际发出的记录回填和加固。
    inj_ids = inj_ids[:by["memory"].text.count("[记忆 · ")]

    for b in blocks:
        dropped.extend(b.dropped)

    out = PackResult(text="", blocks=blocks, total_tokens=total,
                     dropped=dropped, trigger=trig, items=items, drilled=dr,
                     reused=reused, warnings=warns)
    out.text = render(out)
    out.total_tokens = estimate_tokens(out.text)
    out.digest = hashlib.sha1(out.text.encode("utf-8")).hexdigest()[:12]

    # ---- 5. 回填状态（M12 下一轮要用）
    if user_msg:
        _save_state(store, session, by["memory"].text, items, trig, inj_ids)
        # ---- 6. 召回加固（M14.3）。只记真的进了 prompt 的那些。
        if inj_ids:
            try:
                store.touch_events(inj_ids, when=int(now.timestamp()))
            except Exception as exc:      # 加固失败不影响这一轮回复
                log.warning("pack.touch_failed", extra={"fields": {"err": str(exc)}})
    return out


def _drop_regret(res: L2Result) -> L2Result:
    """D 段不装懊悔记忆（M23.3 第 4 条："不进常规 pack"）。

    **为什么不在检索层排除**：retrieve._EXCLUDE_KINDS 只排 idle/raw/giveup。
    regret 是正经记忆（w0=0.85，能活 180 天），该被 `hippo recall` 查到、
    该在评测里被数到。要拦的是"自动注入"——它一旦混进 D 段，会在
    与教训无关的话题里冒出来，读起来像 AI 在自怨自艾。

    拦在装配层还有个好处：drill 的原文配额是按 res.items 分的，
    这里重建一条 res，配额自然就落到真正要发出去的那几条上。
    """
    kept = [it for it in res.items if it.event.kind != "regret"]
    if len(kept) == len(res.items):
        return res
    return L2Result(items=kept, candidates=res.candidates, matched=res.matched,
                    total_tokens=sum(it.tokens for it in kept),
                    elapsed_ms=res.elapsed_ms, query=res.query,
                    degraded=res.degraded, budget=res.budget)


def _overlap(a: list[int], b: list[int]) -> float:
    """两轮结果的 Jaccard。用 id 比较 = 比较事实，不比较语义。"""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


def _read_ids(store: Any, session: str) -> list[int]:
    raw = store.get_state(session, "last_pack_ids", "")
    return [int(x) for x in raw.split() if x.isdigit()]


def _save_state(store: Any, session: str, mem_text: str,
                items: list[MemoryItem], trig: Trigger,
                inj_ids: list[int] | None = None) -> None:
    """回填会话状态。**这一步漏了，下一轮永远 cold_start**，缓存全废。

    `last_pack_ids` 存的是**真正渲染出去的** id（`inj_ids`），不是 `items`
    全量。差别在 D 段被裁剪过的时候：

        items 8 条 → 预算裁到 5 条 → 渲染 5 条
        存 items 全量 → 下一轮复用时会去"加固"那 3 条从来没发出去过的
                      → recall_count 虚高 → 它们的半衰期被凭空延长

    同一个 id 列表既做 D80 的重合判定，又做 M14.3 的加固对象——两件事
    的定义都是"上一轮真的装了什么"，用同一份数据才对。
    """
    ids_out = inj_ids if inj_ids is not None else [it.id for it in items]
    store.set_state(session, "last_pack_d", mem_text)
    store.set_state(session, "last_pack_ids", " ".join(str(i) for i in ids_out))
    ids: set[int] = set()
    for it in items:
        ids.update(it.event.entities)
    # 关键词取"这一轮用户说的话"而不是记忆块的——M12 判的是"这句话和上一轮
    # 的话题像不像"，用记忆块的 30 个词会让分母失控（覆盖率会接近 0）。
    save_keywords(store, session, trig.keywords, ids)


# ------------------------------------------------------------------ 注入
_ANCHOR_PREFIX = "[现在 "


def split_anchor(text: str) -> tuple[str, str]:
    """把时间锚从句尾切出来。锚要单独插到**对话最后**，不能留在 head 里。

    head 在对话之前（缓存前缀），锚在对话之后（每轮都变）。
    两者放一起的话，锚一变，后面的对话历史全部 miss。
    """
    lines = (text or "").rstrip().split("\n")
    if lines and lines[-1].startswith(_ANCHOR_PREFIX):
        return "\n".join(lines[:-1]).rstrip(), lines[-1]
    return (text or "").rstrip(), ""


def inject(messages: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """把 pack 的内容注入 messages。返回**新列表**，不改调用方的对象。

    两处插入：

      head → 最前面。客户端已有 system 时拼在它前面（合并成一条），
             不新增第二条 system——两条 system 会让模型不知道该听谁的。

      tail → **最后一条 user 的内容前面**，不是单独一条 message。

    tail 为什么并入 user 而不是单独一条 system：两种做法对缓存**几乎等价**
    （离线实测 93% vs 94%，差别在一条消息的格式开销），因为锚无论如何
    都在"历史之后"——前缀在锚处断开，前面的 head 和历史照样命中。

    真正致命的是把锚放到**最前面**：那会把后面所有内容（head 主体 + 全部
    历史）都推出缓存。这条才是 §5.2 那句话的意思。

    选并入 user，是因为它少一条 message，而且"现在几点"紧挨着用户这句话，
    模型读起来更自然。
    """
    head, tail = split_anchor(text)
    if not head:
        return messages

    msgs = [dict(m) for m in messages or []]
    first = msgs[0] if msgs else None
    if first is not None and first.get("role") == "system" and isinstance(
            first.get("content"), str):
        first["content"] = head + "\n" + first["content"]
    else:
        msgs.insert(0, {"role": "system", "content": head})

    if tail:
        for m in reversed(msgs):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                m["content"] = tail + "\n" + m["content"]
                break
    return msgs
