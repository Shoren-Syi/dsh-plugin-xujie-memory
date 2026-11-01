"""认知判断：把线索、共同回忆、画像组织成一个**暂时**结论。

方案 2.1 C 说的是三件事：

    解释矛盾、评估人物一致性、调取共同回忆并形成暂时判断。

关键词是「暂时」。这一层不宣布"这个人是谁"——
它只回答"我现在更倾向哪种解释，凭什么，还差什么"。

---

## 为什么判断和授权必须分开

5.3 有一句容易被忽略但很重的话：

> 回答正确不能自动开放私人信息。

意思是：即使对方答对了你们的共同经历，也不等于他是本人。
共同回忆会泄露（他自己说过、别人听过、猜也能猜个大概）。
所以「我判断他是同一个人」和「我可以把私人记忆给他看」
是**两个独立的问题**，不能用一个 verdict 同时回答。

`may_share_sensitive()` 因此**不看 verdict**。它只看：
画像是不是可疑、有没有人明说过"我不是本人"。
判断错了最坏是语气不对；授权错了是不可逆的泄露。

## 为什么会有 `unknown`

因为绝大多数情况真的判断不了。

刚聊两句、对方只说了一句不像的话、你手上只有一条旧记忆——
这些都是 `unknown`，而且**这就是正确答案**。
强行给一个 same/different，只是把不确定性藏起来，
而藏起来的不确定性会在后面变成更贵的错误。

方案 5.1 也说得很清楚：主 AI「尝试解释」，不是「必须解释」。

## 这一层不生成台词

它给出的是「有什么证据、还差什么、能不能给信息」。
要不要问「你还记得那次丢钱的事吗」——那是主 AI 的事。
把试探话术写进这里，五种角色就会问出同一句话。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .profile import ProfileStore
from .recall import Budget, Recall

# 三种结论。`unknown` 是合法答案，不是失败。
SAME = "same_person"
DIFFERENT = "different_person"
UNKNOWN = "unknown"

# 证据的方向：正数指向"是同一个人"，负数指向"不是"。
# 数值只用于排序与解释，不追求精确——这里没有"准确率"可谈，
# 因为真正的判断在主 AI 那里。
_WEIGHTS = {
    "shared_memory": 1.0,      # 对方主动提到只属于你们的经历
    "answered_recall": 1.5,    # 对方答对了你问的共同经历
    "profile_fit": 0.5,        # 说的话跟画像对得上
    "explicit_claim": -3.0,    # 明说"我不是本人"
    "address_shift": -1.5,     # 自称的称呼变了
    "profile_miss": -1.0,      # 说的话跟画像明显不符
}


@dataclass
class Evidence:
    """一条证据。必须能追到出处，否则它只是印象。"""

    kind: str
    summary: str
    weight: float
    trace_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "weight": self.weight,
            "trace_id": self.trace_id,
        }


@dataclass
class Assessment:
    """一份**暂时**判断。

    没有 `is_this_person` 这种布尔字段，只有一个带置信度的倾向。
    布尔会诱使下游把它当事实用。
    """

    verdict: str = UNKNOWN
    confidence: float = 0.0
    evidence: list[Evidence] = field(default_factory=list)
    shared_memories: list[dict] = field(default_factory=list)
    # 还差什么。给主 AI 决定要不要继续问，以及问哪一类。
    open_questions: list[str] = field(default_factory=list)
    # 能不能给敏感信息。**与 verdict 无关**（5.3）。
    may_share_sensitive: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return sum(e.weight for e in self.evidence)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "confidence": round(self.confidence, 3),
            "score": round(self.score, 3),
            "evidence": [e.as_dict() for e in self.evidence],
            "shared_memories": self.shared_memories,
            "open_questions": self.open_questions,
            "may_share_sensitive": self.may_share_sensitive,
            "reasons": self.reasons,
        }

    def render(self) -> str:
        """给人看的一段话。"""
        names = {SAME: "像是同一个人", DIFFERENT: "不像同一个人",
                 UNKNOWN: "判断不了"}
        lines = [f"倾向：{names[self.verdict]}（置信 {self.confidence:.2f}）"]
        if self.evidence:
            lines.append("依据：")
            for e in self.evidence:
                mark = "＋" if e.weight > 0 else "－"
                lines.append(f"  {mark} {e.summary}")
        if self.shared_memories:
            lines.append("你们的共同经历：")
            for m in self.shared_memories:
                lines.append(f"  - {m['content']}")
        if self.open_questions:
            lines.append("还差：" + "、".join(self.open_questions))
        lines.append(
            "敏感信息：" + ("可以给" if self.may_share_sensitive else "不能给")
        )
        return "\n".join(lines)


class Judge:
    """认知判断。只出结论，不改任何状态。"""

    def __init__(self, engine, *, recall: Recall | None = None,
                 profiles: ProfileStore | None = None):
        self.engine = engine
        self.recall = recall or Recall(engine)
        self.profiles = profiles or ProfileStore(engine)

    # ---------------------------------------------------------- 判断
    def assess(
        self,
        scope_id: str,
        account_id: str,
        *,
        hints: list[dict] | None = None,
        answered: list[dict] | None = None,
        probes: list[str] | None = None,
        budget: Budget | None = None,
    ) -> Assessment:
        """形成一份暂时判断。

        参数都是**上层观察到的事实**，不是模型猜的：

            hints     画像异常线索（来自 ProfileStore.watch）
            answered  对方答对了哪些共同经历 [{"summary":..., "trace_id":...}]
            probes    上层想问的方向，用来决定要不要拉回旧记忆

        这一层不做自然语言理解。它只把已有材料组织成一个结论。
        """
        assessment = Assessment()
        profile = self.profiles.get(scope_id, account_id)

        # ---- 1. 异常线索：这是"不像"的那一半
        for hint in hints or []:
            kind = hint.get("kind", "")
            weight = _WEIGHTS.get(kind, 0.0)
            if weight == 0:
                continue
            assessment.evidence.append(Evidence(
                kind=kind,
                summary=_describe_hint(hint),
                weight=weight,
                trace_id=hint.get("evidence_trace_id"),
            ))

        # ---- 2. 答对的共同经历：这是"像"的那一半
        #
        # 注意 5.3：这一条**只影响判断，不影响授权**。
        # 答对了说明"他知道一些事"，而知道不等于他是那个人。
        for hit in answered or []:
            assessment.evidence.append(Evidence(
                kind="answered_recall",
                summary=f"答对了：{hit.get('summary', '')}",
                weight=_WEIGHTS["answered_recall"],
                trace_id=hit.get("trace_id"),
            ))

        # ---- 3. 拉回共同经历。只在需要判断时才拉——
        # 常规对话不查这个，那是方案 9 说的"异常触发深度分析"。
        if hints or answered or probes:
            assessment.shared_memories = self._shared_memories(
                scope_id, probes or [], budget,
            )

        # ---- 4. 画像对得上吗
        if profile:
            fit = self._profile_fit(profile, hints or [])
            if fit is not None:
                assessment.evidence.append(fit)

        # ---- 5. 综合
        self._conclude(assessment)

        # ---- 6. 还差什么
        assessment.open_questions = self._open_questions(assessment, profile)

        # ---- 7. 授权：**独立计算**，不看 verdict
        assessment.may_share_sensitive = self.may_share_sensitive(
            scope_id, account_id,
        )
        return assessment

    def may_share_sensitive(self, scope_id: str,
                            account_id: str) -> bool:
        """能不能把这个人的私人记忆给他看。

        **不看判断结果。** 理由写在模块说明里：
        答对共同回忆不构成认证，共同回忆本来就会泄露。

        三个条件，缺一不可：

            1. 有画像，而且没被标成可疑
            2. 没有 open 的"明说不是本人"线索
            3. 至少有一条 confirmed 的身份类标签

        第 3 条容易被跳过，但它是唯一能证明"这个账号跟这个人建立过关系"的东西。
        没有它，一个刚建的画像也能拿到授权。
        """
        profile = self.profiles.get(scope_id, account_id)
        if profile is None:
            return False
        if profile["status"] != "active":
            return False

        for alert in self.profiles.open_alerts(scope_id, account_id):
            if alert["kind"] == "explicit_claim":
                return False

        confirmed = self.profiles.tags(scope_id, account_id, status="active")
        return any(
            t["source"] == "confirmed" and t["kind"] in ("address", "relation")
            for t in confirmed
        )

    # ---------------------------------------------------------- 内部
    def _shared_memories(self, scope_id: str, probes: list[str],
                         budget: Budget | None) -> list[dict]:
        """拉回"我们之间发生过的事"。

        检索词用 probes；没有 probes 就退化成一个宽查询。
        预算给得很紧——这是**异常时才走**的路径，
        常规对话不该把整段历史翻一遍（方案 9 成本控制）。
        """
        budget = budget or Budget(max_rounds=1, max_candidates=12,
                                  max_items=4, max_chars=800)
        text = " ".join(p for p in probes if p) or "我们"
        result = self.recall.pull(scope_id, text, limit=budget.max_items,
                                  budget=budget)
        return [
            {
                "content": t["content"],
                "trace_id": t["trace_id"],
                "priority": t.get("priority"),
                "anchor": t.get("anchor", False),
            }
            for t in result.traces
        ]

    def _profile_fit(self, profile: dict,
                     hints: list[dict]) -> Evidence | None:
        """说的话跟画像对得上吗。

        只做一个很粗的判断：有异常线索就是"不符"，没有就是"没有反证"。
        刻意不做"风格相似度"——那需要模型，而且它的误报会把
        "今天心情不好"判成"换了人"。
        """
        if hints:
            return Evidence(
                kind="profile_miss",
                summary="说的话跟画像不符",
                weight=_WEIGHTS["profile_miss"],
            )
        return Evidence(
            kind="profile_fit",
            summary="没有发现跟画像冲突的地方",
            weight=_WEIGHTS["profile_fit"],
        )

    def _conclude(self, a: Assessment) -> None:
        """把证据折成一个倾向。

        阈值是刻意保守的：只有证据**明确**偏向一边才给结论，
        否则一律 `unknown`。宁可说"判断不了"，也不要给一个
        看起来确定、其实靠猜的答案。
        """
        score = a.score
        if score >= 2.0:
            a.verdict = SAME
            # 置信度不追求高分：最高 0.85。留一截，
            # 是因为这个判断没有经过任何真正的认证。
            a.confidence = min(0.85, 0.4 + score * 0.1)
            a.reasons.append("有足够多指向同一个人的证据")
        elif score <= -2.0:
            a.verdict = DIFFERENT
            a.confidence = min(0.85, 0.4 + abs(score) * 0.1)
            a.reasons.append("有明确指向'不是本人'的证据")
        else:
            a.verdict = UNKNOWN
            a.confidence = 0.0
            a.reasons.append("证据不足以判断，先不下结论")

    def _open_questions(self, a: Assessment,
                        profile: dict | None) -> list[str]:
        """还差什么。给方向，不给话术。"""
        if a.verdict == SAME:
            return []
        if profile is None:
            return ["还没有任何关于这个人的记录"]

        missing: list[str] = []
        if not a.shared_memories:
            missing.append("没有拉回到共同经历")
        if not any(e.weight > 0 for e in a.evidence):
            missing.append("没有支持'是本人'的证据")
        if any(e.kind == "explicit_claim" for e in a.evidence):
            missing.append("对方明说过不是本人，需要账号主人确认")
        return missing


def _describe_hint(hint: dict) -> str:
    kind = hint.get("kind", "")
    if kind == "address_shift":
        return "自称的称呼跟以往不同"
    if kind == "explicit_claim":
        return "对方说不是本人"
    return kind or "未知线索"
