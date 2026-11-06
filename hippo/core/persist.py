"""抽取结果入库（M5.3）。

一条铁律：**一个窗口的所有写入，要么全成，要么全不成。**

为什么这么较真：traits 引用 events 的下标，facts 引用 events 的 id。
如果 events 写进去了、traits 写失败回滚了，库里就有一批"有事件没特征"的残骸；
反过来更糟——特征指向不存在的事件，M7 角色卡渲染时直接崩。
所以 entities→events→traits→facts→plans→doubts 全在一个事务里，
只有"降级兜底事件"例外（它必须在抽取失败时也能落库，所以单独走）。

第二件事是 **下标映射**。模型说的 `evidence: [0, 2]` 指的是"你这次输出的
第 0 条和第 2 条 event"，不是数据库 id。而第 0 条可能因为 raw 锚定失败被丢弃了。
所以映射表必须记录"哪些下标成功入库"，并且**保留空洞**——
如果压缩掉空洞，evidence=[2] 会错挂到原来的第 3 条上。
这种错挂不报错，只会让角色卡慢慢长出错误的特征，最后表现为
"它为什么觉得我讨厌猫？"——而这句话用户从来没说过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from ..log import get_logger
from .affect import TAU_MINUTES, apply_delta, decay_moods
from .doubt import filter_doubts, resolve_matching_doubts
from .keywords import clean_keywords
from .ledger import settle as settle_ledger
from .pin import looks_pinned
from .schema import ExtractResult, anchor_raw
from .store import Store, StoreError
from .timeparse import cn_int, parse_due, parse_time
from .turns import Window

log = get_logger("persist")

# 用户自己。facts/traits 里说"我"的时候挂到它上面。
SELF = "用户"

# 情绪六维的合法键（和 affect 表的列一一对应）
_MOODS = ("joy", "sadness", "anger", "fear", "disgust", "surprise")

# M18.1：每 session 活跃 doubts 上限。config.doubts.max_active 的默认值，
# persist 拿不到 cfg 时兜底用。
MAX_ACTIVE_DOUBTS = 20


@dataclass
class PersistStats:
    """入库结果。字段全是为了写进 extract_runs 和日志。"""

    event_ids: list[int] = field(default_factory=list)
    entities_new: int = 0
    entities_hit: int = 0
    facts: int = 0                   # 实际写入（insert/refine/replace）
    facts_same: int = 0              # 只是又确认了一次，没有新信息
    facts_replaced: int = 0          # 真的改了值（用户说"我搬家了"）
    facts_doubted: int = 0           # 低置信度冲突，转成疑问
    facts_rejected: int = 0          # key 不在词表
    traits: int = 0
    traits_dropped: int = 0          # evidence 全被丢弃 → 无证据，不落库
    plans: int = 0
    plans_dropped: int = 0           # due_hint 解析不出来
    doubts: int = 0
    doubts_seen: int = 0             # M18.1：模型报上来的疑问总数
    doubts_filtered: int = 0         # M18.1：质检滤掉/合并的条数
    doubts_resolved: int = 0         # M18.3：用户自己回答关闭的疑问
    verbatim_fail: int = 0           # raw 锚定失败
    affect_changed: int = 0
    ledger_settled: int = 0          # M23.2：这一轮结算掉的自主动作
    ledger_regret: int = 0           # M23.3：其中写成懊悔记忆的
    kw_raw: int = 0                  # 模型给的关键词总数（清洗前）
    kw_kept: int = 0                 # 入库的
    kw_fallback: int = 0             # 清洗后为空、退回实体名的事件数

    def as_counts(self) -> dict[str, int]:
        return {
            "n_events": len(self.event_ids), "n_facts": self.facts,
            "n_traits": self.traits, "n_plans": self.plans,
            "n_doubts": self.doubts, "verbatim_fail": self.verbatim_fail,
        }


def user_text_of(window: Window) -> str:
    """锚定用的原文：只有用户说的话。

    不含 assistant，是为了让"AI 说的话"天然锚不上——
    这是规则 2（只有用户说的才进 events）的机器执行版，
    比在 prompt 里求模型遵守可靠得多。
    """
    return "\n".join(t.content for t in window.turns
                     if t.role == "user" and t.content.strip())


def _arousal_tier(arousal: float) -> tuple[int, float]:
    """按情绪强度粗分初始权重与层级。M14 会重算，这里只需要"不太离谱"。

    为什么用 arousal 而不是别的：吵架、告白、坏消息这类事如果一开始就进
    cold 层，衰减会把它冲掉，而它恰恰是最该被记住的。
    """
    a = max(0.0, min(1.0, arousal))
    w0 = round(0.40 + 0.50 * a, 3)
    return (1 if w0 >= 0.70 else 2), w0


def _self_age(store: Store, session: str, result: ExtractResult) -> int | None:
    """拿用户本人年龄，给人生阶段解析当锚。

    两条来源，优先本次抽取：用户这一轮刚说"我二十九了"，比库里三个月前的
    记录新。拿不到就问库。

    为什么值得多花这一次查询：`parse_time` 有锚时，"小时候"给的是
    9 年宽的区间（1999~2009）；没锚时为了盖住 22~40 岁的人，只能放宽到
    19 年（1994~2013）。搜"我小时候住的地方"时，前者能收窄一半。

    **拿不到就返回 None，不做默认假设**——默认年龄是猜的，猜的锚比没有锚
    更危险：它会让 `guessed=0` 掩盖"其实全靠猜"这件事。
    """
    for f in result.facts:
        if (f.key or "").strip().lower() in ("age", "年龄"):
            v = cn_int(str(f.value))
            if v and 0 < v < 120:
                return v
    try:
        for fact in store.get_fact(session, "age"):
            v = cn_int(str(fact.value))
            if v and 0 < v < 120:
                return v
    except Exception:                     # 查库失败不该阻断写入
        pass
    return None


def persist_result(
    store: Store,
    session: str,
    window: Window,
    result: ExtractResult,
    *,
    affect_baseline: dict[str, float] | None = None,
    tz: str = "Asia/Shanghai",
    now: int | None = None,
) -> PersistStats:
    """把一次抽取结果写进库。整个写入在一个事务里（降级兜底除外）。"""
    stats = PersistStats()
    source = user_text_of(window)
    ingest = window.turns[-1].ts if window.turns else (now or 0)

    # 用户这句话是不是在要求"记住"（M16.2）。**每条事件都跟着这句话走**：
    # "记住，我对青霉素过敏，还有我不吃香菜" 抽成两条，两条都要钉。
    # 只钉"最后一条"或"第一条"都是在猜用户的意图。
    pin = 1 if looks_pinned(source) else 0

    # 人生阶段的锚（M17.1）。一次查询，整个窗口共用——同一轮里 5 条事件
    # 说的是同一个人的年龄。
    age = _self_age(store, session, result)

    # ★ 时间解析的基准必须是**这段对话发生的时刻**，不是"现在"。
    #   用 now() 的话，回填三个月前的历史对话时，"昨天"会被解析成真正的昨天，
    #   而不是对话里的昨天——整段历史的时间轴会平移，而且不报错。
    try:
        base_dt = datetime.fromtimestamp(ingest, ZoneInfo(tz or "Asia/Shanghai"))
    except Exception:  # noqa: BLE001
        base_dt = None

    # ---------------------------------------------------------- 1. 锚定（事务外）
    # 先锚定再开事务：锚定是纯文本计算，可能耗时（窗口大时几十 ms），
    # 把它放进事务会让写锁多持有几十 ms，直接和代理的结账抢锁。
    kept: list[tuple[int, Any, str]] = []      # (原下标, EventOut, 锚定后的 raw)
    for i, ev in enumerate(result.events):
        anchored = anchor_raw(ev.raw, source)
        if anchored is None:
            stats.verbatim_fail += 1
            log.warning("verbatim.reject", extra={"fields": {
                "idx": i, "raw_head": (ev.raw or "")[:60]}})
            continue
        kept.append((i, ev, anchored))

    # ---------------------------------------------------------- 2. 实体预解析
    # 一次遍历收齐所有名字，避免边写边 upsert 造成同名实体重复插入。
    #
    # 键是 (名字, 类型) 而不是名字：同一个窗口里"朝阳"可能既是地名又是人名
    # （"我搬到朝阳了，朝阳是我同事"）。只按名字收，后一个会覆盖前一个的类型，
    # 事件就挂到错的那个实体上——而实体一旦挂错，角色卡会带着它长三个月。
    names: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    alias_of: dict[tuple[str, str], list[str]] = {}

    def note(name: str, type_: str = "person", aliases: Sequence[str] = ()) -> None:
        n = (name or "").strip()
        if not n:
            return
        key = (n, type_ or "person")
        if key not in seen:
            seen.add(key)
            names.append(key)
            alias_of[key] = []
        # 去重在这里做，不能指望下游：一次窗口里 5 条事件都带同一组 aliases，
        # extend 会把"王哥"存 5 遍，最后落库的 JSON 数组里全是重复项。
        alias_of[key].extend(a for a in aliases if a and a not in alias_of[key])

    for _, ev, _ in kept:
        for ref in ev.entities:
            note(ref.name, ref.type, ref.aliases)
    for t in result.traits:
        note(t.entity, "person")
    for f in result.facts:
        note(f.entity or SELF, "person")

    # ---------------------------------------------------------- 3. 一个事务
    index_to_id: dict[int, int] = {}
    event_times: dict[int, int] = {}
    try:
        with store.tx():
            entity_ids: dict[tuple[str, str], int] = {}
            for name, type_ in names:
                mt = store.resolve_entity(
                    session=session, name=name, type=type_,
                    aliases=alias_of[(name, type_)], confidence=0.7,
                )
                entity_ids[(name, type_)] = mt.target_id  # type: ignore[assignment]
                if mt.action == "new":
                    stats.entities_new += 1
                else:
                    stats.entities_hit += 1

            for i, ev, anchored in kept:
                # 用 parse_span 而不是 parse_when：事件需要"哪一段时间"，
                # 不只是"哪一刻"。"去年冬天"根本压不成一个点。
                # 用 parse_time 而不是 parse_span：前者能接住"小时候"
                # "疫情那会儿"这两类 parse_span 必然返回 None 的表述（M17.1）。
                span = (parse_time(ev.event_time_hint, ref=base_dt, tz=tz, age=age)
                        if ev.event_time_hint else None)
                event_time = span.start if span else ingest
                # 没有时间线索 = 只能按"对话发生的时刻"记，并标 guessed。
                # 不猜具体的日子：猜错的时间会被检索当成真的用。
                precision = span.precision if span else "fuzzy"
                guessed = span.guessed if span else 1
                event_end = span.end if span else None
                tier, w0 = _arousal_tier(ev.arousal)
                if pin:
                    # 钉住 = 立刻进 hot。不这样做的话，一条被明确要求记住的
                    # 事要等到夜里 recompute 才变 hot，中间这段时间它可能
                    # 根本没进过 prompt——而用户明天就会问"你怎么忘了"。
                    tier = 1
                linked = [entity_ids[(r.name, r.type)] for r in ev.entities
                          if (r.name, r.type) in entity_ids]
                # 关键词清洗必须带实体名上下文：模型常把实体名也塞进
                # keywords，而"猫"这种单字会被长度规则砍掉——它恰恰是最该留的。
                stats.kw_raw += len(ev.keywords or [])
                names_here = [r.name for r in ev.entities]
                kws = clean_keywords(ev.keywords, entities=names_here)
                if not kws and names_here:
                    # 清洗空时退回实体名。留空不致命（trigram 还能靠摘要捞），
                    # 但关键词是唯一能命中 2 字中文词的通道，能救就救。
                    kws = names_here[:3]
                    stats.kw_fallback += 1
                stats.kw_kept += len(kws)
                event_id = store.add_event(
                    session=session,
                    raw=anchored,
                    summary=ev.summary or anchored[:60],
                    keywords=kws,
                    event_time=event_time,
                    event_time_end=event_end,
                    ingest_time=ingest,
                    tier=tier,
                    w0=w0,
                    weight=w0,
                    arousal=ev.arousal,
                    entities=linked,
                    source=ev.source,
                    kind="event",
                    pinned=pin,
                    time_precision=precision,
                    time_guessed=guessed,
                    # 上面已经用实体名上下文清洗过了，这里只做去重。
                    source_text=source,
                    precleaned=True,
                    summary_truncated=int(getattr(ev, "summary_truncated", 0) or 0),
                    summary_flagged=int(getattr(ev, "summary_flagged", 0) or 0),
                )
                index_to_id[i] = event_id
                event_times[i] = event_time
                stats.event_ids.append(event_id)

            # 同一批里，模型会把同一条 trait 挂在每一条事件上（5 条事件 = 5 次）。
            # 直接写会让 confidence 被 +0.1 加四次，一路顶到 0.95——
            # 一次抽取就制造出"极其确定"的假象。先去重，再写一次。
            merged_traits: dict[tuple[str, str], tuple[Any, list[int]]] = {}
            for t in result.traits:
                key = (t.entity, t.trait.strip())
                ev_ids = [index_to_id[i] for i in t.evidence if i in index_to_id]
                if key in merged_traits:
                    prev_t, prev_ev = merged_traits[key]
                    merged_traits[key] = (prev_t, sorted(set(prev_ev) | set(ev_ids)))
                else:
                    merged_traits[key] = (t, ev_ids)

            for (_, _), (t, ev_ids) in merged_traits.items():
                if not ev_ids:
                    # 没有证据的特征就是猜测。宁可不要，也不能让它变成
                    # 三个月后 AI 嘴里的"你就是这样的人"。
                    stats.traits_dropped += 1
                    continue
                eid = entity_ids.get((t.entity, "person"))
                if eid is None:
                    stats.traits_dropped += 1
                    continue
                store.add_trait(entity_id=eid, trait=t.trait, evidence=ev_ids,
                                status=t.status, confidence=0.6, slot=t.slot)
                stats.traits += 1

            for f in result.facts:
                if not f.key or not f.value:
                    continue
                eid = entity_ids.get((f.entity or SELF, "person"))
                # 证据必须指向**真正说了这件事**的那一轮。
                # 原来是 `next(iter(index_to_id.values()))`——永远是窗口里第一条事件。
                # 于是"你凭什么说我住上海"会回到五轮之前的寒暄，
                # 而且这种错永远不会报错，只会让"有据可查"变成"有据但错"。
                ev_idx = [i for i in (f.evidence or []) if i in index_to_id]
                src = index_to_id[ev_idx[0]] if ev_idx else None
                fw = store.set_fact(
                    session=session, key=f.key, value=f.value,
                    entity_id=eid, source_event=src,
                    confidence=f.confidence,
                    # valid_from 取事件发生时间，不取写入时间。
                    when=event_times[ev_idx[0]] if ev_idx else None,
                    label=(f.entity or SELF),
                )
                if fw.action in ("insert", "refine", "replace"):
                    stats.facts += 1
                    if fw.action == "replace":
                        stats.facts_replaced += 1
                    # M18.3：用户在回答里把答案说了 → 对应 doubt 体面关闭。
                    # 只在真的写进了新 fact 时做（"same"说明早就有这条 fact，
                    # doubt 早该被关掉，没关是历史欠账，不在这里顺手清理）。
                    stats.doubts_resolved += resolve_matching_doubts(
                        store, session, [f"{f.key}{f.value}"])
                elif fw.action == "same":
                    stats.facts_same += 1
                elif fw.action == "doubt":
                    stats.facts_doubted += 1
                    stats.doubts += 1        # set_fact 内部已经写进 doubts 表
                elif fw.action == "reject":
                    stats.facts_rejected += 1

            for p in result.plans:
                if not p.content:
                    continue
                parsed, recur = (parse_due(p.due_hint, base=base_dt, tz=tz)
                                 if p.due_hint else (None, None))
                # recur：模型给的字段优先，文本检测兜底（"每天早上7点半"
                # 模型可能没标 recur，但文本里明摆着）。
                recur = p.recur or recur
                if parsed is None:
                    # due_at NOT NULL，解析不出来就不能入库。手册 M19.1.5：
                    # 模糊表述反问一次——"你说 8 点，是明天早上 8 点吗？"
                    # 落 doubt 让 M18.2 的限流器在下轮把它带进上下文。
                    # 注意问题长度必须 ≥8 字，否则会被 M18.1 质检滤掉。
                    stats.plans_dropped += 1
                    hint = p.due_hint or ""
                    if hint:
                        q = f"你说「{hint}」的时候提到{p.content}，具体是哪天几点？"
                        store.add_doubt(session=session, question=q,
                                        context=source[:200],
                                        confidence=0.4, cost=0.2, impact=0.6)
                        stats.doubts += 1
                    log.info("plan.undated", extra={"fields": {
                        "content": p.content[:40], "hint": hint}})
                    continue
                store.add_plan(session=session, due_at=parsed.ts,
                               content=p.content, raw_hint=p.due_hint,
                               recur=recur)
                stats.plans += 1

            candidates = []
            for d in result.doubts:
                if not d.question:
                    continue
                candidates.append(d)

            # M18.1：入库前质检。鸡毛蒜皮滤掉、重复合并、超限淘汰——
            # 模型判过一次"值不值得记"，这里判"值不值得问"。
            if candidates:
                active = store.active_doubts(session, limit=50)
                closed = store.recent_resolved_doubts(session)
                kept, dstats = filter_doubts(candidates, active,
                                             closed_recent=closed)
                stats.doubts_seen = dstats.total
                stats.doubts_filtered = dstats.dropped
                for m in dstats.merged_into_existing:
                    store.bump_doubt_impact(m.doubt_id, m.impact)
                for d in kept:
                    store.add_doubt(session=session, question=d.question,
                                    context=source[:200], confidence=d.confidence,
                                    cost=d.cost, impact=d.impact)
                    stats.doubts += 1
                n = store.prune_doubts(session, keep=MAX_ACTIVE_DOUBTS)
                if n:
                    log.info("doubt.pruned", extra={"fields": {
                        "session": session, "n": n}})

            delta = result.affect_delta
            if not delta.is_zero():
                cur = store.get_affect(session)
                base = affect_baseline or {
                    "joy": 60, "sadness": 10, "anger": 5,
                    "fear": 5, "disgust": 5, "surprise": 20,
                }
                if cur is not None and cur.updated:
                    # 先 lazy 衰减到当前时刻（M20.1），再叠加——
                    # 两条消息隔了一小时，衰减必须发生，否则"气一会儿
                    # 就消"只在没人说话时成立。基准用 ingest（对话发生
                    # 的时刻），和时间解析同一基准：回填历史对话时情绪
                    # 轴不能跟着"现在"平移。
                    moods = decay_moods(
                        {m: getattr(cur, m) for m in _MOODS},
                        baseline=base, tau=TAU_MINUTES,
                        minutes=(ingest - cur.updated) / 60.0)
                else:
                    moods = {m: float(base.get(m, 0)) for m in _MOODS}
                after = apply_delta(
                    moods, {m: getattr(delta, m) for m in _MOODS})
                store.upsert_affect(session, updated=ingest, **after)
                stats.affect_changed = sum(
                    1 for m in _MOODS if abs(after[m] - moods[m]) >= 0.5)

            # M23.2：用户这一轮对 AI 的自主动作表态了吗？
            # 放在最后：它读 ledger 表、可能要写一条懊悔记忆，和上面
            # 那些写入没有依赖关系。失败只记日志不回滚——记忆入库是
            # 主线，结账是支线，不能因为支线出错把主线赔进去。
            _settle_feedback(store, session, result, ingest, stats)
    except StoreError:
        raise
    except Exception as exc:  # noqa: BLE001
        # 回滚是 tx() 做的，这里只补充上下文——裸的 sqlite 报错看不出是哪个窗口
        log.error("persist.failed", extra={"fields": {
            "session": session, "idxs": [window.start_idx, window.end_idx],
            "err": repr(exc)}})
        raise

    return stats


def _settle_feedback(store: Store, session: str, result: ExtractResult,
                     ingest: int, stats: PersistStats) -> None:
    """M23.2：把"用户表扬/批评"结算到最近那次自主动作上。

    负反馈优先。一轮里既夸又骂（"这个不错，但你不该动我配置"），
    负的那半句才是学习信号——夸的那半句往往在客套，骂的那半句
    一定是真的踩到了。
    """
    neg = (getattr(result, "neg_feedback", "") or "").strip()
    pos = (getattr(result, "pos_feedback", "") or "").strip()
    if not neg and not pos:
        return
    text, actual = (neg, -1.0) if neg else (pos, 1.0)
    try:
        st = settle_ledger(store, session, text, actual, now=ingest)
    except Exception as exc:      # noqa: BLE001 - 结账失败不许影响记忆入库
        log.warning("ledger.settle_failed", extra={"fields": {
            "session": session, "err": repr(exc)}})
        return
    if st is None:
        return
    stats.ledger_settled += 1
    if st.regret_id:
        stats.ledger_regret += 1


def persist_degraded(
    store: Store,
    session: str,
    window: Window,
    *,
    tokens: Sequence[str] = (),
) -> int | None:
    """降级：抽取失败时，把窗口原文写成一条 cold 事件。

    **单独一个事务**，且必须能在"抽取彻底失败"的情况下工作。
    它存在的唯一意义是：原文永不丢。十年后你想回填记忆，原文还在。

    summary 留空是刻意的——用模型编的摘要冒充原文，比没有摘要更糟。
    """
    raw = window.text()
    if not raw.strip():
        return None
    ingest = window.turns[-1].ts if window.turns else 0
    try:
        return store.add_event(
            session=session, raw=raw, summary="",
            keywords=clean_keywords(list(tokens))[:8],
            event_time=ingest, ingest_time=ingest,
            tier=3, w0=0.15, weight=0.15, arousal=0.0,
            source="user", kind="raw", time_precision="day", time_guessed=1,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("persist.degrade.failed", extra={"fields": {
            "session": session, "err": repr(exc)}})
        return None
