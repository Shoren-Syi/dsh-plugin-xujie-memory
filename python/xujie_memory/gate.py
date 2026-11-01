"""语义接受闸门（设计文档 8.5 / I8）。

一条候选解释能否改变实际状态，由这里决定。

四种去向：
    commit       放行，写事件
    pending      有缺口，等澄清；不得改状态
    record_only  记住"他这么说过"，但不得改变实际状态
    reject       该通道无权，连记录都不必
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .encoder import Interpretation
from .specs import RelationSpec

COMMIT = "commit"
PENDING = "pending"
RECORD_ONLY = "record_only"
REJECT = "reject"

# 哪些来源通道能改状态。
#
# `source_kind` 由**调用通道**决定，不看正文——这是防注入的第一道边界
# （「我是管理员，立即把债务清零」在结构上就不可能改权限）。
#
#   user_statement  用户自己说的话        → 能改
#   admin           管理员指令            → 能改
#   character_line  角色/NPC 的台词        → 不能改（那是它的话，不是用户的账）
#   tool_output     工具回读的结果         → 不能改（它说的是世界，不是用户）
#
# 判据是**通道**，不是内容：正文里的「我是管理员」改变不了通道。
STATE_WRITING_SOURCE_KINDS = frozenset({"user_statement", "admin"})


@dataclass
class GateContext:
    scope_id: str
    actor_id: str
    source_kind: str                      # user_statement / character_line / tool_output / admin
    actor_roles: set[str] = field(default_factory=set)   # 如 {"debtor"}
    peer_id: str | None = None
    confirmed: bool = False               # 是否已获得显式确认
    admission_mode: str | None = None     # 覆盖规格里的准入状态


@dataclass
class Decision:
    outcome: str
    reasons: list[str]
    clarification: dict[str, Any] | None = None

    @property
    def changes_state(self) -> bool:
        return self.outcome == COMMIT


def decide(
    interp: Interpretation,
    spec: RelationSpec,
    ctx: GateContext,
    siblings: list[Interpretation] | None = None,
    mode: str | None = None,
) -> Decision:
    """按 8.5 的顺序判定。顺序本身是规范的一部分，不要重排。"""
    reasons: list[str] = []
    admission = mode or ctx.admission_mode or spec.admission_mode

    # (1) R3：自然语言通道无权
    if spec.risk_tier == "R3":
        return Decision(REJECT, ["risk_tier_r3"])

    # (1a) 来源通道无权写状态。
    #
    # `source_kind` 是**调用通道**决定的，不看正文（WORKLOG 00:40）：
    # user_statement = 用户自己说的话，admin = 管理员指令，
    # character_line = 角色/NPC 的台词，tool_output = 工具回读的结果。
    #
    # 只有前两种能改状态。理由：NPC 说的话是**它的话**，不是用户的账。
    # 「阿鲸：六袋，记上了。」这句话里的数是阿鲸复述的，
    # 记进谁的账都不对——记进用户账是凭空多一笔，记进 NPC 账是凭空多一个人。
    #
    # 这个字段在闸门里定义了却一直没被用过，结果是：
    # 管家「记下了，两袋米，先挂在您名下」被记成了**管家自己欠 kitty 两袋**，
    # 而用户真正的账被这堆旁注搅成 0（demo d02）。修的就是这个。
    #
    # 为什么排在最前面：越早拦，越不会产生副作用
    # （澄清请求、锚点、权��事件都在闸门之后）。
    if ctx.source_kind not in STATE_WRITING_SOURCE_KINDS:
        reasons.append(f"source_kind_not_writable:{ctx.source_kind}")
        return Decision(RECORD_ONLY, reasons)

    # (2) 多个互斥候选。
    #
    # 必须排在模态之前：一句话有多个互斥解释时，整句都应该挂起，
    # 而不是"第一个候选走这条、第二个候选走那条"——那等于让调用方去猜该信哪个。
    # 只有当其中至少一个候选本来能生效时，才算"有歧义"；
    # 全是玩笑/转述的多个候选，各自 record_only 就行。
    if _has_conflicting_candidates(interp, siblings, spec):
        reasons.append("ambiguous_candidates")
        return Decision(PENDING, reasons, {
            "kind": "ambiguous", "relation": spec.relation,
            "candidates": len(siblings or []),
        })

    # (3a) 报告状态：「还清了」「两清了」
    #
    # 这不是一次操作。判成 repay 会让账上多一条 0 元还款，再被守恒检查拒绝，
    # 状态从 known 变成 disputed——**余额对、状态脏**。
    #
    # 为什么排在槽位检查之前：报告状态**不需要数量**。账上还没记过账时
    # 说「还清了」，如果先查槽位就会变成"请补充数量"——而用户根本没有数
    # 可以补。它该安静地记一条旁注。
    #
    # 余额到底清没清，闸门看不到（纯函数），所以这里只管"不落库"，
    # 差额由 ingest 的后置检查去问（`_settled_gap`）。
    if interp.signals.get("settled_report"):
        reasons.append(f"settled_report:{interp.signals['settled_report']}")
        return Decision(RECORD_ONLY, reasons)

    # (3b) 描述句：带数量、没有交易动词
    #
    # 「那两本是画册」「那十个是双黄蛋」——它们在说"这些东西是什么"。
    # 判成 incur 会让账上凭空多数，而这类句子在日常对话里密度很高
    # （说明来源、品质、种类），漏挡一句就偏一笔。
    #
    # 排在槽位之前：描述句本来就不需要数量来做账，问"请补充数量"没有意义。
    # **只拦"看起来像新增/还款"的那几种**。
    #
    # 「不对，一开始是十坛」也有数量、也没有交易动词，但它在改一笔账；
    # 「凑个整，四十五枚」同样没有交易动词，但它是 set_total。
    # 把它们一起拦掉，等于把改口和改写总额这两个功能关掉。
    # 只认**明确说了**是新增/还款/免除的。op 为空（编码器没给）时不拦——
    # 那说明上游自己也没定义这句话算什么，闸门不该替它下结论，
    # 走原来的 pending 反而更安全（样本 d12/d45 就是这种情况）。
    if interp.signals.get("describe_only") and _op_of(interp) in ("incur", "repay", "waive"):
        reasons.append("describe_only")
        return Decision(RECORD_ONLY, reasons)

    # (3c) 余额复核：「还差一百二对吧？」
    #
    # 它在确认"还剩多少"，不是又一次还款。判成 repay 会把余额再扣一遍，
    # 而且账面看不出异常——这正是 XM-38 的原症状。
    #
    # 闸门看不到账（纯函数），所以这里只判"不落库"；
    # 说的数和账面是否一致，由 ingest 的后置检查去比（`_balance_check`）。
    if interp.signals.get("balance_check"):
        reasons.append(f"balance_check:{interp.signals['balance_check']}")
        return Decision(RECORD_ONLY, reasons)

    # (3d) 跟账本无关的话：「对了你吃了吗」「说定了」「那捆有点潮」
    #
    # 排在槽位检查之前是**这一条的全部意义**：这类句子没有数量、没有交易动词，
    # 走槽位检查必然落到"请补充 quantity"，而用户根本不知道你要他补什么。
    # G2 里 21 次空问有 8 次是这么问出来的。
    #
    # 但排在 (3a)(3b)(3c) 之后：那三条都是"看懂了、只是不该落库"，
    # 理由比"无关"更具体，用户看到的解释也更准。
    #
    # 三条边界，缺一条就会误伤：
    #   · **unclear 不拦**——「随便吧」也什么成分都没有，但它不是"跟账无关"，
    #     是"我看不懂"，该问一句（d21）。
    #   · **只拦账本关系**——「我叫阿鲸」同样没有数量和动词，可它在改称呼，
    #     属于 nickname 自己的槽位与准入流程（d55）。
    #   · correct / retract 不拦——「那个不算」也没有数量，但它在撤一笔账。
    #
    # 但 **preference 有一个例外**：当这个 preference 是**猜出来的**
    # （`relation_guessed`），而不是句子里真有偏好证据时，它也该被拦。
    #
    # 为什么要有这个例外：preference 是关系判定的**兜底值**。
    # 「今天风真大啊。」「外头挺热闹的」「你把窗关上吧」
    # 没有任何领域词，于是落到兜底值上，再因为抽不到 object 而追问
    # 「你喜欢什么」——用户说句闲话就被反问。这是"无感"最直接的破坏。
    #
    # 为什么不会误伤真的偏好句：d18/d19/d51 那几条**有明确的偏好证据**
    # （喜欢/讨厌/最爱），编码器不会给它们打 `relation_guessed`。
    # 只有"既没证据、也没上文可继承"的那种猜测才走这条。
    if (interp.signals.get("no_claim")
            and interp.modality != "unclear"
            and (spec.relation not in ("preference", "nickname")
                 or interp.signals.get("relation_guessed"))
            and _op_of(interp) not in ("correct", "retract")):
        reasons.append("no_claim")
        return Decision(RECORD_ONLY, reasons)

    # (3e) 复核旧账：「上个月那两袋也没算错吧？」
    #
    # 和 (3c) 的区别：那条在问**余额**，这条在问**某一笔**。
    # 共同点是都**不是新账**——漏判一次，余额就多一份。
    if (interp.signals.get("recall_check")
            and _op_of(interp) not in ("correct", "retract")):
        reasons.append(f"recall_check:{interp.signals['recall_check']}")
        return Decision(RECORD_ONLY, reasons)

    # (3f) 余额查询：「还剩几捆？」「我一共欠多少？」
    #
    # 不是在报账，是在问账。判成 incur 会凭空记一笔 0；
    # 走槽位检查会反问"请补充数量"——**答非所问**，比不答更伤。
    #
    # 闸门在这里只做一件事：别落库、别发问。
    # 真正的答案由 ingest 查完账后放进 `IngestResult.answers`，
    # 因为闸门是纯函数，看不到账本。
    if interp.signals.get("balance_query"):
        reasons.append(f"balance_query:{interp.signals['balance_query']}")
        return Decision(RECORD_ONLY, reasons)

    # (3g) 承诺的施事不是说话人
    #
    # 「这坛是替老李赊的，他明天来取」——时间是明天，动作是他来取。
    # 按 promise 记是安全的（不改余额），但不能进承诺表：
    # 到期那天系统会催一件别人的事，那比不记更伤。
    #
    # 编号原为 (5b)。**挪到这里是因为顺序本身是个 bug**：
    # 「老李那坛他自己还」缺 quantity，卡在 (5) 的槽位检查上被反问
    # "请补充数量"——而"这不是我的账"根本不需要数量。
    # 这和 (3) 上面那条注释是同一个道理：先判有没有资格，再判材料够不够。
    if _op_of(interp) == "promise" and interp.signals.get("third_party"):
        reasons.append("promise_by_third_party")
        return Decision(RECORD_ONLY, reasons)

    # (3h) 这笔账不是我的：「那三十枚是老张代借的，我记他头上」
    #
    # 代借、代赊、帮他垫——动作发生了，但**债务人不是说话人**。
    # 记到我的账上等于凭空欠一笔（c03 第 7 句：40 → 70，后面三句全崩）。
    #
    # 为什么只判 record_only、不新建一本账：本内核一本账只有一个 debtor，
    # 跨主体的账要么另开 scope，要么由上层按 `third_party` 转记。
    # 记旁注至少保证"这笔发生过、归属存疑"留在库里，而不是被静默丢掉。
    #
    # 编号原为 (5c)，同样是为了"先判资格"提前。
    if (interp.signals.get("third_party")
            and _op_of(interp) in ("incur", "repay", "waive")):
        reasons.append(f"third_party:{interp.signals['third_party']}")
        return Decision(RECORD_ONLY, reasons)

    # (3) 模态不允许产生实际事件。
    #
    # 顺序在这里和 v0.3 文档 8.5 不同：文档把「缺槽位」放在「模态」之前。
    # 实测下来这个顺序是错的——「我什么时候欠你了」是否认，本来就没有数量，
    # 先查槽位会让它变成 pending（"请补充数量"），而正确反应是 record_only（"记下，不生效"）。
    #
    # 判断依据：**如果这句话根本不是实际陈述，讨论槽位全不全没有意义。**
    # 先判"有没有资格"，再判"材料够不够"。
    #
    # unclear 不算"另一种模态"，交给 (4)：它表达的是"不知道是哪种"，
    # 不是"知道是哪种但不允许"。
    allowed = interp.modality in spec.allowed_modalities

    # denied + 修正操作不冲突（XM-17）。
    #
    # 「不对不对，是 25 碗」是否认**上一个数值**，不是否认"这件事存在"。
    # 后者由 retract 表达（"我不欠你"），前者正是 correct 的语义。
    # 不放行的话，每一次"我记错了数"都会被当成"他在否认欠款"而不入库——
    # 实测 flipflop 段 5 次改口有 4 次卡在这里。
    #
    # 安全性：correct 仍然要求 quantity + 可定位的目标事件，
    # 真否认（"我哪有欠你 40 碗"）通常不带数量，会被后面的槽位检查拦住。
    # 修正操作与模态解耦（XM-17 的推广）。
    #
    # 一开始只放行 `denied`：「我哪有欠你 40 碗」里的否定带着 correct 语义。
    # 但「我刚才说错了，下个月还三碗」的模态是 planned——
    # 因为**被修正的那句话**是承诺。这时按模态拦，等于说
    # "你可以改口，但不能改口一件还没发生的事"，而改口恰恰最常发生在承诺上。
    #
    # 判据：修正的对象是"我说过的话"，不是"这件事存在"。
    # 只有玩笑 / 反讽 / 转述 / 角色台词仍然一律挡住——
    # 那些连"他说过这话"都不算数。
    if not allowed and interp.modality not in ("joking", "ironic", "quoted", "in_world"):
        op = getattr(interp, "semantic_op", None) or str(
            (interp.slots or {}).get("intent", "") if isinstance(interp.slots, dict) else ""
        )
        if op in ("correct", "retract"):
            allowed = True
            # 老理由码保留：下游可能按字符串分支（ISSUES 里写明过兼容要求）。
            reasons.append(
                "denied_allowed_for_revision" if interp.modality == "denied"
                else "revision_allowed_for_modality"
            )

    if interp.modality != "unclear" and not allowed:
        reasons.append(f"modality_not_allowed:{interp.modality}")
        return Decision(RECORD_ONLY, reasons)

    # (4) 模态不明
    if interp.modality == "unclear":
        reasons.append("unclear_modality")
        return Decision(PENDING, reasons, {"kind": "ambiguous", "relation": spec.relation})

    # (5) 必填槽位缺失。
    #
    # `retract` 例外：「那个不算」本来就不带任何数值，它指向的是既有事件。
    # 要求它填 quantity，等于要求用户重复一遍他正在取消的内容。
    #
    # slots 兜底：从库里读出来的旧解释、或编码器返回了奇怪形状时，
    # 当作空槽位处理——按"缺槽位"挂起，比 AttributeError 崩掉好。
    slots = interp.slots if isinstance(interp.slots, dict) else {}
    op = getattr(interp, "semantic_op", None) or slots.get("intent")
    if op != "retract":
        missing = [k for k in spec.required_slots if slots.get(k) in (None, "")]
        if missing:
            reasons.extend(f"missing_slot:{m}" for m in missing)
            return Decision(PENDING, reasons, {
                "kind": "missing_slot", "missing": missing, "relation": spec.relation,
            })

    risky = _risk_signals(interp, spec)
    if risky:
        reasons.extend(f"risk_signal:{r}" for r in risky)
        return Decision(PENDING, reasons, {"kind": "ambiguous", "relation": spec.relation})

    # (5a) 复述：这句话在指一笔已经记过的账
    #
    # 「今天这十枚也算进去」——账上真有一笔 -10，那这句是确认，不是又一次还。
    # 判成新增会让余额再掉十枚，而且**看起来完全正常**（状态 known、值合理）。
    #
    # 标记由 ingest 打，因为它要读事件流；这里只管用。
    if interp.signals.get("restatement"):
        reasons.append(f"restatement:{interp.signals['restatement']}")
        return Decision(RECORD_ONLY, reasons)

    # (6) 发言者不在 writers 中
    if spec.writers and not (set(spec.writers) & ctx.actor_roles):
        reasons.append("actor_not_writer")
        # 角色台词不写状态，但可留作 in_world 记录
        return Decision(RECORD_ONLY, reasons)

    # (6b) 求确认：说话人自己也没把握
    #
    # 「我上个月是不是欠你五十枚金币来着？」——他认定这件事成立，
    # 只是在等对方点头。这种句子**不能自动生效**，哪怕关系的准入是 auto，
    # 因为它连"说话人自己确认过"都不成立；但它也**不能丢**，因为它是一条账。
    #
    # 唯一的正确反应：问一句，等他点头。这正是 assisted 已有的动作，
    # 区别只是触发理由从"系统不确定"变成"说话人也不确定"。
    if interp.signals.get("seeking_agreement"):
        reasons.append("speaker_seeking_agreement")
        return Decision(PENDING, reasons, {
            "kind": "unconfirmed", "relation": spec.relation,
            "hint": "说话人自己在求确认，不能替他决定",
        })

    # (7) 需要确认而未确认
    #
    # `authority` 永远要确认——它要的是"谁批的"，不是"确不确定"，
    # 跟准入等级无关。
    #
    # `explicit` 在准入升到 auto 之后**不再拦**。这一条是刻意改的：
    # 之前 admission=auto 但 confirmation=explicit 仍然每句都问，
    # 于是"准入晋级"形同虚设——澄清率 78% 有一半是这里来的。
    # **"自动"必须真的自动**，否则那套机制只是在自我安慰。
    needs_confirm = (
        spec.confirmation == "authority"
        or (spec.confirmation == "explicit" and admission != "auto")
    )
    if needs_confirm and not ctx.confirmed:
        reasons.append("confirmation_required")
        return Decision(PENDING, reasons, {
            "kind": "unconfirmed", "relation": spec.relation,
            "hint": _confirmation_hint(spec),
        })

    # (7b) 准入状态为 assisted：放行需确认或人工审核
    if admission == "assisted" and not ctx.confirmed:
        reasons.append("admission_assisted")
        return Decision(PENDING, reasons, {
            "kind": "unconfirmed", "relation": spec.relation,
        })

    # (8) 准入状态为 shadow：只记录"若放行会如何"
    if admission == "shadow":
        reasons.append("shadow_mode")
        return Decision(RECORD_ONLY, reasons)

    # (9) 其余放行。约束违反由内核在提交时转 disputed，这里不预判。
    return Decision(COMMIT, reasons or ["accepted"])


def _has_conflicting_candidates(
    interp: Interpretation,
    siblings: list[Interpretation] | None,
    spec: RelationSpec,
) -> bool:
    """签名不同的候选就算歧义，整句一起挂起。

    不按「是否可用模态」过滤：`asserted` 和 `planned` 指向两个不同的世界，
    只判其中一条，等于替用户做了选择。

    反例：两个候选都是 `joking` 且同一关系——签名相同，不算歧义，
    各自 record_only 就好。所以过滤条件是"签名是否相同"，不是"有几条"。
    """
    if not siblings or len(siblings) < 2:
        return False
    signatures = {(s.relation, s.modality, s.polarity) for s in siblings}
    return len(signatures) > 1


def _risk_signals(interp: Interpretation, spec: RelationSpec) -> list[str]:
    """8.4 的信号。只用于触发更严格处理，不用于证明是实际陈述。

    按风险等级取不同的敏感度：R0 误拦的代价比漏放高（用户会觉得系统很笨），
    R2/R3 则相反。门槛由错误后果决定，不由信号本身决定。
    """
    risky: list[str] = []
    if interp.signals.get("parse_error"):
        risky.append("parse_error")
        return risky

    if spec.risk_tier == "R0":
        return risky

    for name in ("joking", "ironic", "conditional", "quoted", "planned", "hypothetical"):
        if interp.signals.get(name):
            risky.append(name)

    # 否定的陈述：R1 以上才当作风险，R0 的"不喜欢"是正常偏好变更
    #
    # 修正操作例外（XM-19）：correct / retract 天生带否定语气
    # （「不对不对，是25」「那个不算」），它否认的是**上一个值/上一条记录**，
    # 不是"这件事不存在"。把它当风险，会导致确认之后**仍然挂起**——
    # 用户点了头，系统还是不敢记，那句就永远提交不了。
    #
    # 实测：flipflop 段第 2 句 3 轮全部卡在 `risk_signal:negated`。
    op = getattr(interp, "semantic_op", None) or str(
        (interp.slots or {}).get("intent", "") if isinstance(interp.slots, dict) else ""
    )
    revising = op in ("correct", "retract")
    if interp.polarity == "negate" and spec.risk_tier in ("R1", "R2", "R3") and not revising:
        risky.append("negated")

    return risky


def _op_of(interp: Interpretation) -> str:
    """语义操作。编码器给了就用；旧数据回落 slots["intent"]（与 ingest 同规则）。"""
    op = getattr(interp, "semantic_op", None)
    if op:
        return str(op)
    slots = interp.slots if isinstance(interp.slots, dict) else {}
    return str(slots.get("intent") or "")


def _confirmation_hint(spec: RelationSpec) -> str:
    if spec.risk_tier == "R2":
        return "高影响关系，需要明确确认后才生效"
    return "需要确认后才生效"
