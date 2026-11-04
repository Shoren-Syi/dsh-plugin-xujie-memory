"""写路径编排：接收 → 编码 → 闸门 → 提交 / 挂起 / 只记录。

设计文档 2.1 / 8：写路径的每一步都不得跳过。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .encoder import SEMANTIC_OPS, Encoder, Interpretation
from .engine import (
    VersionConflict,
    Assertion,
    CommitResult,
    Engine,
    EventAlreadyVoid,
    canonical_json,
    make_state_key,
    relation_of,
)
from . import detect, due as due_mod
from .decay import Lifecycle
from .admission import AdmissionController
from .gate import (
    COMMIT,
    Decision,
    GateContext,
    PENDING,
    RECORD_ONLY,
    REJECT,
    STATE_WRITING_SOURCE_KINDS,
    decide,
)
from .specs import RelationSpec

# 旧版编码器把语义塞在 slots["intent"] 里。保留回落，否则旧库里的解释读出来全是 None。
_LEGACY_INTENT = {"incur": "incur", "repay": "repay", "waive": "waive"}

# 复述句的开头。中文复述一个刚听到的数，几乎总是先应一声再重复：
# 「嗯，十个」「那就三十五」「行，一捆」「对，三坛」。
# 这个表是用来**收窄 echo 判据**的：只靠"数值对得上"会在批量记账时误伤（见 _looks_like_echo）。
_ECHO_OPENERS = ("嗯", "对", "好", "行", "是", "那", "就", "哦", "噢", "呃", "成", "中", "呃")

# 乐观锁冲突后重算「首次 set / 后续 delta」的次数。
#
# 3 次是个经验值：并发冲突通常是"两个线程同时落第一句"，
# 重试一次就够。再多的信息是"这个键被高频争抢"，那是架构问题，
# 靠重试掩盖只会让延迟变成不可预测。
_COMMIT_RETRIES = 3


# 确认类回复的封闭词表。
#
# 为什么用规则而不是模型：确认语是**有限的封闭集**（对/是/嗯/真的），
# 而误判的代价不对称——把「不，我只欠三十」判成确认，会直接提交一个错数。
# 所以这里的策略是**宁可不认，不可认错**：判不准就返回 unclear，
# 让调用方把这句话当正常输入处理（pending 仍然开着）。
_CONFIRM_WORDS = ("对", "是", "嗯", "好", "真的", "没错", "确认", "当然", "确实", "行", "yes", "yeah")
_DENY_WORDS = ("不是", "不对", "没有", "假的", "错了", "才不", "不欠", "别记", "no")

# 超过这个长度就不当"回复"看：太长的句子大概率带了新信息
# （「对，但是我只欠三十」），当成确认会把新信息吞掉。
_REPLY_MAX_LEN = 10

# 多候选时用户怎么指认。他会说"欠账"而不是"debt"。
_RELATION_ALIASES = {
    "debt": ("欠", "债", "账", "米", "粮", "饭"),
    "nickname": ("称呼", "名字", "叫我", "怎么叫"),
    "preference": ("喜好", "喜欢", "讨厌"),
}

# 指认用的短句上限。比确认句更严——「欠三十」里有"欠"，
# 但它是在说新数额，不是在选候选项。
_CHOICE_MAX_LEN = 6

# 数字检测必须认中文数字：「欠三十」里的"三十"是 `isdigit()` 看不见的。
_NUM_CHARS = set("0123456789一二两三四五六七八九十百千万半")

# 转折 / 补充词：出现这些，说明这句话除了"对/不对"还说了别的。
#
# 「对，但是我只欠三十」如果被判成确认，那个"三十"就被吞了——
# 用户会看到系统确认了一个他已经改口的数。
# 长度限制挡不住它（去掉标点只有 9 个字），所以单独判。
_HEDGE_WORDS = ("但", "不过", "可是", "其实", "只", "还", "而且", "另外", "改成", "应该")


def classify_reply(text: str) -> str:
    """判断一句话像不像"对某个待确认项的回复"。

    返回 confirm / deny / unclear。
    **先判否定**：「不对」里含「对」，反过来判会得到相反结论。
    """
    t = "".join(ch for ch in text.strip() if ch not in " \t\n，。！？、,.!?~～…")
    if not t or len(t) > _REPLY_MAX_LEN:
        return "unclear"
    if any(w in t for w in _HEDGE_WORDS):
        return "unclear"
    if any(w in t for w in _DENY_WORDS):
        return "deny"
    if any(w in t for w in _CONFIRM_WORDS):
        return "confirm"
    return "unclear"


@dataclass
class ReplyResult:
    """`Ingest.reply()` 的返回。

    `kind` 决定调用方要不要继续处理这句话：
      confirm   已消费（提交了），别再当新输入
      deny      待确认项已关闭，但**这句话没被消费**——调用方应继续 ingest
      unclear   有 pending，但这句话不像回复；pending 保持，继续 ingest
      no_pending 没有待确认项，继续 ingest
    """

    kind: str
    request_id: str | None = None
    outcome: IngestOutcome | None = None
    # 当前待确认项的完整内容，可直接交给 phrasing.ask() 渲染下一句话。
    # 有它，调用方就不需要再去查一次 clarifications 表。
    request: dict[str, Any] | None = None

    @property
    def consumed(self) -> bool:
        return self.kind == "confirm"

    @property
    def confirmed(self) -> dict[str, Any] | None:
        """被确认的那一条，渲染成 phrasing.acknowledge() 能吃的形状。

        不能直接拿 `request` 去渲染"记下了……"——那是**请求**（可能带两个候选），
        不是**结果**。用错了会输出「记下了，这件事」这种空话。
        """
        if not (self.outcome and self.outcome.interpretation):
            return None
        i = self.outcome.interpretation
        return {"relation": i.relation, "semantic_op": i.semantic_op, "slots": i.slots}


class TargetNotResolvable(Exception):
    """correct / retract 找不到可指向的事件。

    这不是"没有记忆"，是"这句话指向了一个不存在的东西"——
    必须显式处理，不能静默当成新增（那会把一次修正变成一次重复记账）。

    **不往外抛**：`Ingest` 会把它转成 PENDING 澄清请求。
    只有直接调 `_commit` 的内部代码才需要 catch。
    """

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


@dataclass
class IngestOutcome:
    interpretation: Interpretation
    decision: Decision
    event: CommitResult | None = None


@dataclass
class IngestResult:
    trace_id: str
    outcomes: list[IngestOutcome] = field(default_factory=list)
    clarifications: list[dict[str, Any]] = field(default_factory=list)
    # 这句话不是在记账，是在查账（「还剩几捆？」）。
    #
    # 和 clarifications 并列，不是它的子集：澄清是"我要问你"，
    # 这里是"你问我了，钱数是这个"。**两者绝不能互相顶替**——
    # 用户问"还剩多少"，系统回"请补充数量"，是本项目最刺眼的一种答非所问。
    #
    # 形状：{"kind": "balance", "status": "known|unknown|not_found",
    #        "value": 3, "unit": "捆", "state_key": "...", "relation": "debt"}
    answers: list[dict[str, Any]] = field(default_factory=list)
    # 这句话撤掉了哪条锚点（没有则 None）。
    #
    # 单独一个字段，不塞进 outcomes：撤销锚点**没有改状态**，
    # 它只是把"这句要一直留着"改成了"不用留了"。
    # 混进 outcomes 会让调用方以为账变了。
    unanchored: dict[str, Any] | None = None

    @property
    def committed(self) -> list[IngestOutcome]:
        return [o for o in self.outcomes if o.decision.outcome == COMMIT]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for o in self.outcomes:
            counts[o.decision.outcome] = counts.get(o.decision.outcome, 0) + 1
        return counts


# 澄清请求的默认存活时间（秒）。
#
# 30 分钟是对话场景的经验值：太短（比如 60 秒），用户去倒杯水回来就失效了；
# 太长（比如 24 小时），第二天用户回一句「嗯」，系统会拿这句去确认
# 昨天那个早就过时的上下文——而中间可能已经聊过别的。
CLARIFICATION_TTL = 1800

def _plus_seconds(iso: str, seconds: int) -> str:
    """ISO 时间 + N 秒。过期时间在库里存 UTC ISO 字符串，直接字符串比较即可——
    前提是所有时间都出自同一个 `engine.now()`（可注入时钟，阶段 11）。"""
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


# 一轮最多开几个澄清。
#
# 1 个。一句话抽出三个待确认，逐条问三遍，用户会觉得系统在审问他。
# 多出来的不丢，写进 observations（`reason=deferred_clarification`），
# 下轮自然重现时再问——**信息留着，但不当场打断。**
MAX_CLARIFICATIONS_PER_TURN = 1


class Ingest:
    def __init__(
        self,
        engine: Engine,
        encoder: Encoder,
        clarification_ttl: int = CLARIFICATION_TTL,
        max_clarifications: int = MAX_CLARIFICATIONS_PER_TURN,
    ):
        self.engine = engine
        self.encoder = encoder
        # 准入与熔断：决定"这句话要不要用户点头"，并记录被纠正的句式。
        self.admission = AdmissionController(engine)
        # 生命周期：每条痕迹都要有一行，否则衰减、归档、整合周期全都对它无效。
        # 这个 `ensure` 之前只在演示和测试里手动调，写路径上没有——
        # 后果是**真实库里的痕迹永远不会被归档**，整合周期整个空转。
        # v0.34 的端到端验收发现了这一点（lifecycle 表一条记录都没有）。
        self.lifecycle = Lifecycle(engine)
        self.clarification_ttl = clarification_ttl
        self.max_clarifications = max_clarifications

    def ingest(
        self,
        scope_id: str,
        source_ref: str,
        actor_id: str,
        source_kind: str,
        content: str,
        context: dict[str, Any] | None = None,
        *,
        actor_roles: set[str] | None = None,
        confirmed: bool = False,
        admission_mode: str | None = None,
        pattern_escalation: bool = True,
    ) -> IngestResult:
        ctx = context or {}
        trace_id = self.engine.record_trace(
            scope_id, source_ref, actor_id, source_kind, content,
            context_ref=canonical_json(ctx) if ctx else None,
        )
        self.lifecycle.ensure(scope_id, trace_id)
        # 「那个不算」「我刚才说错了」这类句子本身不含任何领域词，
        # 靠关键词判 relation 必然猜错——它们指向的是**上一条**。
        # 所以把"上一条是什么关系"作为上下文传给编码器（XM-14）。
        last_relation, state_hint = self._context_hint(scope_id, ctx.get('_xjm_session'))
        ctx = {**ctx, "last_relation": last_relation, "state": state_hint}

        interps = self.encoder.encode(trace_id, content, {**ctx, "actor_id": actor_id})
        result = IngestResult(trace_id=trace_id)

        # 编码器什么都没说，不等于这句话跟记忆无关（XM-21）。
        #
        # 实测「我上个月是不是欠你五十枚金币来着？」会被判成无关，整段 0/10。
        # 模型没错——它把那句话读成了提问。错的是**没有第二道兜底**：
        # 一句话带着数量、带着债务动词，却因为语气而静默消失，
        # 这不叫"找不到就说找不到"，这叫漏记。
        #
        # 兜底只产出 modality="unclear" 的候选，必然走"问一句"。
        # 最坏结果是多问一句，最好结果是一条账没丢。
        # 「那句可以忘了」——**撤销锚点，不是新记一条**。
        #
        # 必须在 `if not interps` **之前**判：这类句子会被关系兜底接住
        # （「四月十二号那事不用记着了」里没有任何领域词，兜底给 preference），
        # 于是永远进不了"编码器什么都没说"那个分支（实测踩到）。
        #
        # 命中后直接返回：它既不是账、也不是偏好，
        # 不该进澄清队列，更不该被兜底成一条候选。
        # 对一个说"这句可以忘了"的人追问"你是在说哪笔账"，是纯粹的答非所问。
        forget = detect.forget_cue(content)
        if forget:
            undone = self._maybe_unanchor(scope_id, content, forget)
            if undone is not None:
                result.unanchored = undone
                return result
            # 说了"忘了"但找不到对应的锚点 → 落到下面的正常流程。
            # 不在这里返回：宁可按普通话说下去，也不要静默吞掉一句话。

        if not interps:
            # 「记住，四月十二号那天项目上线」——内容是**要留着的话**，
            # 不属于任何关系（它不是一笔账、也不是一个偏好）。
            #
            # 这种句子必须能被锚住：用户说"记住"的时候，
            # 他要的不是"记进某个格子"，而是"这句话以后还在"。
            # 锚是挂在**痕迹**上的（10.4），不依赖它能不能落成状态。
            remember = detect.remember_cue(content)
            if remember:
                self._anchor_only(scope_id, trace_id, content, remember)

            rescued = detect.claim_candidate(
                content, item=ctx.get("item"), unit=ctx.get("unit_hint")
            )
            if rescued is not None:
                interps = [Interpretation(
                    interpretation_id=f"in_{trace_id[-8:]}_rescue",
                    trace_id=trace_id,
                    relation=rescued["relation"],
                    # 兜底不是"我看不懂"，是"我读到了一条账，等你点头"。
                    # 所以用 asserted + seeking_agreement（求确认），
                    # 而不是 unclear——unclear 会让闸门问"这句我拿不准，
                    # 你能再说清楚一点吗"，而用户已经把数量说得很清楚了。
                    #
                    # 承诺例外：它不改余额，不需要点头，直接记成待办。
                    modality="planned" if rescued["semantic_op"] == "promise"
                    else "asserted",
                    polarity="affirm",
                    slots={
                        "quantity": rescued["quantity"],
                        **({"item": rescued["item"]} if rescued["item"] else {}),
                        "due_hint": rescued["due_hint"],
                    },
                    signals={
                        "rescued": rescued["reason"],
                        # 有"是不是/来着"就明确标出来；没有也是待确认，
                        # 因为这是兜底候选，置信度只有 0.3。
                        "seeking_agreement": rescued["confirmation"] or "rescued",
                    },
                    confidence=0.3,
                    semantic_op=rescued["semantic_op"],
                )]

        # 归属槽位先补齐，再进闸门。
        #
        # 为什么不在编码器里做：**归属是结构信息，不是语义**。
        # 点对点对话里「欠债的人是谁、债主是谁」由路由决定，不由这句话决定。
        # 让模型去猜，它会在「先还你点，五碗」这种省略主语的句子上留空，
        # 然后闸门判 missing_slot，用户被追问一个他已经回答过的问题。
        for interp in interps:
            self._normalize_slots(interp)
            self._fill_identity_slots(interp, ctx, actor_id)
            self._apply_surface_cues(interp, content)
            # 方向要在归属补完之后校。模型可以推翻路由的默认，
            # 但必须拿出证据（「你欠我」「你借我」）。
            self._normalize_direction(interp, ctx, actor_id, content)

        self._persist_encoding(trace_id, interps)

        gate_ctx = GateContext(
            scope_id=scope_id, actor_id=actor_id, source_kind=source_kind,
            actor_roles=actor_roles or set(),   # 空表示按关系推导
            peer_id=ctx.get("peer_id"),
            confirmed=confirmed,
            admission_mode=admission_mode,
        )

        for interp in interps:
            try:
                spec = self.engine.registry.get(interp.relation)
            except Exception:
                result.outcomes.append(IngestOutcome(
                    interp, Decision(REJECT, [f"unknown_relation:{interp.relation}"])
                ))
                continue

            gate_ctx.actor_roles = actor_roles or {_default_role(spec.relation)}

            # 这句话属于哪一类句式，要不要用户点头（准入），
            # 以及这个格子现在有没有被熔断。**每句单独算**——
            # 同一轮里的两个候选可能一个放开、一个要问。
            # 外部显式传了 admission_mode 就以外部为准（调试/回放用）。
            pattern = self.admission.pattern_of(
                semantic_op_of(interp, spec),
                bool((interp.slots or {}).get("quantity") is not None),
                interp.signals,
                interp.modality,
            )
            interp.signals = {**interp.signals, "pattern": pattern}
            level = admission_mode or self.admission.level(
                spec.relation, pattern, spec.admission_mode,
                escalate=pattern_escalation,
            )
            gate_ctx.admission_mode = level
            interp.signals = {**interp.signals, "admission_level": level}

            # 「也算进去」+ 账上真有那笔 → 这是确认，不是新账（XM-27）。
            # 判定要读事件流，所以只能放在这里，不能放进闸门
            # （闸门是纯函数，看不到账本）。
            op_now = semantic_op_of(interp, spec)
            if not interp.signals.get("restatement"):
                st_key, _, st_args = resolve(interp, spec, scope_id)
                if self._looks_like_restatement(content, st_key, op_now,
                                                st_args.get("quantity")):
                    interp.signals = {
                        **interp.signals,
                        "restatement": detect.restatement_cue(content),
                    }
                # 「那我欠你三斤。」——账上正好三斤，他不是又欠三斤。
                # 光靠字面判不出来（没有"也算""刚才那"），但数值对得上就是证据。
                elif self._looks_like_echo(content, st_key, op_now,
                                           st_args.get("quantity")):
                    interp.signals = {**interp.signals, "restatement": "echo"}

            # 「还欠你三本」在账上**还没有数**时，不是复核，是第一次说。
            #
            # 闸门 (3c) 看到"还欠 + 数量 + 来着"就判 record_only，它看不到账，
            # 所以不知道账上是不是空的。空账时照判，这一笔就永远进不来
            # （holdout_v2/dev_a 第 1 句：整段 0/4，后面三句全崩）。
            #
            # 判据：能不能比对。比不了（没账 / 读不出数）→ 摘掉标记，走正常路。
            if interp.signals.get("balance_check"):
                if self._balance_said_value(interp, spec, scope_id) is None:
                    interp.signals = {
                        k: v for k, v in interp.signals.items()
                        if k != "balance_check"
                    }

            decision = decide(interp, spec, gate_ctx, siblings=interps)

            # set_total 会抹掉既有增量时，先问一句（XM-20）。
            #
            # 放在闸门之后：闸门只看这句话，看不到账上发生过什么。
            # 也只有 COMMIT 才需要拦——其他去向本来就不改状态。
            #
            # 用户点头之后走 `_settle`，那条路不经过这里，所以能落下。
            # 触发条件是"这一步本来会被放行或被要求确认"：
            #   COMMIT                    —— auto 关系直接放行
            #   PENDING + 只差一个确认    —— assisted 关系（debt 就属于这种）
            #
            # 不能只判 COMMIT：R2 是 assisted，闸门在 (7b) 就已经返回 PENDING，
            # 那个分支永远走不到（第一版就是这么写的，测试没覆盖到）。
            # 也不能替换所有 PENDING：缺槽位的问句被换成"会作废几笔"，
            # 用户会答非所问。
            only_needs_confirm = all(
                r in ("confirmation_required", "admission_assisted",
                      "speaker_seeking_agreement")
                for r in decision.reasons
            )
            gate_open = decision.outcome == COMMIT or (
                decision.outcome == PENDING and only_needs_confirm
            )

            # 两种问法的优先级：
            #   ① 「现在还欠 40 / 当初一共 40」——更具体，而且给了一个**不作废**的选项
            #   ② 「这么改会作废 2 笔还款」——只在读法唯一（总额比已还还少）时才用
            #
            # 顺序反过来，用户先听到"会作废几笔"，可能直接说"不"，
            # 而其实他想要的是"当初一共 40"——那个答案根本不会作废任何东西。
            # 「还差一百二对吧？」——说的数和账面对得上吗（XM-38）。
            #
            # 对得上：他就是在确认，记旁注即可。
            # 对不上：一边是错的。系统不知道哪边错，但**必须说出来**，
            #         否则用户会带着错的认知继续记账。
            if (interp.signals.get("balance_check")
                    and not interp.signals.get("balance_checked")):
                said = self._balance_said_value(interp, spec, scope_id)
                if said is not None:
                    interp.signals = {**interp.signals, "balance_checked": True}
                    if said["matches"] is False:
                        decision = Decision(
                            PENDING, [*decision.reasons, "balance_mismatch"],
                            {"kind": "balance_check", "relation": spec.relation, **said},
                        )
                    else:
                        decision = Decision(
                            RECORD_ONLY, [*decision.reasons, "balance_check"],
                        )

            # 「还清了」但账上还差着——这是矛盾，不是闲聊（XM-34）。
            #
            # 余额为 0：他就是在确认，记一条旁注即可，不该再动状态。
            # 余额不为 0：他可能漏记了一笔，也可能记错了——两种都要问，
            #             "我以为清了、其实没清"是这套东西最该抓住的时刻。
            # 不看 outcome：闸门 (3a) 已经把它判成 record_only 了，
            # 这里要做的是**把 record_only 改回 pending**（账还差着的时候）。
            if (interp.signals.get("settled_report")
                    and not interp.signals.get("settled_checked")):
                gap = self._settled_gap(interp, spec, scope_id)
                if gap is not None:
                    interp.signals = {**interp.signals, "settled_checked": True}
                    if gap:
                        decision = Decision(
                            PENDING, [*decision.reasons, "settled_but_outstanding"],
                            {"kind": "settled_gap", "relation": spec.relation,
                             "settled_report": interp.signals["settled_report"], **gap},
                        )
                    else:
                        decision = Decision(
                            RECORD_ONLY, [*decision.reasons, "settled_report"],
                        )

            readings = None
            if gate_open and not interp.signals.get("total_reading"):
                readings = self._set_total_readings(interp, spec, scope_id)

            if gate_open and not readings:
                info = self._overwrite_scope(interp, spec, scope_id)
                if info:
                    decision = Decision(
                        PENDING, [*decision.reasons, "total_overrides_increments"],
                        {"kind": "total_overrides", "relation": spec.relation, **info},
                    )

            if readings:
                alt = Interpretation(
                    interpretation_id=f"{interp.interpretation_id}_gross",
                    trace_id=interp.trace_id, relation=interp.relation,
                    modality=interp.modality, polarity=interp.polarity,
                    slots={**interp.slots, "target_hint": "first_incur"},
                    signals={**interp.signals, "total_reading": "gross"},
                    semantic_op="correct",
                )
                self._persist_encoding(trace_id, [alt])
                self._record_decision(alt, Decision(PENDING, ["total_reading_ambiguous"]), spec)
                detail = {
                    "kind": "total_reading",
                    "relation": spec.relation,
                    **readings,
                    "options": [
                        {"interpretation_id": interp.interpretation_id,
                         "reading": "balance", "value": readings["gross"]},
                        {"interpretation_id": alt.interpretation_id,
                         "reading": "gross", "value": readings["remaining"]},
                    ],
                }
                decision = Decision(PENDING, [*decision.reasons, "total_reading_ambiguous"], detail)
                result.outcomes.append(IngestOutcome(
                    alt, Decision(PENDING, ["total_reading_ambiguous"], detail)
                ))

            self._record_decision(interp, decision, spec)
            event = None

            if decision.outcome == COMMIT:
                try:
                    event = self._commit(interp, spec, scope_id, actor_id, trace_id, ctx)
                except TargetNotResolvable as exc:
                    # 「我记错了」但账上没有可改的那一条——这是**正常场景**，
                    # 不该让调用方去 catch。正确反应是问用户"你指哪一笔"。
                    #
                    # 抛异常的话，调用方必须知道这个异常的存在，否则整条链路崩掉
                    # （评估脚本就这么崩过一次）。判定应该以 Decision 的形式返回。
                    decision = Decision(PENDING, ["unresolvable_target"], {
                        "kind": "unresolvable_target", "relation": spec.relation,
                        "hint": getattr(exc, "hint", None),
                    })
                    self._record_decision(interp, decision, spec)
            elif decision.outcome == RECORD_ONLY:
                # 承诺也走 record_only，但**记法不同**。
                #
                # 闸门对承诺的判定本来就没变：`planned` 不是实际陈述，
                # 不放行改状态。变的只是"记在哪"——
                # 以前记进 observations（一段没有数量、没有到期日的旁注），
                # 现在记成一条无效果事件（quantity + due_hint，可追踪、可结清）。
                #
                # 为什么不让它走 COMMIT：COMMIT 意味着"这句话改了状态"，
                # 而承诺一个字节都没改。用它喜欢的那个格子，别为了顺手改语义。
                # 只有**真的在承诺**（planned）才记成承诺对象。
                # 「要是明天还你 20 碗」是条件句，不是承诺——
                # 它连"要不要还"都没定，记下来只会在到期时冤枉人。
                if (semantic_op_of(interp, spec) == "promise"
                        and interp.modality == "planned"
                        and not interp.signals.get("third_party")):
                    p_key, _, p_args = resolve(interp, spec, scope_id)
                    try:
                        event = self._commit_promise(
                            interp, spec, p_key, p_args,
                            scope_id=scope_id, actor_id=actor_id,
                            trace_id=trace_id, ctx=ctx,
                        )
                    except (ValueError, TargetNotResolvable):
                        # 没有数量就说"下个月还你"——记不成承诺，落回旁注。
                        # 静默丢弃比记错更糟。
                        self._record_observation(
                            scope_id, trace_id, interp, decision, content
                        )
                else:
                    self._record_observation(scope_id, trace_id, interp, decision, content)

            # 锚点判定放在**判完之后**：它不参与判定，
            # 只在"这句话确实被记下了"之后才决定要不要钉住它。
            #
            # 什么时候算"记下了"：
            #   COMMIT       —— 改了状态
            #   RECORD_ONLY  —— 存成旁注/承诺（"记住我以后不喝咖啡了"就走这里）
            #   PENDING      —— 还没定，等用户点头（确认时再补，见 _settle_owned）
            #
            # 为什么不为 PENDING 设锚点：这句话还没成立。
            # 锚住一条还没被承认的事实，等于把猜测当证据钉死。
            #
            # 为什么只在**能写状态的通道**上设锚点：锚点代表"用户明确要求留住这条"。
            # 管家（NPC）说「记下了，两袋米，先挂在您名下」里有「记下」，
            # 会被当成"记住"的指令——但那是**它**在记账，不是用户要求留住。
            # 钉住的应该是用户的诉求，不是 NPC 的口头禅。
            if (decision.outcome in (COMMIT, RECORD_ONLY)
                    and source_kind in STATE_WRITING_SOURCE_KINDS):
                self._maybe_anchor(interp, spec, scope_id, trace_id, content)

            result.outcomes.append(IngestOutcome(interp, decision, event))

            # 「还剩几捆？」闸门只管不落库，用户要的是那个数。
            # 这里查一次账，把答案挂回结果——闸门是纯函数，看不到账本。
            if interp.signals.get("balance_query"):
                answer = self._answer_balance(interp, spec, scope_id, ctx)
                if answer:
                    result.answers.append(answer)

        # 澄清统一在循环后开，因为歧义要**合并**成一条问句。
        #
        # 之前每个候选各开一条请求：模型把「我好像还欠你四十碗」同时判成
        # debt 和 nickname，用户就会连着被问两次「你是在说欠款吗」
        # 「你是在说称呼吗」。同一句话只该问一次（XM-03）。
        result.clarifications.extend(
            self._open_clarifications(scope_id, result.outcomes, trace_id, content)
        )

        return result

    # ------------------------------------------------------------- 锚点
    def _maybe_anchor(
        self,
        interp: Interpretation,
        spec: RelationSpec,
        scope_id: str,
        trace_id: str,
        content: str,
    ) -> str | None:
        """判断这条要不要设成**重点锚点**。

        锚点是什么：一条**不许被时间拿走**的记忆（10.4）。
        它只影响保留与排序，不代表这条更可信（I7）。

        判据只有三条，全部来自**用户明确表达过的意图**，
        不是模型推测的"这句话好像挺重要"：

            1. 用户说了「记住……」        —— 显式指令，最硬
            2. 这是一次纠正 / 撤销        —— 用户专门回头改，说明他在意
            3. 这是一条有到期日的承诺     —— 有 deadline 的事不能忘

        为什么不做"重要的事自动锚点"：那需要一个价值判断模型，
        而价值判断错误的代价是**永久保留一条错的东西**——
        比漏锚一条普通记忆严重得多。宁可少锚，不可乱锚。

        为什么不锚在**状态**上而锚在**痕迹**上：状态可以被纠正、撤销、
        重算；痕迹是原始证据。"这句话我要它一直在"是关于痕迹的诉求。

        返回锚点原因（没有则 None），方便测试与演示。
        """
        reason: str | None = None

        cue = detect.remember_cue(content)
        if cue:
            reason = f"remember_cue:{cue}"

        if reason is None:
            op = semantic_op_of(interp, spec)
            if op in ("correct", "retract"):
                # 用户专门回头改一笔——他记得它，所以它值得留住。
                reason = f"revision:{op}"

        if reason is None:
            op = semantic_op_of(interp, spec)
            if op == "promise":
                # 有到期日的承诺：到期要提醒，所以不能被时间冲掉。
                #
                # 关键是「到期日」必须**可执行**。「回头还你」「以后给你补」
                # 只有 due_hint，没有 due_at——「回头」没有可催的日子。
                # 拿模糊词当锚点，等于把「回头」这类空头承诺永久钉住，
                # 钉住的还是一条谁都不确定的旧账（demo d01 第 3 句）。
                due_at = (interp.slots or {}).get("due_at")
                due_hint = (interp.slots or {}).get("due_hint")
                if due_at:
                    reason = f"promise_with_due:{due_at}"
                elif due_hint and not due_mod.is_vague(due_hint):
                    reason = f"promise_with_due:{due_hint}"

        if reason is None:
            return None

        self.lifecycle.set_anchor(trace_id, True)
        self._write_anchor_note(scope_id, trace_id, reason, content,
                                interp.interpretation_id, interp.relation,
                                interp.modality, interp.polarity)
        return reason

    def _anchor_only(self, scope_id: str, trace_id: str, content: str,
                     cue: str) -> str:
        """只有锚点、没有关系的句子。

        「记住，四月十二号那天项目上线」这类话不属于任何关系——
        它不是一笔账、也不是一个偏好。但用户明确要求留住它。

        锚挂在**痕迹**上（10.4），所以「能不能落成状态」和
        「它会不会被记住」是两件事。这里只做后者。
        """
        reason = f"remember_cue:{cue}"
        self.lifecycle.set_anchor(trace_id, True)
        self._write_anchor_note(scope_id, trace_id, reason, content,
                                f"in_{trace_id[-8:]}_anchor", "-",
                                "asserted", "affirm")
        return reason

    def _write_anchor_note(self, scope_id: str, trace_id: str, reason: str,
                           content: str, interpretation_id: str,
                           relation: str, modality: str,
                           polarity: str, prefix: str = "anchor") -> None:
        """锚点要留一条可读的旁注，否则事后只能看到 lifecycle 里一个 1。"""
        self.engine._conn().execute(
            "INSERT OR REPLACE INTO observations(observation_id, scope_id, trace_id,"
            " interpretation_id, relation, modality, polarity, slots_json, excerpt,"
            " reason, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"ob_{prefix}_{trace_id[-12:]}", scope_id, trace_id,
                interpretation_id, relation, modality, polarity,
                canonical_json({"anchor_reason": reason}),
                content[:200], reason.split(":", 1)[0], "open",
                self.engine.now(),
            ),
        )

    # --------------------------------------------------------- 撤销锚点
    # 两个锚点内容重叠到这个长度，才认为用户在说同一件事。
    #
    # 3 个字是下限：「项目上线」「四月十二」都能命中，
    # 而「不用记了」「那句话」这种功能词拼不出 3 字的连续子串。
    _UNDO_MIN_OVERLAP = 3

    def _maybe_unanchor(self, scope_id: str, content: str,
                        cue: str) -> dict | None:
        """用户要求撤掉某条锚点。返回被撤掉的那条（没有合适的则不撤）。

        定位顺序，从最确定到最含糊：

            1. 内容重叠    「四月十二号那事不用记了」→ 找共享片段最长的
            2. 指代词      「那句可以忘了」→ 撤最近设的那条
            3. 都对不上    不动

        **宁可没撤，不可撤错。** 撤错的代价是"用户交代要留的话被拿走了"，
        比"用户说了忘但还留着"严重得多。

        所以没有"只剩一条就撤它"这种图省事的规则：
        「那两袋不用记了」是在说**一笔账**（该走 waive），
        不是撤锚点——它既没有足够的内容重叠，也没有指代词，
        正好被挡在门外。
        """
        target = self._resolve_undo_target(scope_id, content)
        if target is None:
            return None
        trace = self.engine.get_trace(target["ref_id"])
        if trace is None:
            return None

        # 只撤锚，不删痕迹。
        # 「不用记着了」是"别再拿它占我的心力"，不是"当这句话没说过"。
        # 痕迹是原始证据，删了就没法回答"他到底说没说过"。
        self.lifecycle.set_anchor(target["ref_id"], False)
        self._write_anchor_note(
            scope_id, target["ref_id"], f"unanchor:{cue}",
            trace["content"], f"in_{target['ref_id'][-8:]}_unanchor",
            "-", "asserted", "affirm", prefix="unanchor",
        )
        return {"trace_id": target["ref_id"], "content": trace["content"],
                "cue": cue}

    def _resolve_undo_target(self, scope_id: str, content: str) -> dict | None:
        """这句话要撤哪一条锚点。"""
        anchors = self.lifecycle.anchored(scope_id)
        if not anchors:
            return None

        # 按说出来时间倒序，让「最近那条」有确定含义。
        # `anchored()` 的返回顺序不可依赖（SQL 没有 ORDER BY）。
        def _recorded_at(item: dict) -> str:
            tr = self.engine.get_trace(item["ref_id"])
            return str(tr["recorded_at"]) if tr else ""

        ordered = sorted(anchors, key=_recorded_at, reverse=True)

        best, best_score = None, 0
        for item in ordered:
            tr = self.engine.get_trace(item["ref_id"])
            if tr is None:
                continue
            score = detect.longest_shared_span(content, tr["content"])
            if score > best_score:
                best, best_score = item, score
        if best is not None and best_score >= self._UNDO_MIN_OVERLAP:
            return best

        if detect.undo_pronoun(content):
            return ordered[0]

        return None


    # ------------------------------------------------------------- 澄清
    def confirm(self, request_id: str, actor_id: str, answer_text: str = "确认") -> IngestOutcome | None:
        """回答一条澄清请求。

        回答本身进入写路径，形成新痕迹（6.10）；但它指向的是**指定的那条解释**，
        不是"最近一条"，避免答非所问被误绑（8.6）。

        多候选时必须用 `choose()` 指定选哪条——「确认」在这种场景下是歧义的：
        它可能表示"都对"，而两条候选指向的是不同关系，不可能都写入。
        """
        row = self._open_row(request_id)
        if row is None:
            return None
        interp_ids = json.loads(row["interpretation_ids_json"])
        if len(interp_ids) > 1:
            raise ValueError(
                f"{request_id} 有 {len(interp_ids)} 个候选，请用 choose() 指定"
            )
        return self._settle(row, interp_ids[0], actor_id, answer_text)

    def choose(
        self, request_id: str, interpretation_id: str, actor_id: str,
        answer_text: str = "是这条",
    ) -> IngestOutcome | None:
        """在多候选里选一条。

        选定即视为确认：用户已经花了一次交互表明"我指的是这个"，
        再追一句"确定吗"只是把成本转嫁给用户。
        """
        row = self._open_row(request_id)
        if row is None:
            return None
        interp_ids = json.loads(row["interpretation_ids_json"])
        if interpretation_id not in interp_ids:
            raise ValueError(f"{interpretation_id} 不在 {request_id} 的候选里")
        return self._settle(row, interpretation_id, actor_id, answer_text)

    def reply(self, scope_id: str, text: str, actor_id: str) -> ReplyResult:
        """把一句话当作对「最近一条待确认」的回复来处理。

        这是"无感确认"的入口：AI 问「主人，你说的给鲸鲸 40 碗是真的吗？」，
        用户回「嗯」「对呀」「是真的」——调用方不需要知道 request_id，
        直接把这句话交给 reply() 就行。

        返回的 `kind` 告诉调用方这句话有没有被消费：

            confirm     已提交，不要再当新输入
            deny        待确认项关掉了，但这句还要当新输入处理
            ambiguous   多候选时"对"没有指认对象，需要再问一句
            unclear     不像回复；待确认项仍然开着
            no_pending  没有待确认项
        """
        row = self._latest_open(scope_id)
        if row is None:
            return ReplyResult("no_pending")

        request = self._row_to_request(row)
        interp_ids = json.loads(row["interpretation_ids_json"])

        # 多候选时，先看这句是不是在**指认**（「欠账」「称呼」）。
        # 指认优先于确认：用户说"欠账"是在回答"哪一个"，
        # 而"对"在这种场景下没有指认对象。
        if len(interp_ids) > 1:
            picked = self._pick_candidate(row, text, actor_id)
            if picked is not None:
                return ReplyResult("confirm", row["request_id"], picked, request)

        # 「一共欠 40」的二选一：说"总共/当初"是后者，说"还欠/剩下"是前者。
        # 这里必须先判，因为"对"在这种语境下没有指认对象——
        # 两个选项都叫"对"（对，是现在还欠 / 对，是当初一共），意思相反。
        if request.get("kind") == "total_reading":
            picked = self._pick_reading(request, text, actor_id)
            if picked is not None:
                return ReplyResult("confirm", row["request_id"], picked, request)
            return ReplyResult("ambiguous", row["request_id"], None, request)

        kind = classify_reply(text)
        if kind == "unclear":
            return ReplyResult("unclear", row["request_id"], None, request)

        if kind == "confirm":
            if len(interp_ids) > 1:
                # 「对」在多候选下没有指认对象。这时强行确认，等于系统替用户
                # 在"欠款"和"称呼"之间做选择——而多候选恰恰说明模型自己也不确定。
                return ReplyResult("ambiguous", row["request_id"], None, request)
            outcome = self._settle(row, interp_ids[0], actor_id, text)
            return ReplyResult("confirm", row["request_id"], outcome, request)

        # deny：关掉这条请求，但不消费这句话——用户可能接着给出正确说法
        # （「不，我只欠三十」里的"三十"还得进正常写路径）。
        self.engine._conn().execute(
            "UPDATE clarifications SET status='dismissed' WHERE request_id=?",
            (row["request_id"],),
        )
        return ReplyResult("deny", row["request_id"], None, request)

    def _row_to_request(self, row) -> dict[str, Any]:
        detail = json.loads(row["detail_json"] or "{}")
        return {"request_id": row["request_id"], **detail}

    def _pick_reading(self, request: dict[str, Any], text: str, actor_id: str):
        """从两种读法里挑一个。挑不出来就返回 None，让调用方再问一句。"""
        gross_words = ("总共", "一共", "当初", "原来", "本来", "起初", "最初")
        balance_words = ("还欠", "剩下", "剩", "现在", "目前", "余额")
        wants_gross = any(w in text for w in gross_words)
        wants_balance = any(w in text for w in balance_words)
        if wants_gross == wants_balance:        # 都没说，或都说了
            return None
        target = "gross" if wants_gross else "balance"
        for opt in request.get("options", []):
            if opt.get("reading") == target:
                return self.choose(request["request_id"], opt["interpretation_id"],
                                   actor_id, text)
        return None

    def _pick_candidate(self, row, text: str, actor_id: str) -> IngestOutcome | None:
        """多候选时按「欠账 / 称呼」这类短句指认。

        限制比确认句更严（≤6 字、必须含别名、不能带数字）：
        「欠三十」里有"欠"，但它说的是新数额，不是选候选项。
        认错的话会把一次改口变成一次选边——那比多问一句糟得多。
        """
        t = "".join(ch for ch in text.strip() if ch not in " \t\n，。！？、,.!?~～…")
        if not t or len(t) > _CHOICE_MAX_LEN:
            return None
        if any(ch in _NUM_CHARS for ch in t):
            return None

        detail = json.loads(row["detail_json"] or "{}")
        for cand in detail.get("candidates", []):
            aliases = _RELATION_ALIASES.get(cand.get("relation") or "", ())
            if aliases and any(w in t for w in aliases):
                return self._settle(row, cand["interpretation_id"], actor_id, text)
        return None

    # expires_at IS NULL 的老记录（XM-03 修复前写入的）按「永不过期」处理，
    # 否则升级代码会把所有旧请求一起作废。
    _OPEN_FILTER = (
        "status='open' AND (expires_at IS NULL OR expires_at > ?)"
    )

    def _latest_open(self, scope_id: str):
        self.expire_stale(scope_id)
        return self.engine._conn().execute(
            "SELECT * FROM clarifications WHERE scope_id=? AND " + self._OPEN_FILTER +
            " ORDER BY created_at DESC LIMIT 1",
            (scope_id, self.engine.now()),
        ).fetchone()

    def unasked_clarifications(self, scope_id: str) -> list[dict[str, Any]]:
        """这个作用域里**还没回答**的澄清。

        为什么需要公开这个：宿主（如 DSH）里没有「用户点确认」这个动作。
        澄清开出来之后就那么悬着，而材料只反映**当轮**——
        于是那件事从此没人再提。

        实测：第 1 轮「我叫小鲸」判成称呼、开了一条澄清，
        到第 5 轮材料里已经完全没有它的痕迹，五轮之后系统仍不认识这个人。
        **问过却没再提，等于没问。**

        只给**还没念过**的（`asked_at IS NULL`）：宿主每轮都会重拼材料，
        同一条问题念五遍，那不是澄清，是审问。

        和 `open_clarifications` 分开两个方法，不是加参数：
        那个是**公开 API**，一堆测试在用，返回全字段。
        改签名会让调用方从"拿到字典"变成"拿到裁剪过的字典"，
        而且错误要在升级后才暴露。**加方法，不改旧方法。**
        """
        self.expire_stale(scope_id)
        rows = self.engine._conn().execute(
            "SELECT * FROM clarifications WHERE scope_id=? AND " + self._OPEN_FILTER +
            " AND asked_at IS NULL ORDER BY created_at",
            (scope_id, self.engine.now()),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_asked(self, request_ids: list[str], session: str | None = None) -> int:
        """把这几条标成「已经念给模型看过了」。

        语义是"已提过"，不是"已回答"。标过之后不再主动提第二次——
        用户没接话就是没接话，追着问只会让人烦。
        """
        if not request_ids:
            return 0
        now = self.engine.now()
        marks = ",".join("?" for _ in request_ids)
        cur = self.engine._conn().execute(
            f"UPDATE clarifications SET asked_at=?, asked_session=?"
            f" WHERE request_id IN ({marks}) AND asked_at IS NULL",
            (now, session, *request_ids),
        )
        return cur.rowcount or 0

    def asked_count(self, scope_id: str, session: str | None = None) -> int:
        """这个作用域累计**主动问过**多少次。熔断用。

        给了 `session` 就只数这个会话里问过的（v0.52：记忆跨会话共享后，
        熔断仍然按会话算，否则问满一次额度就终身不再问）。
        """
        if session is None:
            row = self.engine._conn().execute(
                "SELECT COUNT(*) c FROM clarifications"
                " WHERE scope_id=? AND asked_at IS NOT NULL",
                (scope_id,),
            ).fetchone()
        else:
            row = self.engine._conn().execute(
                "SELECT COUNT(*) c FROM clarifications"
                " WHERE scope_id=? AND asked_at IS NOT NULL AND asked_session=?",
                (scope_id, session),
            ).fetchone()
        return int(row["c"]) if row else 0

    def _open_row(self, request_id: str):
        return self.engine._conn().execute(
            "SELECT * FROM clarifications WHERE request_id=? AND " + self._OPEN_FILTER,
            (request_id, self.engine.now()),
        ).fetchone()

    def _settle(
        self, row, interp_id: str, actor_id: str, answer_text: str
    ) -> IngestOutcome | None:
        request_id = row["request_id"]

        # 抢所有权：只有把 open 改成 answering 的那个调用继续。
        #
        # 没有这一步，两个线程会同时读到 status='open'，各自走完全程：
        # 第一个提交成功，第二个带着同一个幂等键但**不同的载荷**
        # （第一个是 set，第二个因为 `_first_set_rule` 变成 delta），
        # 于是抛 `IdempotencyConflict`。
        #
        # 那个异常的含义是"同一个键被用于不同载荷"，而这里的真实情况是
        # "这件事已经被处理过了"。用错异常，调用方会当 bug 去查。
        with self.engine._tx() as c:
            owned = c.execute(
                "UPDATE clarifications SET status='answering'"
                " WHERE request_id=? AND status='open'",
                (request_id,),
            )
            if owned.rowcount != 1:
                return None

            return self._settle_owned(row, interp_id, actor_id, answer_text)

    def _settle_owned(
        self, row, interp_id: str, actor_id: str, answer_text: str
    ) -> IngestOutcome | None:
        """已经抢到澄清请求的所有权，剩下的事都在同一个事务里完成。"""
        interp = self._load_interpretation(interp_id)
        if interp is None:
            return None

        spec = self.engine.registry.get(interp.relation)

        # 用户点"确认"，是这套系统里最强的"这是真的"证据。
        # 编码器判了 unclear（"我好像还欠你四十碗"里的"好像"），
        # 如果确认之后还是 unclear，那"确认"这个动作就白做了——
        # 用户永远无法把一句含糊的话变成事实。
        #
        # 所以：确认时把 unclear 提升为 asserted。polarity 不动
        # （"我不欠你"+ 确认 = 确认"不欠"这件事）。
        signals = dict(interp.signals)
        # 点头就是"求确认"要的那个答复。留着这个信号，闸门会永远要求确认——
        # 用户点了头，系统还在问"真的吗"，那一句就永远提交不了（死循环）。
        signals.pop("seeking_agreement", None)
        # 「还欠你三本」在空账上被判过"复核"，用户点头之后它已经有了确定含义：
        # 这是一笔账。留着标记，闸门会再判一次 record_only，那这一句永远进不来
        # （holdout_v2/dev_a 第 1 句卡在这）。
        #
        # 但**账面和用户说的数对不上**的复核不一样（kind == "balance_check"）：
        # 那时候用户说"我欠你四罐"、账面是五罐，用户点头只是认领这句话，
        # 不是认领"我还你四罐"这件事。要是照旧把标记摘掉，闸门会重判，
        # semantic_op 还是 repay，于是真落一笔还款——**复核变成了扣款**。
        # 所以这一类**留着** balance_check，让闸门 (3c) 判 record_only，余额不动。
        if row["kind"] != "balance_check":
            signals.pop("balance_check", None)
        signals.pop("recall_check", None)
        signals["confirmed_by_user"] = True

        if interp.modality == "unclear":
            interp = Interpretation(
                interpretation_id=interp.interpretation_id, trace_id=interp.trace_id,
                relation=interp.relation, modality="asserted",
                polarity=interp.polarity, slots=dict(interp.slots),
                signals={**signals, "promoted_by_confirmation": True},
                semantic_op=interp.semantic_op,
            )
        else:
            interp = Interpretation(
                interpretation_id=interp.interpretation_id, trace_id=interp.trace_id,
                relation=interp.relation, modality=interp.modality,
                polarity=interp.polarity, slots=dict(interp.slots),
                signals=signals, semantic_op=interp.semantic_op,
            )

        request_id = row["request_id"]
        answer_trace = self.engine.record_trace(
            row["scope_id"], f"answer-{request_id}", actor_id, "user_statement", answer_text,
        )
        gate_ctx = GateContext(
            scope_id=row["scope_id"], actor_id=actor_id, source_kind="user_statement",
            actor_roles={_default_role(interp.relation)}, confirmed=True,
        )
        decision = decide(interp, spec, gate_ctx, siblings=[interp])
        self._record_decision(interp, decision, spec)

        event = None
        if decision.outcome == COMMIT:
            # 走同一条提交路径。之前这里是一份手抄的副本，
            # 结果 _first_set_rule 只加在了 ingest 那边，confirm 算出另一个数（踩过）。
            try:
                event = self._commit(
                    interp, spec, row["scope_id"], actor_id, interp.trace_id, {},
                    extra_evidence=(answer_trace,),
                )
                # 用户点头了，而且这条是辅助通道（熔断后的格子也在这一条）。
                # 记一次成功——攒够了就把格子放回自动。
                #
                # 为什么不给 auto 的提交记成功：auto 没有人点头，
                # "没被纠正"不等于"对"。**只给明确确认过的记成功**，
                # 这样恢复的依据和熔断的依据是同一种强度。
                pattern = str((interp.signals or {}).get("pattern") or "")
                if pattern:
                    self.admission.record_success(spec.relation, pattern)
                # 锚点判定要在这里补一次。
                #
                # 为什么：纠正/撤销正好走这条路——它们在原句上被判 PENDING
                # （要用户点头），点头之后才提交。
                # 如果只在 ingest 里判锚点，纠正就永远拿不到锚点，
                # 而"用户专门回头改一笔"恰恰是最该留住的一句。
                #
                # 锚在**原句**的痕迹上，不是"确认"这一句上——
                # 用户点头是行为，原始表达才是记忆本体。
                origin = self.engine.get_trace(interp.trace_id)
                if origin is not None:
                    self._maybe_anchor(
                        interp, spec, row["scope_id"], interp.trace_id,
                        origin["content"],
                    )
            except TargetNotResolvable as exc:
                decision = Decision(PENDING, ["unresolvable_target"], {
                    "kind": "unresolvable_target", "relation": spec.relation,
                    "hint": getattr(exc, "hint", None),
                })
                self._record_decision(interp, decision, spec)

        self.engine._conn().execute(
            "UPDATE clarifications SET status='answered' WHERE request_id=?", (request_id,)
        )
        return IngestOutcome(interp, decision, event)

    def _load_interpretation(self, interp_id: str) -> Interpretation | None:
        row = self.engine._conn().execute(
            "SELECT * FROM interpretations WHERE interpretation_id=?", (interp_id,)
        ).fetchone()
        if row is None:
            return None
        keys = row.keys()
        interp = Interpretation(
            interpretation_id=row["interpretation_id"],
            trace_id=row["trace_id"],
            relation=row["relation"],
            modality=row["modality"],
            polarity=row["polarity"],
            slots=json.loads(row["slots_json"]),
            signals=json.loads(row["signals_json"] or "{}"),
            # 旧库没有这一列；NULL 表示走 slots["intent"] 回落
            semantic_op=row["semantic_op"] if "semantic_op" in keys else None,
        )
        # 旧数据里的 slots 可能是早期格式。读回来和写进来走同一套规范化，
        # 否则"新数据能跑、旧数据崩"这种问题只会在升级后才暴露。
        self._normalize_slots(interp)
        return interp

    def open_clarifications(self, scope_id: str) -> list[dict[str, Any]]:
        self.expire_stale(scope_id)
        rows = self.engine._conn().execute(
            "SELECT * FROM clarifications WHERE scope_id=? AND " + self._OPEN_FILTER +
            " ORDER BY created_at",
            (scope_id, self.engine.now()),
        ).fetchall()
        return [dict(r) for r in rows]

    def expire_stale(self, scope_id: str | None = None) -> int:
        """把过期的 open 请求置为 expired。

        **过期 ≠ 丢弃。** 请求还在库里、detail 还能读，审计链完整；
        它只是不再等用户回答。`status='expired'` 让调用方可以统计
        「问了多少、多少没人理」——这个数本身就有价值。

        在每次查询前惰性执行，不靠定时任务：单人项目多一个后台进程
        就多一个会挂的东西。调用方也可以显式调它，配合可注入时钟做测试。
        """
        where = "WHERE status='open' AND expires_at IS NOT NULL AND expires_at <= ?"
        params: tuple = (self.engine.now(),)
        if scope_id is not None:
            where += " AND scope_id=?"
            params += (scope_id,)
        cur = self.engine._conn().execute(
            "UPDATE clarifications SET status='expired' " + where, params
        )
        return cur.rowcount

    def _record_observation(
        self,
        scope_id: str,
        trace_id: str,
        interp: Interpretation,
        decision: Decision,
        content: str,
        reason_override: str | None = None,
    ) -> None:
        """record_only / 被延后的澄清：记住，但不改状态、不当场追问。

        XM-10：必须有独立落点，否则"记住他说过"只做到一半。
        status 预留在 open/promoted 之间流转，提升逻辑见 promote_observation。

        `reason_override`：延后的澄清不写闸门原因（那会说 missing_slot），
        写 `deferred_clarification`——**原因要说清"为什么它在这张表里"**，
        否则统计的时候两类东西混在一起没法读。
        """
        obs_id = f"ob_{interp.interpretation_id}"
        reason = reason_override or ",".join(decision.reasons)
        self.engine._conn().execute(
            "INSERT OR REPLACE INTO observations(observation_id, scope_id, trace_id,"
            " interpretation_id, relation, modality, polarity, slots_json, excerpt,"
            " reason, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                obs_id, scope_id, trace_id, interp.interpretation_id,
                interp.relation, interp.modality, interp.polarity,
                canonical_json(interp.slots), content[:200],
                reason, "open", self.engine.now(),
            ),
        )

    def observations(
        self, scope_id: str, relation: str | None = None, status: str = "open"
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM observations WHERE scope_id=? AND status=?"
        params: list[Any] = [scope_id, status]
        if relation:
            sql += " AND relation=?"
            params.append(relation)
        sql += " ORDER BY created_at DESC"
        return [dict(r) for r in self.engine._conn().execute(sql, params).fetchall()]

    def promote_observation(
        self, observation_id: str, actor_id: str, note: str = "confirmed as fact"
    ) -> CommitResult | None:
        """把一条"只记录"升级为事件。

        XM-10 的预留路径：当用户后来确认"我说的是真的"，这条记录可以补提交。
        现在只做最小实现（用原 trace 作证据 + 一条确认痕迹），
        不做批量提升、不做冲突合并。
        """
        row = self.engine._conn().execute(
            "SELECT * FROM observations WHERE observation_id=? AND status='open'",
            (observation_id,),
        ).fetchone()
        if row is None:
            return None
        interp = self._load_interpretation(row["interpretation_id"])
        if interp is None:
            return None

        spec = self.engine.registry.get(interp.relation)
        confirm_trace = self.engine.record_trace(
            row["scope_id"], f"promote-{observation_id}", actor_id, "user_statement", note,
        )
        state_key, op, args = resolve(interp, spec, row["scope_id"])
        # 提升只处理增量类操作：correct / retract 指向的是一条既有事件，
        # 而"把一句只记录的话提升为事实"本身就不该改写别的记录。
        if op in ("correct", "retract"):
            raise ValueError(f"cannot promote a {op} observation: {observation_id}")

        effect_kind, payload = self._to_effect(op, args, spec)
        effect_kind, payload = self._first_set_rule(state_key, effect_kind, payload, op)
        event = self.engine.commit(Assertion(
            scope_id=row["scope_id"], state_key=state_key, relation=spec.relation,
            operation="append", effect_kind=effect_kind, payload=payload,
            idempotency_key=f"promote:{observation_id}",
            evidence_trace_ids=(interp.trace_id, confirm_trace),
            actor_id=actor_id,
        ))
        self.engine._conn().execute(
            "UPDATE observations SET status='promoted', promoted_event_id=? WHERE observation_id=?",
            (event.event_id, observation_id),
        )
        return event

    # ------------------------------------------------------------- 内部
    def _context_hint(self, scope_id: str, session: str | None = None) -> tuple[str | None, dict[str, Any]]:
        """返回当前会话最近事件的关系和状态，供省略句编码使用。

        有会话标识时只能读取同一会话的证据；无标识的旧调用沿用库级行为。
        """
        # events 表不存 relation——它由 state_key 推导（relation 是键的一段）。
        # 查键再解出来，比冗余一列更不容易不一致。
        conn = self.engine._conn()
        if session:
            row = conn.execute(
                "SELECT e.state_key FROM traces t"
                " JOIN event_evidence v ON v.trace_id=t.trace_id"
                " JOIN events e ON e.event_id=v.event_id"
                " WHERE t.scope_id=? AND t.erased_at IS NULL AND json_valid(t.context_ref)"
                " AND json_extract(t.context_ref,'$._xjm_session')=? AND e.scope_id=? AND e.void=0"
                " ORDER BY e.rowid DESC LIMIT 1", (scope_id, session, scope_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT state_key FROM events WHERE scope_id=? AND void=0 ORDER BY rowid DESC LIMIT 1",
                (scope_id,),
            ).fetchone()
        if row is None:
            return None, {}

        state_key = row["state_key"]
        relation = relation_of(state_key)
        try:
            view = self.engine.get_state(state_key)
        except Exception:                                   # noqa: BLE001
            return relation, {}
        # 只给 relation 层面的摘要，不给键的细节（键里有人名/物品名）。
        hint = {relation: view.value if view.status == "known" else view.status}
        return relation, hint

    @staticmethod
    def _normalize_slots(interp: Interpretation) -> None:
        """把 slots / signals 规整成 dict，入口处做**一次**。

        为什么集中做：模型会返回 `"slots": ["quantity"]` 这种形状，
        从库读回来的旧数据也可能不合规。之前在每一处用 slots 的地方
        各加一次 isinstance 判断，结果是漏一处就崩一处（踩过两次）。
        **入口规范化一次，后面所有代码都可以假定类型是对的。**
        """
        if not isinstance(interp.slots, dict):
            interp.signals = {**(interp.signals if isinstance(interp.signals, dict) else {}),
                              "bad_slots_type": type(interp.slots).__name__}
            interp.slots = {}
        elif not isinstance(interp.signals, dict):
            interp.signals = {}

    def _fill_identity_slots(
        self, interp: Interpretation, ctx: dict[str, Any], actor_id: str
    ) -> None:
        """补齐"谁欠谁"这类结构槽位，**只填空缺，不覆盖模型给的值**。

        边界（很重要）：
        - 只在有 peer_id 的**点对点**场景生效。群聊里"对方"不唯一，猜不得。
        - 只在槽位为空时填。模型判出「她欠我」这种第三人称时，那是它的判断，
          不该被这里的默认值盖掉。
        - 不补 quantity。**数量必须来自原话**，补一个默认值就是编造。
        """
        peer = ctx.get("peer_id")
        if not peer:
            return
        slots = interp.slots
        filled: list[str] = []

        def put(key: str, value: Any) -> None:
            if slots.get(key) in (None, ""):
                slots[key] = value
                filled.append(key)

        if interp.relation == "debt":
            put("debtor", actor_id)
            put("creditor", peer)
            if ctx.get("item"):
                put("item", ctx["item"])
        elif interp.relation == "nickname":
            put("subject", actor_id)

        # 记下"这些值不是模型抽出来的，是系统按路由补的"。
        # 不记的话，事后审计会把系统补的归属当成模型判对了——
        # 而这两件事的可信度完全不同。
        if filled:
            interp.signals = {**interp.signals, "filled_by_router": filled}

    @staticmethod
    def _normalize_direction(
        interp: Interpretation, ctx: dict[str, Any], actor_id: str, content: str
    ) -> None:
        """把"债主债务人反了"的方向掰回来（XM-28）。

        点对点对话里，默认方向是"我欠你"——说话人是债务人，这由路由决定，
        不由这句话决定（`_fill_identity_slots` 的注释写过）。

        模型偶尔会给出相反方向：「上周那二十枚没记错吧」被记成
        `kitty/debt/player`，于是鲸鲸欠我二十枚。值可能还是对的，
        **方向错了**，而错的那本账上看不出任何异常。

        规则：模型可以推翻默认，但必须拿出证据——
        「你欠我」「你借我」「你该还」这类结构。没有证据就掰回来。
        """
        if interp.relation != "debt" or not isinstance(interp.slots, dict):
            return
        peer = str(ctx.get("peer_id") or "")
        if not peer:
            return
        debtor = str(interp.slots.get("debtor") or "")
        creditor = str(interp.slots.get("creditor") or "")
        if debtor != peer or creditor != actor_id:
            return

        evidence = detect.reversal_cue(content)
        if evidence:
            interp.signals = {**interp.signals, "direction_evidence": evidence}
            return

        interp.slots["debtor"], interp.slots["creditor"] = actor_id, peer
        interp.signals = {**interp.signals, "direction_corrected": "no_evidence"}

    def _set_total_readings(
        self, interp: Interpretation, spec: RelationSpec, scope_id: str
    ) -> dict[str, Any] | None:
        """「我一共就欠你四十」到底是"现在还欠 40"还是"总共欠过 40"（XM-31）。

        引擎把 `set` 落成**余额**，所以默认读法是前者。但中文里「一共」指的是
        总额——说这句话的人多半在说：我原来只欠你 40，那还过的就该算数。

        两种读法差一个"已还总额"，而**两边都不会报错**。所以不猜，把两个数
        都摆出来让用户选：

            现在还欠 40          → set_total 40
            当初一共 40（已还 35）→ 改初始值 → 还剩 5

        只在"有过还款、且两种读法结果不同"时才问。没有还款时两者重合，
        多问一句就是打扰。
        """
        state_key, op, args = resolve(interp, spec, scope_id)
        if op != "set_total":
            return None

        gross = args.get("value", args.get("quantity"))
        if not isinstance(gross, (int, float)) or isinstance(gross, bool):
            return None

        state = self.engine.get_state(state_key)
        if state.value is None:
            return None

        repaid = 0.0
        for ev in self.engine.events_of(state_key, include_void=False):
            if ev["effect_kind"] != "delta":
                continue
            d = json.loads(ev["payload_json"]).get("delta")
            if isinstance(d, (int, float)) and d < 0:
                repaid += -d

        if repaid <= 0:
            return None
        remaining = gross - repaid
        if remaining < 0:
            # 总额比已还的还少——那不是"读法"问题，是数字本身对不上。
            # 交给守恒检查去判 disputed，不要伪装成一次选择。
            return None
        if remaining == gross:
            return None

        return {
            "state_key": state_key,
            "gross": gross,
            "repaid": repaid,
            "remaining": remaining,
            "current": state.value,
        }

    def _overwrite_scope(
        self, interp: Interpretation, spec: RelationSpec, scope_id: str
    ) -> dict[str, Any] | None:
        """这次 `set_total` 会不会抹掉已经发生过的增量（XM-20）。

        例：「我翻了下记录，上个月那笔是四十五枚」——如果判成 `set_total 45`，
        之前还过的 20+10 就被悄悄作废了，余额从 15 变成 45，而状态仍是 known。
        看起来完全正常，这是最难查的一类错。

        这里**不判断该不该抹**，只算出"会抹掉什么"，交给用户点头。
        值不变、状态不变，只是把静默变成看得见。
        """
        state_key, op, args = resolve(interp, spec, scope_id)
        if op != "set_total":
            return None

        new_value = args.get("value", args.get("quantity"))
        if not isinstance(new_value, (int, float)):
            return None

        state = self.engine.get_state(state_key)
        if state.value is None or new_value == state.value:
            return None

        deltas = [
            json.loads(e["payload_json"]).get("delta", 0)
            for e in self.engine.events_of(state_key, include_void=False)
            if e["effect_kind"] == "delta"
        ]
        down = [d for d in deltas if isinstance(d, (int, float)) and d < 0]
        if not down:
            return None

        return {
            "state_key": state_key,
            "erased_count": len(down),
            "erased_total": abs(sum(down)),
            "current_value": state.value,
            "new_value": new_value,
        }

    def _looks_like_restatement(
        self, content: str, state_key: str, op: str, quantity: Any
    ) -> bool:
        """这句话是不是在指一笔**已经记过的**账（XM-27）。

        光看字面不够：「也算进去」既可能指旧账，也可能是第一次说。
        所以必须同时满足两个条件：

            字面有复述线索（也算 / 算上 / 已经还 / 刚才那）
            账上确实有同向同额的事件

        少一个条件都不降级。**宁可多记一笔，不要把一笔真账挡在门外。**
        """
        if not detect.restatement_cue(content):
            return False
        if op in ("correct", "retract", "promise"):
            return False
        qty = quantity
        if not isinstance(qty, (int, float)) or isinstance(qty, bool):
            return False

        events = self.engine.events_of(state_key, include_void=False)[-20:]
        for ev in reversed(events):
            payload = ev["payload_json"]
            try:
                data = json.loads(payload)
            except Exception:                       # noqa: BLE001
                continue
            # 只比**数额大小**，不比方向、也不比语义操作。
            #
            # 理由是这里的问题不是"这句话是什么操作"，而是"这个数账上有没有"。
            # 实测「刚才那两袋也算进去」被规则编码器判成 incur（句子里没有"还"字），
            # 要求 op 对得上就永远匹配不到。而"再还你两袋"不含复述线索，
            # 根本走不到这里——所以放宽匹配不会误伤新账。
            if ev["effect_kind"] == "delta":
                d = data.get("delta")
                if isinstance(d, (int, float)) and abs(d) == abs(qty):
                    return True
            elif ev["effect_kind"] == "set":
                if data.get("value") == qty:
                    return True
        return False

    def _looks_like_echo(
        self, content: str, state_key: str, op: str, quantity: Any
    ) -> bool:
        """这句话是不是在**复述账面已有的那个数**。

        与 `_looks_like_restatement` 的分工：

            restatement  靠字面线索（「也算」「刚才那」「已经还」）
            echo         靠**数值对得上**——他把刚听到的数重复了一遍

        「那我欠你三斤。」在账上正好三斤时，不是"又欠三斤"，是把三斤说了一遍。
        c06 第 7 句一直错在这里：余额 3 → 6，后面四句全崩。

        为什么这条判据值得信：口语里复述一个数**不会**再带增量词
        （又/再/也/另/补），也不会用长句。「又欠三斤」和「那我欠你三斤」
        差的就是那个"又"。反过来，只要账户余额和他说的数不一样，
        这句话一定在说别的——**账上有没有这个数，是客观事实，不是猜**。

        trade-off（写在这儿，因为它是真风险）：
            账上恰好 40，他说「我欠你四十」，本意是"再加 40"（共 80）。
            这种句子在中文里几乎总要带「又」「再」或先说一句「再加」，
            所以限制增量词能挡住绝大部分。剩下那部分仍会漏——
            但漏记一条"余额不变"的账，比把每一次复述都记成新欠款好。

        **repay 一律不看**：「还你五袋」在余额正好五袋时，是**还清**，
        不是复述。c01 第 9 句就是这么被误伤成 record_only 的（余额停在 5）。
        还款额等于余额是最常见的结清方式，不能挡。
        """
        if op not in ("incur", "set_total"):
            return False
        if not isinstance(quantity, (int, float)) or isinstance(quantity, bool):
            return False
        if quantity <= 0:
            return False
        try:
            st = self.engine.get_state(state_key)
        except Exception:                           # noqa: BLE001
            return False
        current = getattr(st, "value", None)
        if not isinstance(current, (int, float)) or isinstance(current, bool):
            return False
        if current <= 0 or current != quantity:
            return False
        if detect.increment_cue(content):
            return False
        # 复述有**语气**：要么很短（「三捆。」），要么以应答词起头（「嗯」「那就」）。
        #
        # 只靠"数值对得上"是不够的：并发测试里 30 条「我欠你 1 碗」，
        # 第一条之后余额永远是 1，后面 29 条全被判成复述——
        # 那不是复述，是同一个人反复记账。**语气是字面证据，余额不是。**
        stripped = "".join(
            ch for ch in content if ch not in " \t\u3000，。！？、,.!?~…—-"
        )
        if len(stripped) > 10:
            return False
        return len(stripped) <= 4 or stripped.startswith(_ECHO_OPENERS)

    def _apply_surface_cues(self, interp: Interpretation, content: str) -> None:
        """在进闸门前，用**字面上能看见的线索**校正一次语义操作。

        为什么必须在这一层、而不是 prompt 里：
        「下个月」「是不是」不需要理解，只需要看见。
        把它交给概率模型，等于把一个确定性的判断换成抽签——
        实测同一个 prompt、temperature=0，两次运行结果不同（XM-13）。

        这里只做两件事，且都**只会让系统更保守**：
            有未来时间词 + 欠/还   → promise（不改余额）
            有求确认句式           → seeking_agreement（必须问一句）
        """
        if not isinstance(interp.slots, dict):
            interp.slots = {}

        # 「掌柜的说你欠我五十克」——说话人在**转述**别人的话（XM-40）。
        #
        # 句子里那个「你欠我」的方向属于**被引述的人**，不是玩家的账。
        # 漏判的后果是余额 200 → 250，而且**账面看不出异常**（状态 known、值合理）。
        #
        # 处理只有一步：把模态收敛到 `quoted`。剩下的交给闸门——
        # `modality_not_allowed:quoted` → record_only。
        # 引述**永不进玩家自己的账，也永不进承诺表**。
        #
        # 为什么必须改模态而不是只打个信号：闸门 (3g)/(3h) 只看
        # `third_party`，而「掌柜的说你欠我五十克」句中有"我"，
        # `third_person_cue` 明确不认这种句子（说话人就是当事人）。
        # 只有 `quoted` 这条既有机制能把整类引述一次性挡住。
        quote = detect.reported_quote_cue(content)
        if quote:
            interp.signals = {**interp.signals, "reported_quote": quote}
            interp.modality = "quoted"

        due = detect.future_cue(content)
        if due:
            interp.signals = {**interp.signals, "future_tense": due}
            interp.slots.setdefault("due_hint", due)
            op = interp.semantic_op or str(interp.slots.get("intent") or "")
            # 只升 repay，不升 incur。
            #
            # `incur` 说的是"这笔债此刻成立"，未来词修饰的是**还款计划**，
            # 不是"债成不成立"。「我欠你两袋米，回头还你」里的
            # 「回头还」是下一件事，不该让它把已经成立的账抹掉——
            # 整句升成 promise 之后，这两袋一条都记不上（实测踩到，
            # 端到端对话里关键是实全被这条规则挡在门外）。
            #
            # `repay` 相反：「下个月还你三袋」确实还没还，该升。
            if op == "repay":
                interp.semantic_op = "promise"
            # 编码器可能**已经**判成 promise（prompt 里有这一格），
            # 那条路径也要补施事和日期——之前只在"提升"分支里补，
            # 结果编码器判对的时候反而没有 due_at（测试当场抓到）。
            if interp.semantic_op == "promise":
                # 这个未来属于谁？「他明天来取」属于他，不属于我。
                # 仍然按 promise 记（否则会当成"已经还了"，那更糟），
                # 但记上施事，后面就不给它开承诺——不然到期会催别人的事。
                who = detect.third_party_near(content, due)
                if who:
                    interp.signals = {**interp.signals, "third_party": who}
                # 相对时间落成绝对时间。算不出来（"回头""发工资"）就不写——
                # 猜一个日期去提醒，比不提醒更伤信任（XM-24）。
                at = due_mod.resolve_due(content, self.engine.now())
                if at:
                    interp.slots["due_at"] = at

        conf = detect.confirmation_cue(content)
        if conf:
            interp.signals = {**interp.signals, "seeking_agreement": conf}

        # 「替老张赊的」——施事不是说话人，归属可疑（G0 矩阵的归属格）。
        # 它不影响"记多少"，影响"记在谁头上"，所以只打信号、交给闸门去问。
        proxy = detect.proxy_cue(content)
        if proxy and not interp.signals.get("third_party"):
            interp.signals = {**interp.signals, "third_party": proxy}

        # 「另外老张那边我还欠五本，跟你这儿没关系」——整句话在说**另一本账**。
        # 数量标的都齐，唯一的问题是记不在这里。这条漏了就是把别人的债记到我头上。
        other = detect.other_account_cue(content)
        if other and not interp.signals.get("third_party"):
            interp.signals = {**interp.signals, "third_party": other}

        # 「老李那坛他自己还。」——整句主语是"他"，不是我的账。
        who3 = detect.third_person_cue(content)
        if who3 and not interp.signals.get("third_party"):
            interp.signals = {**interp.signals, "third_party": who3}

        # 量词是**字面的**，不该指望模型抽。
        #
        # 实测真模型在「我欠你两石米」上不给 unit，「再欠你三斗」也不给，
        # 于是两笔账都成了"无单位"，2+3 直接等于 5——单位冲突根本无从发现。
        # 而"石"就在句子里，看一眼就知道，没有理由交给概率模型。
        #
        # 只补空缺，不覆盖模型给的值：模型说「一碗」而正则抽到「碗」时一致；
        # 模型说 unit=斤、正则抽到"碗"时以模型为准（它读了整句）。
        if not interp.slots.get("unit"):
            m = re.search(
                rf"(?:\d+|[一二两三四五六七八九十]+)\s*([{detect.QUANTIFIERS}])", content
            )
            if m:
                interp.slots["unit"] = m.group(1)

        # 「还差一百二对吧？」在确认余额，不是还款（XM-38）。
        if detect.balance_check_cue(content):
            interp.signals = {**interp.signals,
                              "balance_check": detect.balance_check_cue(content)}

        # 「上个月那两袋也没算错吧？」在复核一笔旧账，不是又欠两袋。
        # 它没有余额词，上面那条接不住；漏掉就会当成新增（c10 第 4 句）。
        if detect.recall_check_cue(content):
            interp.signals = {**interp.signals,
                              "recall_check": detect.recall_check_cue(content)}

        # 「还剩几捆？」——它在问账，不是在报账。
        # 和上面那条的区别只有一条：**有没有给出数字**。
        if detect.balance_query(content):
            interp.signals = {**interp.signals,
                              "balance_query": detect.balance_query(content)}

        # 「那两本是画册」——带数量，却没有交易动词（XM-26）。
        # 它在说"这两本是什么"，不是"又欠两本"。判成 incuir 会让账上凭空多两本，
        # 而这句话在真实对话里出现得极频繁（说明品质、来源、种类）。
        if detect.describes_only(content):
            interp.signals = {**interp.signals, "describe_only": True}

        # 「对了你吃了吗」「说定了」「那捆有点潮」——整句没有一个能进账本的成分。
        # 这类句子最容易被编码器的关系兜底值（preference）接住，
        # 然后因为缺 object 而被要求"补一句你喜欢什么"。
        if detect.no_claim(content):
            interp.signals = {**interp.signals, "no_claim": True}

        # 「还清了」是报告状态，不是又还一笔（XM-34）。
        # 只打标记；怎么处理交给闸门和后置检查。
        settled = detect.settled_cue(content)
        if settled:
            interp.signals = {**interp.signals, "settled_report": settled}

        # 改的是哪个数：「刚才那三尺是两尺」里的**三**（XM-35）。
        # 数字就在句子里，此前只用来算"改成多少"，没人用它指认对象。
        if interp.semantic_op == "correct" or interp.slots.get("intent") == "correct":
            if "old_value" not in interp.slots:
                ov = detect.old_value_cue(content, interp.slots.get("quantity"))
                if ov is not None:
                    interp.slots["old_value"] = ov
                    interp.signals = {**interp.signals, "located_by_amount": ov}

        # 改哪一笔：只在 correct 上补，不替模型改语义操作。
        #
        # 「我一开始跟你说的是五十」——模型判 correct 是对的，但经常不给
        # target_hint，内核就退到"最近一条"，于是改的是最近那笔还款，
        # 余额变成 40 而不是 20。这类错在账面上同样看不出异常（XM-30）。
        if (interp.semantic_op == "correct" or interp.slots.get("intent") == "correct"):
            if not interp.slots.get("target_hint"):
                hint = detect.target_cue(content)
                if hint:
                    interp.slots["target_hint"] = hint
                    interp.signals = {**interp.signals, "target_hint_from_cue": hint}

    def _first_set_rule(
        self, state_key: str, effect_kind: str, payload: dict[str, Any],
        op: str = "incur",
    ) -> tuple[str, dict[str, Any]]:
        """「首次 set，后续 delta(+)」——只对 incur 生效。

        「我欠你 40 碗」在已经有欠款时，业务上通常是「又欠 40」，不是「总额改成 40」。
        但 `set_total` 是**显式**要改总额（「凑个整」「就是三十碗」），必须原样保留 set。
        把两者混在一句规则里，就会出现「说了凑整却变成又欠 30」。
        """
        if op != "incur" or effect_kind != "set":
            return effect_kind, payload
        # 「账上有没有数」看的是**带值的事件**，不是事件总数。
        # 一条承诺（effect_kind="none"）也在事件流里，但它没建基数——
        # 拿它当"已经有账"，第一笔欠款就会变成 delta，从 0 开始加。
        if not any(
            e["effect_kind"] in ("set", "delta")
            for e in self.engine.events_of(state_key, include_void=False)
        ):
            return effect_kind, payload
        # 转成 delta 时**保留其余字段**，别只留 delta。
        #
        # 这里曾经写成 `{"delta": payload["value"]}` —— unit 就在这一步被丢掉，
        # 于是「再欠你三斗」的事件里没有 unit，折叠时看不出和「两石」不同，
        # 2+3 直接等于 5。这类"重建对象时少抄一个字段"的错，
        # 和 encoder 那边"改 prompt 格式丢了字段、主对话掉到 0/12"是同一个形状。
        rest = {k: v for k, v in payload.items() if k != "value"}
        return "delta", {**rest, "delta": payload["value"]}

    def _latest_live_event(
        self, state_key: str, effect_sign: int | None = None, oldest: bool = False
    ) -> dict[str, Any] | None:
        """最近一条仍有效的事件，可按方向过滤。

        `correct` / `retract` 要指向一条具体事件，而编码器看不到 event_id。
        两种做法：
          a) 让模型猜一个 id —— 它不知道，会瞎编
          b) 内核按"最近一条"定位 —— 确定性，可解释

        选 b，但允许模型给一个方向提示（target_hint），
        因为「不是五，是七」到底在改还款还是改总额，只有它知道。

        `oldest=True` 取**最早**一条而不是最近一条。用于「我一开始就欠 30 碗」
        这类改初始值的说法——它指的是第一笔，不是最后一笔。
        """
        rows = [
            r for r in self.engine.events_of(state_key, include_void=False)
            if r["effect_kind"] != "invalidate"
        ]
        if effect_sign is not None:
            def sign_of(row: dict[str, Any]) -> int:
                payload = json.loads(row["payload_json"])
                if row["effect_kind"] == "delta":
                    return -1 if payload.get("delta", 0) < 0 else 1
                return 1

            rows = [r for r in rows if sign_of(r) == effect_sign]
        if not rows:
            return None
        return rows[0] if oldest else rows[-1]

    def _persist_encoding(self, trace_id: str, interps: list[Interpretation]) -> None:
        """编码产物必须落库（6.3）：模型版本号不能替代编码结果本身。"""
        conn = self.engine._conn()
        encoding_id = "enc_" + trace_id[-16:]
        encoder_version = type(self.encoder).__name__
        conn.execute(
            "INSERT OR REPLACE INTO encodings(encoding_id, trace_id, encoder_version,"
            " prompt_version, schema_version, payload_json, generated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                encoding_id, trace_id, encoder_version, "n/a", "v1",
                canonical_json([i.to_dict() for i in interps]), self.engine.now(),
            ),
        )
        for interp in interps:
            conn.execute(
                "INSERT OR REPLACE INTO interpretations(interpretation_id, encoding_id,"
                " trace_id, relation, modality, polarity, slots_json, signals_json,"
                " semantic_op, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    interp.interpretation_id, encoding_id, trace_id,
                    interp.relation, interp.modality, interp.polarity,
                    canonical_json(interp.slots), canonical_json(interp.signals),
                    interp.semantic_op, self.engine.now(),
                ),
            )

    def _commit(
        self,
        interp: Interpretation,
        spec: RelationSpec,
        scope_id: str,
        actor_id: str,
        trace_id: str,
        ctx: dict[str, Any],
        extra_evidence: tuple[str, ...] = (),
    ) -> CommitResult:
        state_key, op, args = resolve(interp, spec, scope_id)

        if op == "promise":
            return self._commit_promise(
                interp, spec, state_key, args, scope_id, actor_id, trace_id, ctx,
                extra_evidence=extra_evidence,
            )

        if op in ("correct", "retract"):
            return self._apply_revision(
                op, state_key, args, spec, scope_id, actor_id, trace_id, ctx,
                extra_evidence=extra_evidence,
            )

        effect_kind, payload = self._to_effect(op, args, spec)

        # 「首次 set、后续 delta」的判断和提交必须锚在**同一个版本**上。
        #
        # 早先是纯 TOCTOU：先 events_of() 看一眼，再开新事务写。两个线程
        # 可以同时看到空账，各自决定 set，互相覆盖——余额停在 40 而用户说了
        # 两次。它不报错、不冲突，只是少记了一笔。
        #
        # 现在带上 expected_version：读到的版本被推进了就重来，重来时
        # events_of 已经非空，规则自然给出 delta。
        for attempt in range(_COMMIT_RETRIES):
            version = self.engine.get_state(state_key).version
            ek, pl = self._first_set_rule(state_key, effect_kind, payload, op)
            try:
                return self.engine.commit(Assertion(
                    scope_id=scope_id, state_key=state_key, relation=spec.relation,
                    operation="append", effect_kind=ek, payload=pl,
                    idempotency_key=f"{trace_id}:{interp.relation}",
                    evidence_trace_ids=(trace_id, *extra_evidence), actor_id=actor_id,
                    effective_at=ctx.get("effective_at"),
                    gate_decision_id=f"gd_{interp.interpretation_id}",
                    expected_version=version,
                    # 把句式写进事件：将来这条被纠正时，才知道该关哪个格子。
                    pattern=str(interp.signals.get("pattern") or ""),
                ))
            except VersionConflict:
                if attempt == _COMMIT_RETRIES - 1:
                    raise
                continue

        raise AssertionError("unreachable")            # pragma: no cover

    def _commit_promise(
        self,
        interp: Interpretation,
        spec: RelationSpec,
        state_key: str,
        args: dict[str, Any],
        scope_id: str,
        actor_id: str,
        trace_id: str,
        ctx: dict[str, Any],
        extra_evidence: tuple[str, ...] = (),
    ) -> CommitResult:
        """承诺进事件流，但**不改变余额**（effect_kind="none"）。

        「下个月我还你两袋」之后，余额还是那么多——那两袋还在债主手里。
        但这句话不能丢：它是**一笔可以被追讨的账**，账单上现在写着
        「他答应下个月还两袋」。到期没还，系统能拿出来问。

        不用 delta 模拟：delta 会让余额立刻少两袋，等于替对方提前收货。
        也不用 observations：observations 是"他说过这话"的旁注，
        没有数量、没有生命周期、不会到期——那不是承诺，那是日记。
        """
        qty = args.get("quantity")
        if not isinstance(qty, (int, float)) or isinstance(qty, bool):
            raise ValueError(f"promise 需要数字数量，拿到 {qty!r}")

        payload: dict[str, Any] = {
            "kind": "promise",
            "quantity": abs(qty),
            "due_hint": args.get("due_hint") or (interp.slots or {}).get("due_hint"),
            # 单位只用于展示（「2 碗」），不参与计算，也不进 state_key（XM-12）。
            "unit": (interp.slots or {}).get("unit"),
        }
        due_at = (
            args.get("due_at")
            or (interp.slots or {}).get("due_at")
            or ctx.get("due_at")
        )
        if due_at:
            payload["due_at"] = due_at

        return self.engine.commit(Assertion(
            scope_id=scope_id, state_key=state_key, relation=spec.relation,
            operation="append", effect_kind="none", payload=payload,
            idempotency_key=f"{trace_id}:promise",
            evidence_trace_ids=(trace_id, *extra_evidence), actor_id=actor_id,
            effective_at=ctx.get("effective_at"),
            gate_decision_id=f"gd_{interp.interpretation_id}",
            pattern=str(interp.signals.get("pattern") or ""),
        ))

    def _to_effect(
        self, op: str, args: dict[str, Any], spec: RelationSpec
    ) -> tuple[str, dict[str, Any]]:
        """语义操作 → (effect_kind, payload)。

        `unit` 一并写进 payload：它是**这笔账的口径**，不是显示细节。
        不写进去，折叠时就无法发现「两石」和「三斗」被直接相加——
        2+3−5=0，状态 known，账面看不出任何异常（单位换算那个坑）。
        """
        unit = args.get("unit")
        tag = {"unit": unit} if unit else {}

        if op == "set_total" or spec.conservation != "repayment":
            return "set", {"value": args.get("value", args.get("quantity")), **tag}

        qty = args.get("quantity")
        if not isinstance(qty, (int, float)) or isinstance(qty, bool):
            # 数量不合法时不猜。给一个非数字 payload 让内核判 disputed，
            # 比在这里静默按 0 处理安全——0 会悄悄把余额抹平。
            return "delta", {"delta": qty, **tag}

        if op == "incur":
            return "set", {"value": qty, **tag}   # 首次 set，_first_set_rule 再决定
        # repay / waive 都是减少
        return "delta", {"delta": -abs(qty), **tag}

    def _apply_revision(
        self,
        op: str,
        state_key: str,
        args: dict[str, Any],
        spec: RelationSpec,
        scope_id: str,
        actor_id: str,
        trace_id: str,
        ctx: dict[str, Any],
        extra_evidence: tuple[str, ...] = (),
    ) -> CommitResult:
        """correct / retract：改写的是一条**已存在的事件**，不是新增。

        这是本次改造的核心：之前「不是五，是七」会被当成一次新的 +7，
        余额从 35 变成 42，而正确答案是 33。
        """
        hint = str(args.get("target_hint") or "last")
        # 「一开始就欠 X」改的是**第一笔**，不是最近一笔。
        # 少了这个方向，模型只能用 set_total 覆盖总额——
        # 那会把期间的还款记录一起抹掉（flipflop 段 1/11 的根因）。
        # 先用**金额**定位（XM-35）：句子里那个"被改掉的数"是账上唯一可比的键。
        # 「刚才那三尺是两尺」——hint 只说"最近"，而最近可能是一条快照；
        # 但"三尺"这三个字直接指向金额为 3 的那笔。
        target = None
        old_value = args.get("old_value")
        if op == "correct" and isinstance(old_value, (int, float)) and not isinstance(old_value, bool):
            target = self._locate_by_amount(state_key, float(old_value))

        if target is None:
            sign = {"last_repay": -1, "last_payment": -1,
                    "last_incur": 1, "first_incur": 1}.get(hint)
            target = self._latest_live_event(state_key, sign, oldest=(hint == "first_incur"))
        if target is None:
            raise TargetNotResolvable(
                f"{op} 找不到可指向的事件：{state_key} hint={hint}", hint=hint
            )

        evidence = (trace_id, *extra_evidence)

        # 要改的这条是**自动提交**的 → 这个句式被证明不可靠，记一次失败。
        #
        # 为什么"被纠正"是最可靠的失败证据：提交成功不等于对，
        # 但用户专门回来说"不是这个"，那一定是错了。所以熔断只认这一种信号。
        self._note_auto_corrected(target, spec, scope_id, trace_id)

        if op == "retract":
            state = self.engine.retract_event(
                target_event_id=target["event_id"],
                idempotency_key=f"{trace_id}:retract",
                evidence_trace_ids=evidence,
                actor_id=actor_id,
            )
            # retract_event 只返回 StateView，这里补出上游需要的形状
            tomb = self._latest_tombstone(state_key)
            return CommitResult(
                event_id=tomb["event_id"] if tomb else target["event_id"],
                state_key=state_key,
                sequence_no=tomb["sequence_no"] if tomb else 0,
                applied=True,
                state=state,
            )

        qty = args.get("quantity", args.get("value"))
        if target["effect_kind"] == "none":
            # 改的是一条承诺（「不是两袋，是三袋」）。
            # 它不能变成一次 delta——那会把"答应过"改成"已经还了"。
            new_payload = {
                "kind": "promise", "quantity": abs(qty),
                "due_hint": args.get("due_hint")
                or json.loads(target["payload_json"]).get("due_hint"),
            }
        elif target["effect_kind"] == "delta":
            old = json.loads(target["payload_json"]).get("delta", 0)
            new_payload = {"delta": (-1 if old < 0 else 1) * abs(qty)}
        else:
            new_payload = {"value": qty}

        result = self.engine.correct(
            target_event_id=target["event_id"],
            new_payload=new_payload,
            idempotency_key=f"{trace_id}:correct",
            evidence_trace_ids=evidence,
            actor_id=actor_id,
            effective_at=ctx.get("effective_at"),
        )

        # 改了初始值，就得把**建立在旧初始值上的结论**一起撤掉（XM-33）。
        #
        # 账上有两条并存的记账方式：
        #     初始值 + 增量   10 → -2 → -3 = 5
        #     余额快照        「一共就欠你 8」= 8（一条 set 事件）
        #
        # 快照的语义是"前面的不用算了，就是这么多"。所以它一旦存在，
        # 改初始值对余额完全没有影响——折叠时快照排在最后，把前面全盖住。
        # 用户说「不对，一开始说的是十二尺」，系统一声不响什么都不做。
        #
        # 修法：改初始值时，把之后**用户说的**快照一并作废。
        # 理由是"改基础事实"这个动作本身就意味着"基于旧事实的结论作废了"，
        # 不需要用户再说一遍「那条也不算」。
        #
        # 为什么只作废 target_ref IS NULL 的：correct 自己会新增一条
        # effect_kind='set' 的替代事件（target_ref 指向原事件），那是新的事实，
        # 不能误伤。
        cascaded, after = self._void_stale_snapshots(
            state_key, target, hint, trace_id, actor_id
        )
        if cascaded:
            result.cascaded = cascaded
            if after is not None:
                result.state = after
        return result

    def _void_stale_snapshots(
        self,
        state_key: str,
        target: dict[str, Any],
        hint: str,
        trace_id: str,
        actor_id: str,
    ) -> tuple[int, Any]:
        """作废"改初始值"之后残留的余额快照。返回 (作废条数, 最后的状态)。"""
        # 只在改**最早那笔**、且它是一条 set 时做。
        # 改一条 delta（「刚才那十枚是十五枚」）不影响初始值，快照仍然成立。
        if hint != "first_incur" or target["effect_kind"] != "set":
            return 0, None

        rows = self.engine._conn().execute(
            "SELECT event_id FROM events WHERE state_key=? AND effect_kind='set'"
            " AND void=0 AND sequence_no > ? AND target_ref IS NULL"
            " ORDER BY sequence_no",
            (state_key, target["sequence_no"]),
        ).fetchall()

        count = 0
        state = None
        for i, row in enumerate(rows):
            try:
                state = self.engine.retract_event(
                    target_event_id=row["event_id"],
                    idempotency_key=f"{trace_id}:stale_snapshot:{i}",
                    evidence_trace_ids=(trace_id,),
                    actor_id=actor_id,
                )
                count += 1
            except EventAlreadyVoid:
                continue          # 并发下已被别处撤掉，不算错
        return count, state

    def _balance_said_value(
        self, interp: Interpretation, spec: RelationSpec, scope_id: str
    ) -> dict[str, Any] | None:
        """他说"还差 X"，X 是多少、账上是多少。None 表示读不出来。"""
        try:
            state_key, _, args = resolve(interp, spec, scope_id)
        except Exception:                       # noqa: BLE001
            return None
        said = args.get("quantity")
        if not isinstance(said, (int, float)) or isinstance(said, bool):
            return None
        state = self.engine.get_state(state_key)
        if state.value is None:
            return None
        return {
            "said": said,
            "actual": state.value,
            "matches": said == state.value,
            "unit": (interp.slots or {}).get("unit"),
        }

    def _answer_balance(
        self, interp: Interpretation, spec: RelationSpec, scope_id: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """「还剩几捆？」→ 查一下，把数报出去。

        三种答案，必须分开（I5）：

            known      有值，报数
            unknown    记过，但依据被撤了 —— 不是 0，是"现在说不准"
            not_found  从来没记过 —— 不是"没有欠款"，是"你没说过"

        后两种合并成一句「没有」，就是把"系统没记住"和"确实没有"
        说成同一件事，而这套系统的全部价值就在于分得清这两者。
        """
        try:
            state_key, _, _ = resolve(interp, spec, scope_id)
        except Exception:                       # noqa: BLE001
            return None
        slots = interp.slots or {}
        detail: dict[str, Any] = {
            "kind": "balance",
            "relation": spec.relation,
            "state_key": state_key,
            # 单位优先取这句话里说的，其次取对话上下文给的。
            # 「还剩几捆？」里"几捆"没有数字，编码器抽不出 unit——
            # 但对话一开始就说了是柴火（捆），那个单位一直有效。
            "unit": slots.get("unit") or (context or {}).get("unit_hint"),
            "item": slots.get("item"),
            "creditor": slots.get("creditor"),
        }

        state = self.engine.get_state(state_key)
        # disputed 要单独说：这时候**有值，但不能信**。
        # 说成 known 是撒谎，说成 unknown 是抹掉"我记过"这个事实。
        if state.status == "disputed":
            return {
                **detail, "status": "disputed", "value": state.value,
                "reason": state.conflict_reason,
            }
        if state.value is None:
            had = bool(self.engine.events_of(state_key))
            return {**detail, "status": "unknown" if had else "not_found"}
        return {**detail, "status": "known", "value": state.value}

    def _settled_gap(
        self, interp: Interpretation, spec: RelationSpec, scope_id: str
    ) -> dict[str, Any] | None:
        """他说"还清了"，账上还差多少。None 表示账上根本没数。

        `{}` 表示确实清了；带 `outstanding` 的字典表示还差着。
        """
        try:
            state_key, _, _ = resolve(interp, spec, scope_id)
        except Exception:                       # noqa: BLE001
            return None
        state = self.engine.get_state(state_key)
        if state.value is None:
            return None
        if state.value == 0:
            return {}
        return {
            "state_key": state_key,
            "outstanding": state.value,
            "unit": (interp.slots or {}).get("unit"),
            "creditor": (interp.slots or {}).get("creditor"),
        }

    def _note_auto_corrected(
        self, target: dict[str, Any], spec: RelationSpec, scope_id: str, trace_id: str
    ) -> None:
        """这条事件是 auto 提交的吗？是的话，给它的句式记一次失败。"""
        pattern = target.get("pattern")
        decision_id = target.get("gate_decision_id")
        if not pattern or not decision_id:
            return
        row = self.engine._conn().execute(
            "SELECT admission_mode FROM gate_decisions WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        if row and row["admission_mode"] == "auto":
            self.admission.record_failure(
                scope_id, spec.relation, str(pattern), f"被修正：{trace_id}"
            )

    def _locate_by_amount(
        self, state_key: str, amount: float
    ) -> dict[str, Any] | None:
        """按金额找那一条事件。**找不到、或找到多条，都返回 None。**

        不唯一时宁可退回"最近一条"（旧的、可解释的行为），也不猜——
        猜错会改到另一笔账上，而账面上看不出任何异常。

        先找 delta（还款/免除），再找 set（初始值）。顺序有意义：
        「刚才那三尺」说的是还款，账上通常也正好有一条 -3。
        """
        events = self.engine.events_of(state_key, include_void=False)

        def _payload(e):
            try:
                return json.loads(e["payload_json"] or "{}")
            except Exception:                       # noqa: BLE001
                return {}

        deltas = [
            e for e in events
            if e["effect_kind"] == "delta"
            and abs(float(_payload(e).get("delta", 0) or 0)) == amount
        ]
        if len(deltas) == 1:
            return deltas[0]

        sets = [
            e for e in events
            if e["effect_kind"] == "set"
            and float(_payload(e).get("value", -1) or -1) == amount
        ]
        if len(sets) == 1:
            return sets[0]
        return None

    def _latest_tombstone(self, state_key: str) -> dict[str, Any] | None:
        rows = self.engine._conn().execute(
            "SELECT event_id, sequence_no FROM events WHERE state_key=?"
            " AND effect_kind='invalidate' ORDER BY sequence_no DESC LIMIT 1",
            (state_key,),
        ).fetchall()
        return dict(rows[0]) if rows else None

    def _record_decision(
        self, interp: Interpretation, decision: Decision, spec: RelationSpec
    ) -> None:
        conn = self.engine._conn()
        conn.execute(
            "INSERT OR REPLACE INTO gate_decisions(decision_id, interpretation_id,"
            " spec_version, outcome, reasons_json, admission_mode, decided_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                f"gd_{interp.interpretation_id}", interp.interpretation_id,
                spec.spec_version, decision.outcome,
                canonical_json(decision.reasons),
                # 记**实际用的**准入等级，不是规格里那个默认值。
                # 熔断要靠这个字段回查"这条事件当初是不是自动提交的"，
                # 写成 spec 的默认值会让熔断永远不触发（踩过）。
                str((interp.signals or {}).get("admission_level")
                    or spec.admission_mode),
                self.engine.now(),
            ),
        )

    def _open_clarifications(
        self,
        scope_id: str,
        outcomes: list[IngestOutcome],
        trace_id: str,
        content: str = "",
    ) -> list[dict[str, Any]]:
        """为一轮里需要澄清的解释开请求。同一句话的多个候选合并成一条。

        歧义必须合并：模型把「我好像还欠你四十碗」同时判成 debt 和 nickname 时，
        逐条开请求等于把同一个问题问两遍（XM-03 的实际后果）。
        合并之后，用户看到的是「你指的是哪件事」，一次选完。

        其他类型（缺槽位、待确认）保持逐条——它们指向的是不同的补全动作，
        合并没有意义。
        """
        pending = [
            (o.interpretation, o.decision)
            for o in outcomes if o.decision.outcome == PENDING
        ]
        if not pending:
            return []

        conn = self.engine._conn()
        opened: list[dict[str, Any]] = []

        def insert(request_id: str, ids: list[str], detail: dict[str, Any]) -> None:
            now = self.engine.now()
            expires = _plus_seconds(now, self.clarification_ttl) \
                if self.clarification_ttl > 0 else None
            conn.execute(
                "INSERT OR REPLACE INTO clarifications(request_id, scope_id,"
                " interpretation_ids_json, kind, detail_json, status, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    request_id, scope_id, canonical_json(ids),
                    detail.get("kind", "ambiguous"), canonical_json(detail),
                    "open", now, expires,
                ),
            )
            opened.append({"request_id": request_id, "expires_at": expires, **detail})

        ambiguous = [
            (i, d) for i, d in pending
            if (d.clarification or {}).get("kind") == "ambiguous"
        ]
        # 「一共欠 40」的两种读法也要合并成一条：它们问的是同一件事，
        # 分成两条就是让用户回答两次同一个问题。
        readings = [
            (i, d) for i, d in pending
            if (d.clarification or {}).get("kind") == "total_reading"
        ]
        others = [
            (i, d) for i, d in pending
            if (i, d) not in ambiguous and (i, d) not in readings
        ]

        if ambiguous:
            ids = [i.interpretation_id for i, _ in ambiguous]
            request_id = f"cl_amb_{trace_id[-8:]}"
            detail = {
                "kind": "ambiguous_candidates",
                "candidates": [
                    {
                        "interpretation_id": i.interpretation_id,
                        "relation": i.relation,
                        "semantic_op": i.semantic_op,
                        "modality": i.modality,
                        "slots": i.slots,
                    }
                    for i, _ in ambiguous
                ],
            }
            insert(request_id, ids, detail)

        if readings:
            ids = [i.interpretation_id for i, _ in readings]
            request_id = f"cl_tot_{trace_id[-8:]}"
            detail = dict((readings[0][1].clarification or {}))
            insert(request_id, ids, detail)

        # 限流：超出上限的**不丢**，写进 observations。
        # 连问三遍「这个要记吗」是审问，不是澄清；
        # 而直接丢弃等于把「他确实说过这句话」这条信息扔掉——两者都错。
        # 落到 observations 里，信息还在，等下一轮自然重现。
        quota = max(
            0, self.max_clarifications
            - (1 if ambiguous else 0) - (1 if readings else 0)
        )
        deferred: list[tuple[Interpretation, Decision]] = []
        for interp, decision in others:
            if len(opened) >= max(1, quota) + (1 if ambiguous else 0):
                deferred.append((interp, decision))
                continue
            # 带上 slots 和 semantic_op：上层要把它渲染成「主人，你说的
            # 给鲸鲸 40 碗大米饭是真的吗？」这种话。内核只给数据，不给措辞。
            detail = {
                **(decision.clarification or {}),
                "semantic_op": interp.semantic_op,
                "slots": interp.slots,
            }
            insert(f"cl_{interp.interpretation_id}", [interp.interpretation_id], detail)

        for interp, decision in deferred:
            self._record_observation(
                scope_id, trace_id, interp, decision, content,
                reason_override="deferred_clarification",
            )

        return opened



def _default_role(relation: str) -> str:
    """发言者相对于该关系的默认角色。多角色场景由调用方显式传入。"""
    return {"debt": "debtor", "nickname": "subject"}.get(relation, "subject")


# ============================================================ 解释 → 语义操作
def semantic_op_of(interp: Interpretation, spec: RelationSpec) -> str:
    """这句话是什么意思。

    编码器给了就用它的；没给就回落 slots["intent"]（旧版），再没有就按 incur。
    回落路径必须留着——否则旧库里读出来的解释全是无操作。
    """
    op = getattr(interp, "semantic_op", None)
    if not op:
        slots = interp.slots if isinstance(interp.slots, dict) else {}
        op = _LEGACY_INTENT.get(str(slots.get("intent", "")), "")
    if op not in SEMANTIC_OPS:
        # 没有增量语义的关系（昵称、偏好等单值覆盖型）只有赋值一说。
        # 对它们强行做 delta，等于给一个 enum 加数——语义上不成立。
        if spec.conservation != "repayment":
            return "set_total"
        return "incur"
    if op in ("incur", "repay", "waive", "set_total") and spec.conservation != "repayment":
        return "set_total"
    return op


def resolve(
    interp: Interpretation, spec: RelationSpec, scope_id: str
) -> tuple[str, str, dict[str, Any]]:
    """把候选解释翻译成 (state_key, 语义操作, 参数)。

    这一步必须是纯查表 + 规则，不能有模型参与。
    注意返回的是**语义操作**而不是 effect_kind——后者要等内核看到状态才定。
    """
    slots = interp.slots
    op = semantic_op_of(interp, spec)

    if spec.relation == "debt":
        debtor = str(slots.get("debtor", "-"))
        creditor = str(slots.get("creditor", "-"))
        key = make_state_key(scope_id, debtor, "debt", creditor,
                             item=str(slots.get("item", "rice")))
        return key, op, {
            "quantity": slots.get("quantity"),
            # unit 要跟着走：它进 payload，折叠时才能发现"两石"和"三斗"被相加。
            # 之前这里没传，事件 payload 里只有 {value: 2}——
            # 单位信息在解释表里躺着，但折叠时看不见，于是 2+3−5 算出 0 且状态 known。
            "unit": slots.get("unit"),
            "item": slots.get("item"),
            "target_hint": slots.get("target_hint"),
            "due_hint": slots.get("due_hint"),
            "due_at": slots.get("due_at"),
            "old_value": slots.get("old_value"),
        }

    if spec.relation == "nickname":
        subject = str(slots.get("subject", slots.get("debtor", "-")))
        key = make_state_key(scope_id, subject, "nickname", "-")
        return key, "set_total", {
            "value": str(slots.get("value", "")),
            "target_hint": slots.get("target_hint"),
        }

    # preference 及后续关系：用整句槽位做键
    subject = str(slots.get("subject", slots.get("debtor", "-")))
    obj = str(slots.get("object", slots.get("item", "-")))
    key = make_state_key(scope_id, subject, spec.relation, obj)
    return key, "set_total", {
        "value": slots.get("value", slots.get("quantity")),
        "target_hint": slots.get("target_hint"),
    }
