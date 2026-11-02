"""认知协调器：一次交互的编排。

方案 2.2 的流程，落到一个 `handle()` 里：

    用户输入
      → 观察（画像异常线索）
      → 记忆写入（判定层）
      → 【异常才】深度分析（判断层）
      → 按需检索
      → 情绪与关系
      → 组织材料
    → 交给主 AI 生成回应

---

## 它不生成台词

协调器给主 AI 的是一包**材料**：这个人是谁、现在什么心情、说过什么、
有没有需要留意的。**没有一句"你应该这么说"。**

理由和角色卡那条一样：话术一旦写进内核，所有角色就会说同一种话。
而方案 2.2 最后一步写的是「主 AI 生成最终回应」——那一步不在这个仓库里。

## 为什么深度分析要"异常才做"

方案第九章的成本控制，落成两条：

    常规一轮：一次检索，几条记忆，不碰判断层
    异常一轮：才调画像、共同回忆、矛盾证据

不做这个区分，每一句"今天天气不错"都会触发一次历史翻查——
那既慢又贵，而且**没有必要**：没有异常的时候，判断层给不出任何结论，
只会回一个 `unknown`。花着钱拿一个"我不知道"，不如不花。

## 为什么情绪要由调用方给

`feelings` 参数是结构化的（`summary / severity / anger / trust_delta`）。
协调器不猜"这句话该多生气"——那需要一个价值判断模型，
而且它的错误会直接改关系状态。

方案 2.2 里那一步叫「情绪与关系状态计算」，
而"算"这个字要求输入是确定的。**上半句是理解，下半句是算术。**
理解交给模型，算术留在这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import detect
from . import importance as auto_importance
from .encoder import RuleEncoder
from .ingest import Ingest
from .judge import Assessment, Judge
from .profile import ProfileStore
from .recall import Budget as RecallBudget
from .recall import Recall


def _echo_key(text: str) -> str:
    """比对「是不是同一句」用的键：去掉空白与标点，只留字。

    为什么不能直接比字符串：宿主可能把同一句话分块送来、带换行，
    或者用户自己重打了一遍标点不同的话——那些都还是「刚说的这句」，
    不该被当成一件往事喂回去。
    """
    keep = []
    for ch in (text or ""):
        if ch.isspace() or ch in "，。！？、；：“”‘’（）()[]【】,.!?;:\"'`~…—-":
            continue
        keep.append(ch.lower())
    return "".join(keep)


# 澄清类型 → 一句人话。
#
# 内核只给数据不给措辞，但材料是**给模型读的**。
# 直接甩一个 `missing_slot` 过去，模型要么忽略，要么照着念术语。
_CLARIFICATION_LABELS = {
    "ambiguous_candidates": "同一句话能理解成好几种，还没选定是哪一种",
    "total_reading": "「一共多少」有两种读法，还没选定",
    "missing_slot": "缺一个关键信息（比如数量、对象）",
    "unconfirmed": "需要确认这句话是不是这个意思",
}


@dataclass
class TurnBudget:
    """一轮对话能花多少。

    默认值按"常规一轮"给；异常时上层可以放开 `deep_analysis`。
    """

    deep_analysis: bool = True        # 允不允许在异常时做深度分析
    max_recall: int = 4               # 常规检索最多几条
    max_deep_recall: int = 3          # 深度分析最多拉几条共同回忆
    max_context_chars: int = 1500     # 喂给主 AI 的材料上限
    # ---- 澄清熔断 ----
    #
    # 默认值是按"一次普通交流能忍几次追问"定的，不是按"能问几次"定的。
    # 一轮最多 1 件、一个会话最多主动问 6 次——超过这个数，
    # 用户感受就从"这助手挺细心"变成"怎么老问"。
    max_questions: int = 1            # 一轮最多提几件还没确定的事
    max_asked_total: int = 6          # 一个会话累计问够这么多次就不再新开


@dataclass
class Turn:
    """一轮的结果。**材料，不是回应。**"""

    trace_id: str = ""
    # 记忆层：这句话被判定成什么、进了账没有
    outcomes: list = field(default_factory=list)
    clarifications: list = field(default_factory=list)
    # 画像层：发现了什么异常
    alerts: list = field(default_factory=list)
    # 判断层：只有异常时才不为 None
    assessment: Assessment | None = None
    # 检索：拉回了什么
    recalled: list = field(default_factory=list)
    # 给主 AI 的材料
    context: str = ""
    budget_used: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def escalated(self) -> bool:
        """这一轮有没有升级到深度分析。看日志时最想知道的就是这个。"""
        return self.assessment is not None

    def as_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "outcomes": [
                {"relation": o.interpretation.relation,
                 "outcome": o.decision.outcome,
                 "reasons": list(o.decision.reasons)}
                for o in self.outcomes
            ],
            "clarifications": [c.get("kind") for c in self.clarifications],
            "alerts": [a.get("kind") for a in self.alerts],
            "assessment": self.assessment.as_dict() if self.assessment else None,
            "recalled": [t["content"] for t in self.recalled],
            "anchored": [t["content"] for t in self.anchored],
            "budget_used": self.budget_used,
            "warnings": list(self.warnings),
        }


class Orchestrator:
    """把五层串起来跑一轮。"""

    def __init__(
        self,
        engine,
        *,
        encoder=None,
        profiles: ProfileStore | None = None,
        judge: Judge | None = None,
        recall: Recall | None = None,
        mind=None,
        auto_confirm: bool = False,
    ):
        """`auto_confirm` 是**压测与演示**专用：模拟"用户会点头"。

        生产环境不要开——澄清的存在意义就是等人回答，
        自动点头等于把 assisted 关系悄悄升成 auto。
        """
        self.engine = engine
        self.profiles = profiles or ProfileStore(engine)
        self.recall = recall or Recall(engine)
        self.judge = judge or Judge(engine, recall=self.recall,
                                    profiles=self.profiles)
        self.ingest = Ingest(engine, encoder or RuleEncoder())
        self.mind = mind
        self.auto_confirm = auto_confirm
        # 一轮一个 source_ref。**不能只靠时间戳**：同一秒内可能来两句
        # （自动补测、并发用户、回放脚本），撞上幂等键就直接抛异常。
        self._seq = 0
        self._derive_turns: dict[str, int] = {}

    def handle(
        self,
        scope_id: str,
        account_id: str,
        text: str,
        *,
        actor_id: str | None = None,
        source_kind: str = "user_statement",
        context: dict | None = None,
        feelings: dict | None = None,
        budget: TurnBudget | None = None,
        probes: list[str] | None = None,
        session: str | None = None,
    ) -> Turn:
        """跑一轮。

        `feelings` 是结构化事件（见模块说明）；给了才会改情绪与关系。
        `probes` 是"想问对方的方向"，只在深度分析时用来拉共同回忆。
        """
        budget = budget or TurnBudget()
        ctx = dict(context or {})
        actor = actor_id or account_id
        turn = Turn()

        # ---- 1. 观察：这句话跟画像对得上吗
        #
        # 放在记忆写入之前。理由：观察只看这句话，不需要账本；
        # 而它一旦发现问题，后面的检索和判断都要跟着变。
        profile = self.profiles.ensure(scope_id, account_id)
        turn.alerts = self._observe(scope_id, account_id, text)

        # ---- 2. 记忆写入
        self._seq += 1
        result = self.ingest.ingest(
            scope_id, f"turn_{self.engine.now()}_{self._seq}", actor,
            source_kind, text, context=ctx,
        )
        turn.trace_id = result.trace_id
        turn.outcomes = list(result.outcomes)
        turn.clarifications = list(result.clarifications)

        # 自动重要性：只对用户陈述打分，注入/系统消息不参与。
        # 它只改「淡得慢一点」，不钉锚点；误判的代价是多留一阵。
        if source_kind == "user_statement":
            try:
                auto_importance.apply(self.engine, scope_id, result.trace_id, text)
            except Exception:  # noqa: BLE001 —— 打分失败不该影响这一轮记忆
                pass

        if self.auto_confirm and result.clarifications:
            # 演示/压测专用：替用户点一次头。不点的话，那几句会一直挂着，
            # 画像就只是半截——看起来像"系统没记住"，其实是"没收到确认"。
            for cl in result.clarifications:
                try:
                    self.ingest.confirm(cl["request_id"], actor)
                except Exception:  # noqa: BLE001
                    pass

        # 把刚提交的事实同步进画像（幂等）。
        #
        # 画像从 `states` 派生，但**没人触发就不会派生**——
        # 少了这一步，账上有"不吃辣"，卡片上还是"还不了解这个人"，
        # 主 AI 拿着空卡去回答"今天吃什么"，自然只能反问。
        # 放在这里而不是 `render()` 里：render 是读操作，不该有副作用。
        # v0.52：换人嫌疑要能自己解除。
        #
        # 宿主（DSH）里没有人会调用 `profiles.evaluate()`，于是一句「以后叫我老王」
        # 就让画像永久处于 suspect：之后所有新标签进暂存区、卡片上再也看不到，
        # 还每轮挂着「别把他当同一个人」。两条解除路径：
        #   1. 这一轮用户明确改了称呼并已入账 → 改称呼本身就是解释，判同一个人；
        #   2. 嫌疑超过 SUSPECT_TTL 秒没人处理 → 自动解除（线索留在库里可查）。
        self._settle_suspicion(scope_id, account_id, turn, result)

        # v0.52：只派生这一轮动过的状态；每个 scope 在进程里第一次、
        # 以及每 50 轮做一次全量对账兜底（澄清确认等间接改动不会漏太久）。
        self._derive_turns[scope_id] = self._derive_turns.get(scope_id, 0) + 1
        if self._derive_turns[scope_id] % 50 == 1:
            self.profiles.derive_from_memory(scope_id, account_id)
        else:
            touched = sorted({o.event.state_key for o in result.outcomes
                              if getattr(o, "event", None) is not None})
            self.profiles.derive_from_memory(scope_id, account_id,
                                             state_keys=touched)

        # ---- 3. 异常才做深度分析（方案 9 的成本控制）
        if turn.alerts and budget.deep_analysis:
            hint_dicts = [
                {"kind": a["kind"], "evidence_trace_id": a.get("evidence_trace_id")}
                for a in turn.alerts
            ]
            turn.assessment = self.judge.assess(
                scope_id, account_id,
                hints=hint_dicts,
                probes=probes,
                budget=RecallBudget(max_rounds=1, max_candidates=12,
                                    max_items=budget.max_deep_recall,
                                    max_chars=800),
            )
        elif turn.alerts:
            # 预算关掉了深度分析。**必须说出来**——
            # 静默降级会让调用方以为"没有异常"，那是两回事。
            turn.warnings.append("有异常线索，但本轮预算不允许深度分析")

        # ---- 4. 按需检索。排除当前这句——它不是"旧事"。
        turn.recalled = self._recall(scope_id, text, budget,
                                     exclude=turn.trace_id)

        # ---- 5. 情绪与关系（只在调用方给了结构化事件时才动）
        if feelings and self.mind is not None:
            self._apply_feelings(profile, feelings, turn)

        # ---- 6. 组织材料（先把锚点取出来——它不依赖检索）
        turn.anchored = self._anchored(scope_id)
        turn.context = self._compose(
            scope_id, account_id, profile, turn, budget, session=session,
        )
        turn.budget_used = {
            "deep_analysis": turn.escalated,
            "recalled": len(turn.recalled),
            "context_chars": len(turn.context),
        }
        return turn

    def compose_context(
        self,
        scope_id: str,
        account_id: str,
        text: str = "",
        *,
        budget: TurnBudget | None = None,
        session: str | None = None,
    ) -> str:
        """只读：给材料，**一个字都不写**。

        宿主有时需要材料但没有新消息——开场白、切换会话、用户手动刷新。
        那种时候不能调 `handle()`：它会记一笔账，而用户什么都没说。

        用它，不要直接调 `_compose`：那是私方法，签名会随内部变化。
        """
        budget = budget or TurnBudget()
        profile = self.profiles.ensure(scope_id, account_id)
        turn = Turn()
        turn.anchored = self._anchored(scope_id)
        if text:
            turn.recalled = self._recall(scope_id, text, budget)
        # mark=False：读一次材料不等于问过用户。只读接口不该有副作用。
        return self._compose(scope_id, account_id, profile, turn, budget,
                             mark=False, session=session)

    # ---------------------------------------------------------- 各步
    def _observe(self, scope_id: str, account_id: str,
                 text: str) -> list[dict]:
        """轻量观察。只出线索，不下结论（4.5）。"""
        alert = self.profiles.watch(scope_id, account_id, text)
        if alert is None:
            return []
        # 顺带看一眼还有没有别的 open 线索——
        # 判断层需要全部线索，不是只有这一条。
        return self.profiles.open_alerts(scope_id, account_id)

    def _recall(self, scope_id: str, text: str, budget: TurnBudget,
                exclude: str = "") -> list[dict]:
        """常规检索。**用这句话本身当线索**，不额外猜。

        `exclude` 是当前这句的 trace：它不算"旧事"。
        不过滤的话，材料里会出现「相关的旧事：我是他同事，帮我看看他的账」——
        主 AI 会把它当成一件往事，而它其实是刚说的话。
        """
        if budget.max_recall <= 0:
            return []
        # 回声：当前这句话本身永远不算"旧事"。
        # `/turn` 靠 exclude（trace_id）挡，`/context` 没有 trace_id 可挡——
        # 它只拿得到文本，所以这里按内容再挡一次。两条路都走这个函数，
        # 挡在源头比在两处各挡一次可靠。
        echo = _echo_key(text)
        result = self.recall.pull(
            scope_id, text, limit=budget.max_recall + 1,
            budget=RecallBudget(max_rounds=1,
                                max_candidates=budget.max_recall * 4,
                                max_items=budget.max_recall + 1,
                                max_chars=budget.max_context_chars // 2),
            exclude=exclude,
        )
        related = [
            {
                "content": t["content"],
                "trace_id": t["trace_id"],
                "priority": t.get("priority"),
                "importance": t.get("importance"),
                "anchor": t.get("anchor", False),
                "kind": "related",
            }
            for t in result.traces
            if t["trace_id"] != exclude and _echo_key(t["content"]) != echo
        ][: budget.max_recall]
        if related:
            return related

        # 检索不到就退化成"最近说过的事"，并且**标明它不是相关的**。
        #
        # 为什么不干脆留空：材料里少一段，主 AI 会以为"这个人没说过别的话"，
        # 而实际是"这句 query 没匹配上"。那是两种完全不同的信息。
        # 也不冒充成"相关的旧事"——最近说的和相关的经常不是一回事。
        return self._recent(scope_id, exclude, budget.max_recall, echo)

    def _recent(self, scope_id: str, exclude: str,
                limit: int, echo: str = "") -> list[dict]:
        """最近说过的话。**只用来兜底，标签必须是 recent。**

        `answer-%` 是澄清回答（"确认"/"是这条"），不是用户说过的事。
        把它当材料喂进去，主 AI 会以为"他最近说了句确认"——
        那不是事实，是协议的一部分。
        """
        rows = self.engine._conn().execute(
            "SELECT trace_id, content FROM traces"
            " WHERE scope_id=? AND trace_id!=? AND erased_at IS NULL"
            " AND source_ref NOT LIKE 'answer-%'"
            " ORDER BY recorded_at DESC LIMIT ?",
            (scope_id, exclude, max(limit + 4, limit)),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            if echo and _echo_key(r["content"]) == echo:
                continue
            out.append({"content": r["content"], "trace_id": r["trace_id"],
                        "priority": None, "anchor": False, "kind": "recent"})
            if len(out) >= limit:
                break
        return out

    def _apply_feelings(self, profile: dict, feelings: dict,
                        turn: Turn) -> None:
        """按结构化事件改情绪与关系。

        缺字段就报错，不补默认值：
        补一个 `severity=1` 等于替调用方决定了"这事有多严重"，
        而那个决定不该由这里做。
        """
        need = ("summary", "severity", "anger", "trust_delta", "reason")
        missing = [k for k in need if k not in feelings]
        if missing:
            turn.warnings.append(f"感受事件缺少字段: {', '.join(missing)}")
            return
        self.mind.offended(
            profile,
            summary=feelings["summary"],
            severity=float(feelings["severity"]),
            anger=float(feelings["anger"]),
            trust_delta=float(feelings["trust_delta"]),
            reason=feelings["reason"],
            evidence_trace_id=turn.trace_id or None,
        )

    def _anchored(self, scope_id: str, limit: int = 5) -> list[dict]:
        """被用户要求"记住"的话。

        **这一段必须无条件进材料。** 锚点在库里的意思是"不随时间淡去"，
        但如果它没进上下文，主 AI 就看不到它——那和忘了没区别。

        上一版就是这样：锚点确实设了，`priority` 恒为 1.0，
        可它出现在材料里只是因为"还很新"（排在最近几句话里）。
        聊到第 20 轮，它掉出最近列表，从此消失。

        锚住却看不见，等于没锚。这一段的成本是几条短句，
        换来的是"用户明确要求的事不会丢"。
        """
        from .decay import Lifecycle

        out: list[dict] = []
        for item in Lifecycle(self.engine).anchored(scope_id):
            trace = self.engine.get_trace(item["ref_id"])
            if not trace:
                continue
            text = trace["content"]
            # 「记住，」这个前缀是指令，念给主 AI 听没有意义。
            body = detect.remember_body(text) or text
            out.append({
                "content": body,
                "raw": text,
                "trace_id": item["ref_id"],
                "priority": item.get("priority"),
            })
            if len(out) >= limit:
                break
        return out

    SUSPECT_TTL = 1800

    def _settle_suspicion(self, scope_id: str, account_id: str, turn: Turn,
                          result) -> None:
        from datetime import datetime, timezone
        profile = self.profiles.get(scope_id, account_id)
        if not profile or profile.get("status") != "suspect":
            return
        alerts = self.profiles.open_alerts(scope_id, account_id)
        renamed = any(
            o.interpretation.relation == "nickname" and o.decision.outcome == "commit"
            for o in result.outcomes
        )
        only_address = bool(alerts) and all(a["kind"] == "address_shift" for a in alerts)
        expired = False
        try:
            since = datetime.fromisoformat(str(profile.get("updated_at")))
            now = datetime.fromisoformat(self.engine.now())
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            expired = (now - since).total_seconds() > self.SUSPECT_TTL
        except (TypeError, ValueError):
            expired = False
        if (renamed and only_address) or expired or not alerts:
            reason = "用户明确改了称呼" if renamed else "嫌疑超时自动解除"
            for a in alerts:
                self.profiles.evaluate(a["alert_id"], "same_person", [reason],
                                       actor_id="system")
            self.profiles.restore(scope_id, account_id)
            turn.alerts = []

    def _debt_lines(self, scope_id: str, account_id: str, limit: int = 6) -> list[str]:
        import json as _json
        conn = self.engine._conn()
        rows = conn.execute(
            "SELECT state_key, value_json FROM states WHERE scope_id=?"
            " AND status='known' AND state_key LIKE ? LIMIT 50",
            (scope_id, f"{scope_id}|%|debt|%"),
        ).fetchall()
        lines: list[str] = []
        for r in rows:
            parts = r["state_key"].split("|")
            if len(parts) < 4:
                continue
            debtor, creditor = parts[1], parts[3]
            try:
                value = _json.loads(r["value_json"]) if r["value_json"] else None
            except ValueError:
                value = None
            if not isinstance(value, (int, float)) or value == 0:
                continue
            ev = conn.execute(
                "SELECT payload_json FROM events WHERE state_key=? AND void=0"
                " AND payload_json LIKE '%\"unit\":\"%'"
                " ORDER BY sequence_no DESC LIMIT 1",
                (r["state_key"],),
            ).fetchone()
            unit = ""
            if ev and ev["payload_json"]:
                try:
                    unit = str(_json.loads(ev["payload_json"]).get("unit") or "")
                except (ValueError, AttributeError):
                    unit = ""
            who = {"assistant": "你（助手）", "peer": "你（助手）"}.get(creditor, creditor)
            amount = f"{value:g}{unit}"
            if debtor == account_id:
                lines.append(f"- 他欠{who} {amount}")
            else:
                lines.append(f"- {debtor} 欠{who} {amount}")
            if len(lines) >= limit:
                break
        return lines

    def _clarification_quote(self, row: dict, limit: int = 40) -> str:
        """澄清对应的那句原话（截短）。找不到就返回空串。"""
        import json as _json
        try:
            ids = _json.loads(row.get("interpretation_ids_json") or "[]")
        except ValueError:
            return ""
        if not ids:
            return ""
        got = self.engine._conn().execute(
            "SELECT t.content FROM interpretations i JOIN traces t"
            " ON t.trace_id = i.trace_id WHERE i.interpretation_id=? LIMIT 1",
            (ids[0],),
        ).fetchone()
        if not got:
            return ""
        text = " ".join(str(got["content"]).split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    # ---------------------------------------------------------- 组装
    def _compose(self, scope_id: str, account_id: str, profile: dict,
                 turn: Turn, budget: TurnBudget, *, mark: bool = True,
                 session: str | None = None) -> str:
        """把材料拼成给主 AI 的一段话。

        分四段，因为这个顺序就是主 AI 该有的思考顺序：
        先知道对面是谁，再知道自己什么状态，再看相关的旧事，
        最后才是"有没有要留意的"。
        """
        blocks: list[str] = []

        emotions = self.engine._conn().execute(
            "SELECT name,intensity FROM ai_emotion_settings WHERE scope_id=? ORDER BY name", (scope_id,)
        ).fetchall()
        if emotions:
            lines = "、".join(f"{row['name']} {row['intensity']}/100" for row in emotions)
            blocks.append("## 用户设置的 AI 情绪倾向\n" + lines + "\n请把这些数值作为表达风格参考。")

        # 1. 这个人
        card = self.profiles.render(scope_id, account_id)
        blocks.append("## 关于这个人\n" + card)

        # 2. 用户要求记住的事。**排在画像之后、状态之前**——
        # 它比"我现在什么心情"更该先被看见。
        if turn.anchored:
            items = "\n".join(f"- {t['content']}" for t in turn.anchored)
            blocks.append(
                "## 重要的事（他专门说过要记住，不会随时间淡去）\n" + items)

        # 2.5 账和约定（v0.52）：已经入账的欠款直接给出余额。
        # 以前账只在「相关的旧事」里以原话出现，余额要模型自己算，
        # 还经常因为检索没命中而整段消失。
        debts = self._debt_lines(scope_id, account_id)
        if debts:
            blocks.append("## 账目（已确认）\n" + "\n".join(debts))

        # 3. 我现在的状态
        if self.mind is not None:
            blocks.append("## 我现在的状态\n" + self.mind.render(profile))

        # 4. 相关的旧事 / 最近说过的话
        #
        # 两段分开写。混在一起，主 AI 会把"最近说的"当成"与本题相关的"，
        # 于是拿一句无关的旧话去解释眼前的事。
        if turn.recalled:
            related = [t for t in turn.recalled if t.get("kind") == "related"]
            recent = [t for t in turn.recalled if t.get("kind") == "recent"]
            if related:
                def _tag(t: dict) -> str:
                    if t.get("anchor"):
                        return "（重点）"
                    if float(t.get("importance") or 0) >= 3.0:
                        return "（重要）"
                    return ""

                items = "\n".join(
                    f"- {t['content']}{_tag(t)}" for t in related
                )
                blocks.append("## 相关的旧事\n" + items)
            if recent:
                items = "\n".join(f"- {t['content']}" for t in recent)
                blocks.append("## 最近说过的话（不是检索命中的，仅供参考）\n"
                              + items)

        # 5. 还没确定的事
        #
        # 和「要留意的」分开：那段说的是**这一轮**的问题，
        # 这段说的是**跨轮悬着**的问题。
        # 混在一起，模型会以为"这句话需要问"，而实际是"三天前那句还没定"。
        #
        # 宿主形态下这一步是必需的：DSH 没有"点确认"的交互，
        # 澄清开出来就永远悬着——问过却不再提，等于没问。
        #
        # ## 但必须熔断
        #
        # 每轮都念一遍「有件事还没确定」，用户会被同一句话连问五遍。
        # 那不是澄清，是审问。三道闸：
        #
        #   1. 每条只念一次        —— 念过就标 asked，不再进材料
        #   2. 一轮最多提几条      —— budget.max_questions
        #   3. 累计问够就不再新开  —— budget.max_asked_total
        #
        # 熔断之后材料里**只剩一句总数**，不说"去问一句"。
        # 用户主动提起来时，那些记录还在库里，不会丢。
        unasked = self.ingest.unasked_clarifications(scope_id)
        asked_total = self.ingest.asked_count(scope_id, session)
        if unasked and asked_total < budget.max_asked_total:
            shown = unasked[: budget.max_questions]
            # v0.52：带上原话。只写「缺一个关键信息」，主模型不知道是哪句话，
            # 也就无从问起。
            lines = []
            for p in shown:
                label = _CLARIFICATION_LABELS.get(p["kind"], "有一处还没说清楚")
                quote = self._clarification_quote(p)
                lines.append(f"- 「{quote}」：{label}" if quote else "- " + label)
            # 「当前这句」而不是「下面这句」：这段文字进的是 systemPrompt，
            # 用户那句话在另一条消息里，不在这段下面。
            hint = ("\n（如果用户当前这句其实已经说清楚了，就当它确认了；"
                    "否则**最多挑一件**顺带问一句，别追问）")
            tag = "" if len(unasked) <= len(shown) else f"，另有 {len(unasked) - len(shown)} 件"
            blocks.append(
                "## 还没确定的事" + tag + "\n" + "\n".join(lines) + hint
            )
            # 只读调用（`/context`）不标记：读一次材料不等于问过用户。
            if mark:
                self.ingest.mark_asked([p["request_id"] for p in shown],
                                       session)
        elif unasked:
            # 熔断：问够了。只报个数，不再引导追问。
            blocks.append(
                f"## 还没确定的事\n- 有 {len(unasked)} 处还没确认"
                "（已经问过几次了，别主动追问；他主动提起来再说）"
            )

        # 6. 要留意的
        notes: list[str] = []
        if turn.assessment is not None:
            notes.append(turn.assessment.render())
        if turn.clarifications:
            notes.append(f"这句话还没确认清楚，需要问一句"
                         f"（{len(turn.clarifications)} 处）")
        if turn.warnings:
            notes.extend(turn.warnings)
        if notes:
            blocks.append("## 要留意的\n" + "\n".join(notes))

        text = "\n\n".join(blocks)
        if len(text) > budget.max_context_chars:
            # 截断要留痕，不能悄悄切掉——主 AI 不知道被截了，
            # 就会以为自己看到的是全部。
            text = text[: budget.max_context_chars]
            turn.warnings.append(
                f"材料超过 {budget.max_context_chars} 字，已截断",
            )
        return text
