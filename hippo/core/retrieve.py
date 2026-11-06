"""检索 L1 分类层（M9）。

这一层不做语义，只做**减法**：把 8000 条事件砍成几百条，且一条真该想起的都不许砍掉。

三个砍法，顺序不能换：

    1. 实体（M9.1）   "这句话提到了谁" —— 砍掉 95%，误砍代价最高，所以最保守
    2. 状态+时间（M9.2）"还有效吗""是哪个时间段的" —— 砍掉过期与越界的
    3. 上限（M9.3）   剩下的还太多就按权重截 —— 这是兜底，不是策略

**为什么这一层不能做语义判断**
"这句话该不该想起那件事"只有模型答得上来。L1 用规则答，就一定会在边界上错。
所以 L1 的设计目标不是"准"，是"**宁可多留，不可错杀**"：

  - 实体名短于 2 字不参与匹配（"妈"会命中"妈妈""姨妈""姑妈"）
  - 时间词旁边有言说动词就不当时间过滤（"我上周说要搬家"——搬家发生在三个月前）
  - 拿不准就退回 plain，让 M10 的关键词打分去解决

**与手册的一处实现差异（D59）**
手册 M9.1 写"命中实体的事件 + `events.entities` 包含该 id 的事件，取并集"。
实际 `event_entities` 由 002 的触发器从 `events.entities` 同步，两者语义相同、
`event_entities` 是真相源（002 迁移文件开头的约定）。这里只查 `event_entities`。
取并集会把"同步坏了一半"这种故障掩盖掉，而它恰恰是要被发现的。
`tests/test_m9_1_entity_filter.py::test_json_column_never_drifts_from_relation` 守这条不变量。
"""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..log import get_logger
from .entities import normalize_name
from .timeparse import TimeSpan, parse_span

log = get_logger(__name__)

__all__ = [
    "Query", "L1Result",
    "PLAIN", "HISTORICAL", "RECENT", "AT",
    "extract_entities_in_query", "parse_query", "l1_candidates", "l1_facts",
    "RECENT_DAYS", "MIN_ENTITY_LEN",
]

PLAIN = "plain"
HISTORICAL = "historical"
RECENT = "recent"
AT = "at"

# 单字实体名不参与查询匹配。中文里"妈""爸""猫""车"几乎必然出现在
# 别的词里（妈妈/姨妈、爸爸、小猫、车站），命中一次就把候选集扩大到全库，
# 而这个"扩大"是静默的：检索照样返回结果，只是结果开始跑偏。
# 代价是"我妈"这种问法捞不到 canonical='妈' 的实体——但这类实体通常
# 有更长的别名（"老妈""妈妈"），M7 建实体时就会带进来。
MIN_ENTITY_LEN = 2

# "最近"没有明确边界，给一个窗口。30 天是"还能叫最近"的上限：
# 再长用户就会说"前阵子/前段时间"，再短会把"上个月聊的那事"漏掉。
RECENT_DAYS = 30

# 历史意图。命中这些词 = 用户要的是"已经不是现在"的那部分记忆。
# 刻意不收"上次/那次"——它们指向**某一次具体事件**，不是"过期状态"，
# 收进来会让"上次吃的那家店"把已失效的 city=北京 也捞出来。
_HIST_WORDS = (
    "以前", "之前", "原来", "原先", "当时", "那时候", "那会儿", "那时",
    "从前", "过去", "小时候", "童年", "当年", "曾经", "原先的", "旧的",
    "前任", "上一份", "上一个", "老东家",
)

# 近期意图（parse_span 解析不出具体区间的那些，如"最近""这几天"）
_RECENT_WORDS = (
    "最近", "近期", "近来", "这几天", "这两天", "这阵子", "这段时间",
    "刚才", "刚刚", "最近几天",
)

# 时间词若是在修饰"说"这个动作，它就不是事件时间。
# "我上周说要搬家"：搬家可能发生在三个月前，上周只是"说"的时间。
# 按 [上周, 现在) 过滤会把搬家事件砍掉，然后关键词"搬家"也捞不到——
# 这是 L1 唯一一类"错杀且无法在下游补救"的误判，必须在源头放过。
_SPEECH_VERBS = ("说", "讲", "聊", "问", "提", "谈", "告诉", "回复", "答应", "提过", "说过")
_SPEECH_WINDOW = 4          # 时间词前后几个字内算"修饰言说"

# 这些 kind 不进候选集：
#   idle   心跳占位，没有内容
#   raw    M6.3 的原文归档，是备份不是记忆，进来只会挤掉真正的记忆
# doubt 曾在 M8 时被排除（"存疑记录不是事实"），M18.3 放开：存疑池过期的
# 疑问会转成 kind=doubt 的 cold 记忆，手册 §519 明确要求它"下次相关话题
# 出现时被检索到"——这就是"我忘了"的体感来源。它 w0=0.15、tier=3，
# weight 天然垫底，只有话题真正相关时才会浮上来，不会刷屏。
# giveup：M22.3 的能力边界记录，只给统计看，不进对话注入——
#   每次 prompt 塞一行"我上次做不了 X"等于让 AI 反复道歉。
_EXCLUDE_KINDS = ("idle", "raw", "giveup")


@dataclass
class Query:
    """一次查询在 L1 眼里的样子。`explain()` 就是把它打印出来。"""

    text: str
    entity_ids: list[int] = field(default_factory=list)
    entity_names: list[str] = field(default_factory=list)
    mode: str = PLAIN
    span: TimeSpan | None = None
    signals: list[str] = field(default_factory=list)   # 命中的词，调试用
    time_muted: str = ""                                # 非空 = 时间词被言说动词压住了
    soft_since: int | None = None                       # "最近"这类模糊近期：只加权，不过滤

    @property
    def include_historical(self) -> bool:
        return self.mode == HISTORICAL


@dataclass
class L1Result:
    """L1 的输出。只有 id，没有正文——正文由 M10/L2 去取。"""

    event_ids: list[int]
    total: int                 # 未截断时 = len(event_ids)；截断时 = cap（未知真实总数）
    truncated: bool
    elapsed_ms: float
    query: Query | None = None

    def __len__(self) -> int:
        return len(self.event_ids)


# ------------------------------------------------------------------ M9.1 实体
def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def extract_entities_in_query(
    text: str, session: str, store: Any, *, min_len: int = MIN_ENTITY_LEN,
) -> tuple[list[int], list[str]]:
    """这句话提到了谁。返回 (entity_id 列表, 命中的正名列表)。

    **最长优先 + 占位**：先匹配长名，命中后把它占的字符区间标记掉，
    短名不能重复命中同一段。否则"老王"和"王"会一起命中，返回两个实体，
    候选集凭空翻倍。

    **不做同义/相似匹配**：M7 已经定了"宁可多一个实体，不可错合并一次"，
    检索端如果擅自用相似度扩展，等于在读取侧把 M7 拒绝的合并又做了一遍，
    而且做得更隐蔽（用户看不见）。
    """
    src = _nfkc(text)
    if not src:
        return [], []

    # (名字, entity_id)。canonical 和 aliases 一视同仁：别名就是用户嘴里说的那个。
    pairs: list[tuple[str, int]] = []
    for ent in store.list_entities(session):
        for raw in [ent.canonical, *ent.aliases]:
            name = _nfkc(normalize_name(raw))
            if len(name) >= min_len:
                pairs.append((name, ent.id))
    if not pairs:
        return [], []

    pairs.sort(key=lambda p: (-len(p[0]), p[1]))

    taken: list[tuple[int, int]] = []
    hit_ids: list[int] = []
    hit_names: list[str] = []
    for name, eid in pairs:
        start = 0
        while True:
            i = src.find(name, start)
            if i < 0:
                break
            j = i + len(name)
            if not any(i < b and j > a for a, b in taken):
                taken.append((i, j))
                if eid not in hit_ids:
                    hit_ids.append(eid)
                    hit_names.append(name)
            start = i + 1

    return hit_ids, hit_names


# ------------------------------------------------------------------ M9.2 时间
def _speech_near(text: str, at: int, width: int, span_len: int) -> bool:
    """时间词附近有没有言说动词。"""
    lo = max(0, at - width)
    hi = min(len(text), at + span_len + width)
    return any(v in text[lo:hi] for v in _SPEECH_VERBS)


def parse_query(
    text: str, session: str, store: Any, *, now: datetime | None = None,
    tz: str = "Asia/Shanghai",
) -> Query:
    """把一句人话拆成 L1 的三件条件：谁 / 什么时候 / 要不要历史。"""
    src = _nfkc(text or "").strip()
    q = Query(text=src)

    q.entity_ids, q.entity_names = extract_entities_in_query(src, session, store)

    if not src:
        return q

    # 1) 历史词优先。句子里同时出现"以前"和"上周"时，"以前"才是真意图：
    #    "我上周跟你说我以前住北京"——问的是以前。
    for w in _HIST_WORDS:
        if w in src:
            q.mode = HISTORICAL
            q.signals.append(w)
            break

    # 2) 解析时间区间。历史意图也解析（"去年冬天搬走之前"要落在去年冬天）。
    span = parse_span(src, base=now, tz=tz) if src else None
    if span is not None and span.precision == "fuzzy" and q.mode != HISTORICAL:
        # "最近""前几天"的 fuzzy span 不可靠，交给 RECENT 的固定窗口
        span = None

    if span is not None:
        # 时间词被言说动词压住 → 不当事件时间用
        i = src.find(span.matched) if span.matched else -1
        if i >= 0 and _speech_near(src, i, _SPEECH_WINDOW, len(span.matched)):
            q.time_muted = span.matched
            span = None
        else:
            q.span = span
            q.signals.append(span.matched)

    # 3) 没解析出区间但有近期词 → 固定窗口
    if q.span is None and not q.time_muted:
        for w in _RECENT_WORDS:
            if w in src:
                if q.mode != HISTORICAL:
                    q.mode = RECENT
                q.signals.append(w)
                # 软下限在解析阶段就算好：它是"这句话的语义"的一部分，
                # 放在 l1_candidates 里算会让同一句话在不同调用路径下
                # 得到不同的 Query（M12 话题切换要缓存 Query，缓存就会错）。
                base = now or datetime.now()
                q.soft_since = int(base.timestamp()) - RECENT_DAYS * 86400
                break

    if q.mode == PLAIN and q.span is not None:
        q.mode = AT
    return q


# ------------------------------------------------------------------ L1 主流程
def l1_candidates(
    store: Any,
    session: str,
    query: Query | None = None,
    *,
    text: str = "",
    cap: int | None = None,
    now: datetime | None = None,
    tz: str = "Asia/Shanghai",
) -> L1Result:
    """L1：查询 → 候选 event_id 列表。

    `query` 与 `text` 二选一。传 `query` 是为了让调用方（M12 话题切换）
    能复用已经解析过的结果，避免每轮重复解析同一句话。
    """
    if query is None:
        query = parse_query(text, session, store, now=now, tz=tz)

    cfg = getattr(store, "retrieve_cfg", None)
    if cap is None:
        cap = int(getattr(cfg, "max_candidates", 800))
    recent_days = int(getattr(cfg, "recent_days", RECENT_DAYS))

    t_start: int | None = None
    t_end: int | None = None
    if query.span is not None:
        t_start, t_end = query.span.start, query.span.end
    elif query.mode == RECENT:
        # **RECENT 不做硬过滤**（D61）。手册 M9.2 把"最近"和"上个月"并列写成
        # `event_time >= ?`，实测这条会错杀：
        #   用户问"老王最近怎么样"，老王三个月前辞职——这正是要回答的那件事，
        #   而 [今天-30天, 今天) 把它砍掉了，返回空候选，AI 答"不记得他近况"。
        # "最近"根本没有边界，30 天是拍的；而"上个月""这周"有边界，走的是 AT，
        # 那条路照旧硬过滤。所以这里只留一个 soft_since，交给 M10 加权。
        # 配置里的 recent_days 与常量不同时，以配置为准（用户可能想调窗口）。
        if query.soft_since is not None and recent_days != RECENT_DAYS:
            base = now or datetime.now()
            query.soft_since = int(base.timestamp()) - recent_days * 86400

    clock = time.perf_counter()
    ids, truncated = store.l1_filter(
        session,
        entity_ids=query.entity_ids or None,
        include_historical=query.include_historical,
        time_start=t_start,
        time_end=t_end,
        exclude_kinds=_EXCLUDE_KINDS,
        cap=cap,
    )
    elapsed = (time.perf_counter() - clock) * 1000

    if truncated:
        # 截断必须是可见的。静默截断的表现是"用久了之后它开始忘记最近的对话"，
        # 而日志里什么都查不到——因为最该被记住的那条刚好被 weight 挤出了前 800。
        log.warning("retrieve.l1.truncated", extra={"fields": {
            "session": session, "cap": cap, "entities": len(query.entity_ids),
            "mode": query.mode, "ms": round(elapsed, 2)}})

    return L1Result(
        event_ids=ids, total=len(ids), truncated=truncated,
        elapsed_ms=elapsed, query=query,
    )


# 用户本人。抽取器会为"我"建一个实体（`canonical='用户'`），它不是"一个人物"，
# 是**当前用户的自我**。这里**不**把它并进实体查询的条件里：
# 实测问"老王最近怎么样"时，用户的 17 条事实全被带上（city/diet/pet/sleep…），
# 老王的只有 1 条——18 条里 17 条无关，M13 的 600 token 预算一半没了。
# 用户问自己的事时（"我几点睡"）不会认出任何实体，走的是"无实体"分支，
# 那时全部 facts 本来就都会给。所以"问谁给谁"是自洽的。
SELF_NAMES = frozenset({"用户", "我", "自己", "本人"})


def self_entity_ids(store: Any, session: str) -> set[int]:
    """用户本人的实体 id。给 M13 渲染"我"的时候用，L1 过滤不用它。"""
    return {e.id for e in store.list_entities(session) if e.canonical in SELF_NAMES}


def l1_facts(
    store: Any, session: str, query: Query | None = None, *,
    limit: int = 200,
) -> list[Any]:
    """L1 的 facts 分支。

    与 events 的区别：facts 天然是"当前状态表"，默认只给 `valid_to IS NULL` 的。
    历史意图才放开——这也解释了为什么 M8 的 `historical` 是 0 条并不影响
    M9 的验收：**历史数据少，不代表历史查询路径可以不写**。

    返回的是 `Fact` 而不是 `(Fact, who)`：`who` 是渲染层的事，
    在检索层带上它等于让上层多拆一层元组，而检索层对它没有任何判断要做。
    """
    if query is None:
        query = Query(text="")
    rows = store.list_facts(
        session, include_historical=query.include_historical)
    facts = [f for f, _who in rows]

    if query.entity_ids:
        keep = set(query.entity_ids)
        by_entity = [f for f in facts if f.entity_id in keep]
        # entity_id 为空的 fact 是全局事实（不属于任何人），任何实体查询都该带上。
        global_facts = [f for f in facts if f.entity_id is None]
        merged = {f.id: f for f in (*by_entity, *global_facts)}
        facts = sorted(merged.values(), key=lambda f: f.id)
    return facts[:limit]


# ------------------------------------------------------------------ 调试
def explain(q: Query, res: L1Result | None = None) -> str:
    """一句话解释 L1 砍了什么。CLI `recall --explain` 用它。"""
    def day(t: int | None) -> str:
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d") if t else "-"

    lines = [f"查询：{q.text or '(空)'}"]
    muted = f"  时间词被言说动词压住：{q.time_muted}" if q.time_muted else ""
    lines.append(f"模式：{q.mode}{muted}")
    ents = ", ".join(f"{n}#{i}" for n, i in zip(q.entity_names, q.entity_ids))
    lines.append("实体：" + (ents or "无"))
    if q.span is not None:
        lines.append(f"区间：[{day(q.span.start)}, {day(q.span.end)}) {q.span.precision}")
    elif q.soft_since is not None:
        lines.append(f"软下限：{day(q.soft_since)} 起（只加权，不过滤）")
    lines.append("信号：" + (", ".join(q.signals) or "无"))
    if res is not None:
        lines.append(f"候选：{len(res.event_ids)} 条"
                     + ("（已截断）" if res.truncated else "")
                     + f"  {res.elapsed_ms:.2f}ms")
    return "\n".join(lines)
