"""编码器：把一句话变成候选解释。

设计文档 6.4 / 8.2：
    编码器只产出候选，可多个，可 unclear。
    模型输出一律是候选产物，必须持久化、校验后才能用于提交。

本模块提供三种编码器：
    RuleEncoder    词表 + 正则，用于跑通流程与测试闸门（不追求准确率）
    ReplayEncoder  从标注样本回放，用于可复现的 T02 测试
    LLMEncoder     调用 OpenAI 兼容接口（可选，需要使用者自己的 key）
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

# 数字解析与「未来 / 求确认 / 否认」线索都搬到了 detect.py。
# 那边不依赖本模块，所以 ingest 和闸门可以直接用，不会绕回来。
# 这里保留同名导入，是为了 `parse_number` 这个公开名字不消失
# （`__init__.py` 导出过它，删掉会破坏既有调用方）。
from . import detect
from .detect import (  # noqa: F401
    QUANTIFIERS,
    confirmation_cue,
    future_cue,
    has_amount,
    parse_number,
    trade_verb,
    quantity_count,
    quantity_cue,
    time_has_arrived,
)

# 模态线索。只用于触发更严格处理，不用于证明是实际陈述（8.4）。
SIGNAL_WORDS = {
    "conditional": ("要是", "如果", "假如", "倘若", "万一"),
    "quoted": ("他说", "她说", "听说", "据说", "转述", "有人讲"),
    "denied": ("哪有", "才没", "才不", "没欠", "不欠", "什么时候欠"),
    "joking": ("（笑）", "(笑)", "哈哈", "开玩笑", "逗你", "骗你的", "嘿嘿"),
    "planned": ("明天", "下次", "回头", "待会", "以后", "等我"),
    "hypothetical": ("假设", "假如", "比如说", "想象一下"),
    "ironic": ("呢～", "可真", "真是", "好一个"),
}

DEFAULT_RELATIONS = ("debt", "preference", "nickname")

# 语义操作：这句话在说一件什么事。
#
# 注意和 events.operation 区分：那边是 append / update / retract / conflict，
# 描述"事件怎么进流"；这里描述"这句话什么意思"。
#
# set 还是 delta 不在这里定 —— 那取决于当前有没有既有状态，
# 是内核的信息，编码器看不到（见 ingest._first_set_rule）。
_SEMANTIC_OPS = (
    "incur", "repay", "waive", "set_total", "correct", "retract",
    # promise：这句话说的是**以后**要做的事，不是已经发生的事。
    #
    # 它不是「偿还」，因为它现在不改变任何余额；
    # 也不是「noop」，因为它留下了一个**可以被追讨的对象**。
    # 少了这一格，「下个月我再还你两袋」只有两种归宿：
    # 要么当成已经还了（余额提前扣掉，XM-22），要么当成闲聊（承诺消失）。
    # 两者都不对，因为承诺既没发生，也没消失——它挂在那里。
    "promise",
)
SEMANTIC_OPS = _SEMANTIC_OPS


@dataclass
class Interpretation:
    interpretation_id: str
    trace_id: str
    relation: str
    modality: str
    polarity: str                       # affirm / negate
    slots: dict[str, Any]
    signals: dict[str, Any] = field(default_factory=dict)
    evidence_span: tuple[int, int] | None = None
    confidence: float | None = None      # 仅作辅助信号，不作判定依据（6.3）
    # 这句话是什么意思，而不是"该怎么写库"。
    #
    #   incur      新增一笔欠款      「我又欠你 5 碗」
    #   repay      偿还              「还你 5 碗」
    #   waive      免除              「免我 5 碗」
    #   set_total  总额就是 X        「就是三十碗」「凑个整」
    #   correct    上一句说错了      「不是五，是七」
    #   retract    上一句不算        「那个不算」
    #   noop       与状态无关        「今天天气不错」
    #
    # 为什么不让编码器直接输出 set/delta：
    # set 还是 delta 取决于**当前有没有既有状态**，那是内核的信息，编码器看不到。
    # 「我欠你 40 碗」在空账上是 set，在已有 30 碗时是 +40。
    # 让一个看不到状态的组件去做这个判断，它只能猜。
    semantic_op: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "interpretation_id": self.interpretation_id,
            "trace_id": self.trace_id,
            "relation": self.relation,
            "modality": self.modality,
            "polarity": self.polarity,
            "slots": self.slots,
            "signals": self.signals,
            "evidence_span": list(self.evidence_span) if self.evidence_span else None,
            "confidence": self.confidence,
            "semantic_op": self.semantic_op,
        }


class Encoder(Protocol):
    def encode(self, trace_id: str, content: str, context: dict[str, Any]) -> list[Interpretation]:
        ...


# ============================================================ 规则编码器
class RuleEncoder:
    """词表 + 正则的弱编码器。

    存在的意义不是准确，是让整条写路径可以在没有模型的情况下跑通、
    并且让闸门的判定逻辑可以被稳定测试。
    """

    def __init__(self, relations: Sequence[str] = DEFAULT_RELATIONS):
        self.relations = tuple(relations)

    def encode(self, trace_id: str, content: str, context: dict[str, Any]) -> list[Interpretation]:
        signals: dict[str, Any] = {}
        modality = "asserted"

        # 「记住」是**指令**，不是内容（见 detect.remember_body）。
        #
        # 不剥的话：「记住，四月十二号那天项目上线，我们熬到凌晨四点」
        # 里"记"被当成记账动作、"四"被当成数量，整句判成一笔 4 袋的债——
        # 余额从 2 变成 6，状态还是 known，账面看不出任何异常。
        #
        # 用户说"这句很重要"，系统听到"再欠四袋"。
        body = detect.remember_body(content)
        if body:
            signals["remember_cue"] = detect.remember_cue(content)
            content = body

        # v0.52：提问不是陈述。「你还记得我欠谁钱吗」以前被判成一笔待确认的债。
        # 求确认句（「我是不是欠你 50 来着」）不算提问，走原来的路径。
        if not body and is_question(content) and not confirmation_cue(content):
            return []

        for name, words in SIGNAL_WORDS.items():
            hits = [w for w in words if w in content]
            if name == "planned":
                # 「明天了，给你五匹」里的「明天」是"时间到了"，不是计划。
                # 这张词表和 future_cue 是两条入口，判据必须是同一个，
                # 否则修了一处、另一处照样把已发生的还款降级成承诺。
                hits = [w for w in hits if not time_has_arrived(content, w)]
            if hits:
                signals[name] = hits
                if modality == "asserted":
                    modality = name

        if context.get("in_world"):
            modality = "in_world"
        elif modality == "asserted" and any(w in content for w in ("能不能", "给我", "求你", "帮我")):
            # 祈使/请求不是陈述。少了这一步，「给我免掉个零头」会被当成
            # 假设句（hypothetical）判 record_only，用户明确要求的事反而不记。
            modality = "request"

        # 表层线索（detect.py）。规则版和 LLM 版都要过这一道：
        # 模型漏掉的「下个月」「是不是」，这里补上。
        # 只补信号，不改关系——线索能收紧处理，不能替人做判断。
        due = future_cue(content)
        if due:
            signals["future_tense"] = due
            if modality == "asserted":
                modality = "planned"
        conf = confirmation_cue(content)
        if conf:
            signals["seeking_agreement"] = conf

        # 「开玩笑的。」——这不是又一次玩笑，是在**撤回前一句**（方案 3.1）。
        #
        # 必须在下面判定 relation / op 之前认出来：
        # 它是"我说过的那句不算"，不是"我现在说了什么"。
        #
        # 认出之后做两件事：
        #   1. 把 modality 从 joking 改回 asserted ——
        #      这句话本身是**陈述**（陈述"刚才那句是玩笑"），不是玩笑。
        #      不改的话闸门会把它当玩笑挡掉，撤回永远落不了地。
        #   2. 记一个信号，让 op 走 retract。
        joke_retract = detect.joke_retraction(content)
        if joke_retract:
            modality = "asserted"
            # 把 joking 摘掉：这句话的**字面**含"开玩笑"，
            # 但它不是在开玩笑——它是在说"刚才那句是玩笑"。
            # 不摘的话闸门会用 risk_signal:joking 把它拦成 pending，
            # 撤回永远落不了地（试过，卡在这儿）。
            signals.pop("joking", None)
            signals["joke_retraction"] = joke_retract
            # 撤回的对象是上一句，所以关系必须继承上文——
            # 这句话里没有任何领域词，靠关键词判关系必然落空。
            context = {**context, "joke_retraction": joke_retract}

        polarity = "negate" if signals.get("denied") else "affirm"

        # 这个数**没有归属**吗？「我今年二十三岁」里的二十三不是债。
        #
        # 为什么要在这里单独判：下面有一条"没证据就继承上文关系"的兜底路径。
        # 于是「我今年二十三岁」跟在一句欠款后面，就被当成"又欠了二十三袋"，
        # 余额凭空涨 23，账面还完全正常（status=known）——
        # 存下来了，但存的是错的。
        orphan = detect.orphan_amount(content)

        relation = context.get("relation") or self._guess_relation(
            content, context, orphan=orphan
        )

        if relation is None:
            # 这句话里的数**没有归属**——「我今年二十三岁」的二十三不是债。
            #
            # 不产出解释，也不追问。它跟账无关，记一笔旁注就完了。
            #
            # 注意这里**不能**回退到某个关系：回退到 debt 就会凭空记一笔债，
            # 回退到 preference 就会追问「你喜欢什么」——两条都是打扰，
            # 前者更糟（存错了，而且账面看不出异常）。
            return []

        # 这个关系是**猜出来的**吗？猜的和有证据的，待遇必须不同。
        #
        # preference 是关系判定的兜底值：「今天风真大啊。」「你把窗关上吧」
        # 没有任何领域词，于是落到兜底值上。带着这个标记，
        # 闸门才知道「这不是在说他喜欢什么，是在说废话」，
        # 就不该追问「你喜欢什么」——那是最直接的打扰。
        #
        # 有明确证据的偏好句（「我最喜欢喝奶茶」）不打这个标记，
        # 走 preference 自己的槽位与准入流程。
        if (relation == "preference"
                and not context.get("relation")
                and not _has_preference_evidence(content)):
            signals["relation_guessed"] = True

        # 「你以后就叫我小鲸」「我以后不喝咖啡了」——
        # 这里的「以后」说的是**生效范围**（从今往后），不是"未来某刻会发生的事"。
        # 把它当 planned，设定类关系就永远入不了库：
        # nickname 只允许 asserted，于是一句自我介绍被判 record_only。
        #
        # 只豁免设定类关系。债务仍然要管「下个月还」和「已经还了」的区别——
        # 那是钱，含糊不得。
        if (modality == "planned"
                and relation in ("nickname", "preference")
                and signals.get("future_tense") in ("以后", "今后", "往后")):
            modality = "asserted"
            # `planned` 这个信号也要摘：闸门的 risk_signal:planned 会把它
            # 拦成 pending，modality 改了但信号还在，等于换个理由拦住。
            signals.pop("planned", None)
            signals["setting_scope"] = signals.pop("future_tense")

        if signals.get("joke_retraction"):
            # 追认玩笑是一次撤回，不是一笔新账。
            op = "retract"
        else:
            op = self._guess_semantic_op(content, relation)

        # 债**已经成立**（incur），未来词修饰的是还款计划，不是这笔债的模态。
        #
        # 「我欠你两袋米，回头还你」——两袋此刻就在账上，
        # 「回头还」是下一件事。整句按 planned 处理，这笔债一条都记不上。
        if modality == "planned" and op == "incur":
            modality = "asserted"
            signals.pop("planned", None)

        slots = self._extract_slots(content, context, op, relation)
        if slots.pop("_ambiguous_preference", False):
            return []
        # 口语里省略喜好对象很常见；保留原话，不猜对象也不生成无意义的澄清。
        if (relation == 'preference' and not slots.get('object') and not context.get('relation')
                and not re.search(r'我\s*(?:喜欢|讨厌|偏爱|偏好|爱吃|爱喝|不喜欢)', content)):
            return []
        missing = [k for k in ("quantity", "unit") if slots.get(k) is None]
        if missing:
            signals["missing_slots"] = missing

        return [Interpretation(
            interpretation_id=f"in_{trace_id[-8:]}_{relation}",
            trace_id=trace_id,
            relation=relation,
            modality=modality,
            polarity=polarity,
            slots=slots,
            signals=signals,
            confidence=0.5 if signals else 0.8,
            semantic_op=op,
        )]

    def _guess_semantic_op(self, content: str, relation: str) -> str | None:
        """规则版只认最扎眼的几个说法。

        重要：这**不是**在解决语义，是把三条非增量路径（correct / retract /
        set_total）也纳入可测试范围，否则新代码路径永远没有测试覆盖。
        """
        # 非金额关系（偏好/称呼/闲聊…）没有"增减"语义，semantic_op 必须是空。
        # 以前这里返回 set_total，等于给一句"我不吃辣"记了一次"总额变更"；
        # 基准里 preference/nickname 的意图命中因此是 0%（见 BENCHMARK.md）。
        if relation != "debt":
            return None
        # 改口优先于时间： 「我刚才说错了，下个月还三碗」是一次**修正**，
        # 不是又许一个新愿。顺序反了会留下两条并存的承诺（演示里一眼看到）。
        # 「不对」是中文里最常见的改口引子（「不对，我一开始说的是五十」）。
        # 只收「不对」，不收「不是」——「不是本地人」这种普通否定会被误伤。
        # retract 排在 correct 前面：「不对，刚才那袋不算免」两个词都有，
        # 但"不算"比"不对"更具体——它明确说的是"这条撤销"。
        # 顺序反了会判成 correct，却因为没有新值而什么都不做，
        # 免除就撤不掉了（c01 第 7 句一直卡在这里）。
        if ("不算" in content or "不作数" in content or "划掉" in content
                or "不免" in content or "别免" in content):
            return "retract"
        if any(w in content for w in ("记错", "说错", "不对")):
            return "correct"
        # 「不是 30，是 20 碗」：两个数量 + 不是…是…，是在改上一句的数。
        if "不是" in content and quantity_count(content) >= 2:
            return "correct"
        # 两个数量、又没有任何"新增"动词 → 在改一笔旧账。
        # 「刚才那三尺是两尺」：三改成两。少了这一条会落进 incur，
        # 变成"又欠两尺"（c09/d02 第 7 句一直错在这里）。
        if quantity_count(content) >= 2 and not any(
            w in content for w in ("欠", "又", "再", "还", "给", "补", "免")
        ):
            return "correct"
        # 未来时间词优先于增量判断：说了「下个月还」就不是「已经还」。
        #
        # 但「欠」不在这个名单里：它是**已经发生**的事。
        # 「我欠你两袋米，回头还你」——债务此刻成立，"回头还"是还款计划。
        # 把整句判成承诺，这两袋就一条都记不上（实测过：
        # 端到端对话里三句关键事实全被这条规则挡在门外）。
        if (future_cue(content) and "欠" not in content
                and any(w in content for w in ("还", "给", "补"))):
            return "promise"
        # 「我还剩两百八」「还差三百」——这是**余额陈述**，不是又欠一笔。
        #
        # XM-59：判成 incur 会把余额再累加一遍。实测：
        #   我欠你三百二   → 320
        #   我还剩两百八   → 600   ← 用户说的是"余额只剩 280"
        # 「剩」这个字只可能指余额——"我又剩你两百八"不通。
        #
        # **不收「还欠」**：「我又欠你三袋」和「我还欠你四十碗」都是新账，
        # 两者靠字面分不开，动了会把真账吞掉。宁可少认，不可乱认。
        if (any(w in content for w in ("还剩", "还差", "剩下", "余下"))
                and detect.has_amount(content)):
            return "set_total"
        # 「一共」「总共」说的是**总额**，不是又欠一笔。
        # 「我一共就欠你四十」在已有 60 的账上是"改成 40"，不是"+40"。
        # 但条件句里的「就欠」不是总额陈述：「要是你赢了，我就欠你 40 碗」。
        if (
            any(w in content for w in ("一共", "总共", "就欠"))
            and "欠" in content
            and not any(w in content for w in ("要是", "如果", "假如"))
        ):
            return "set_total"
        # 「其实只欠 8 碗」= 总额就是 8，不是又欠 8。
        if "只欠" in content and any(w in content for w in ("其实", "实际上", "本来就")):
            return "set_total"
        if "凑个整" in content or "凑整" in content:
            return "set_total"
        # 「不用还了」「这几袋不用还了」：这是免除，不是偿还。
        # 必须在 repay 的「还」之前判，否则会把"免债"记成"还债"。
        if "不用还" in content or "不必还" in content or "不用给了" in content:
            return "waive"
        # 「那 6 碗算了」：债务语境里的"算了"是免掉，不是还掉。
        if "算了" in content:
            return "waive"
        # 「总数 30 碗」「按 4 袋算」：把总额改成这个数。
        if "总数" in content:
            return "set_total"
        if re.search(r"按\s*(?:\d+|[零一二两三四五六七八九十百千万亿]+)\s*[碗袋本个块条]?\s*算", content):
            return "set_total"
        # 「补你 2 袋」：补上欠的东西 = 还款。
        if "补" in content and any(w in content for w in ("你", "他", "她")):
            return "repay"
        # 「把债务清零」「清空」：这是免除/作废，不是又欠一笔。
        if "免" in content or "清零" in content or "清空" in content:
            return "waive"
        # 「0 碗，我不欠了」「还清了」：结算类，不是新增欠款。
        if any(w in content for w in ("不欠了", "还清了", "两清")):
            return "repay"
        # 顺序必须是「欠 → 还」。真实对话里「我还欠你四十碗」的「还」是副词，
        # 不是动词——先判「还」会把它当成还款，第一句就把余额算成负数（踩过）。
        if "欠" in content:
            return "incur"
        # 「我借了你一把扳手」——借进来的东西要还，语义上就是欠。
        # 注意区分方向：「借给你」是借出去，那是对方的账，不能记成我的欠款。
        if ("借了" in content or "借你的" in content) and "借给" not in content:
            return "incur"
        # 「还是」里的「还」不是还款动词——它是"仍然"，副词。
        # 「那就还是四十袋吧」说的是"总额没变"，不是"我又还了四十袋"。
        # 把它当还款，账上凭空少四十（demo d02：主人账被清成 0）。
        if detect.has_repay_verb(content):
            return "repay"
        # 「今天给你五个」——在债务语境里，"给债主东西"就是还款。
        # 少了这一条，它会落进 incur，余额越还越多（c05 规则版 1/10、
        # holdout_v5/t02 第 2 句都是这个）。
        if "给" in content:
            return "repay"
        return "incur"

    def _guess_relation(
        self, content: str, context: dict[str, Any] | None = None,
        orphan: bool = False,
    ) -> str:
        # 「那个不算」「我刚才说错了」「开玩笑的」不含任何领域词，
        # 它们指向的是**上一条**。
        # 靠关键词判必然落空，所以先看上下文给的 last_relation。
        #
        # 追认玩笑在这里必须**先**接住：它没有任何领域词，
        # 落到兜底值就成了「偏好」，撤回就会撤到别的东西上。
        if (context or {}).get("joke_retraction"):
            last = (context or {}).get("last_relation")
            if last:
                return last
        # 交易动词和数量须相邻；“记日程”“给朋友写卡片”不应与编号拼成欠账。
        plain = strip_time_words(content)
        transaction = re.search(
            r"(?:给|还|补|赔|退|记上|记账)(?:给)?(?:你|我|他|她)?(?:了|上)?\s*"
            r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百千万]+)\s*"
            r"(?:元|块钱|块|分钱|袋|碗|坛|瓶|斤|公斤|本|件|枚)", plain
        )
        if transaction:
            return "debt"

        # 「刚才那三尺是两尺」——句子里没有一个领域词，却有两个数量。
        # 这是对账句（在改一笔旧账），不是报一笔新账。脱离上下文不可解，
        # 所以继承上一句的关系；判成 preference 会让整笔账记到另一个键上。
        if quantity_count(content) >= 2 and len(re.sub(r"\s+", "", content)) <= 18:
            last = (context or {}).get("last_relation")
            if last:
                return last

        if any(w in content for w in ("不算", "不作数", "记错", "说错", "划掉", "不对")):
            last = (context or {}).get("last_relation")
            if last:
                return last
        # 债务词表要够宽。「赊」「坛」「枚」这些不是新语义，是同一个关系的
        # 另一种说法——缺了它们，「那我再赊一坛」会被判成 preference，
        # 整笔账记到另一个键上（评估里当场看到 2 而不是 3）。
        # v0.52：单字「还」「本」「书」「瓶」在日常中文里太常见
        # （「你还记得」「我喜欢看书」都被判成了债）。现在：
        #   - 明确的债务词直接算；
        #   - 「还」只认还钱类搭配；
        #   - 量词类单字必须和数量一起出现。
        if (detect.has_owed_cue(content) or re.search(r"(?<!流水)账", content)
                or re.search(r"还了(?=你|我|他|她|钱|点|些|[0-9零一二两三四五六七八九十百千万])", content)
                or any(w in content for w in
                       ("借钱", "借了", "借我", "借你", "借他", "借她", "赊",
                        "债", "还钱", "还款", "还清", "还你", "还他", "还她", "两清"))):
            return "debt"
        if (context or {}).get("last_relation") == "debt" and len(plain) <= 12 and has_amount(plain) and any(w in plain for w in
               ("碗", "袋", "坛", "枚", "本", "瓶", "粮", "免")):
            return "debt"
        if (any(w in content for w in ("叫我", "喊我")) and not detect.address_claim(content)
                and not any(w in content for w in ("称呼", "名字", "我叫"))):
            return None
        if (any(w in content for w in ("叫我", "喊我", "称呼"))
                or re.search(r"我(?:的)?名字(?:叫|是)", content)
                or ("我叫" in content and "叫你" not in content)):
            return "nickname"
        # 偏好必须有**正面证据**。
        #
        # 这是关系判定的兜底值，此前"什么都没命中"就落在这里——
        # 于是「那就三十五。」「三捆。」「说定了。」「对了你吃了吗？」
        # 全被判成"在说喜好"，再因为抽不到 object 而要求用户补一句
        # 「你喜欢什么」。G2 实测 21 次空问有 17 次源于这一条。
        #
        # 沉默不是偏好。没有偏好词时宁可继承上文：
        # 「那就三十五」在债务对话里说的是那笔账，不是"爱吃三十五"。
        #
        # 但**孤立的数**（`orphan=True`）不能继承上文：
        # 「我今年二十三岁」里的二十三既不是账也不是偏好，
        # 继承上一句的 debt 关系就等于凭空记一笔债。
        # 这种句子没有可继承的关系，返回 None 让调用方按"跟账无关"处理。
        if orphan:
            return None
        if _has_preference_evidence(content):
            return "preference"
        # 纯寒暄/天气：没有偏好词，也没有领域词，属于"与账无关"。
        # 以前会落到兜底的 preference，虽然闸门会打 relation_guessed 不追问，
        # 但基准里仍算一次误报（「今天天气真不错」被记成"他喜欢天气"）。
        if any(w in content for w in ("天气", "你在吗", "在吗", "吃了吗", "早上好", "晚安")):
            return None
        # v0.52：没有任何领域词时，只有带数量的短句（「那就三十五」「三捆。」）
        # 才继承上文关系；「哈哈」「我女朋友叫小美」不再被当成偏好/账。
        # 这些话照样进情节记忆（原文可检索），只是不生成事实。
        last = (context or {}).get("last_relation")
        if last and len(re.sub(r"\s+", "", plain)) <= 12 and has_amount(plain):
            return last
        return None

    def _extract_slots(
        self, content: str, context: dict[str, Any],
        op: str = "incur", relation: str = "debt",
    ) -> dict[str, Any]:
        slots: dict[str, Any] = {}

        if relation == "preference":
            content = _self_preference_text(content)
            # 「我最喜欢吃烤鱼」：subject=说话人，object=喜欢的东西。
            # 抽不出来就留空，让闸门判缺槽位——**猜比漏更糟**。
            slots["subject"] = context.get("actor_id", "self")
            # 否定形式必须一起认。「我不吃辣，一点辣都不行」是偏好句里
            # 最常见的一类，只认「喜欢/讨厌」会把整句挂成缺 object，
            # 然后永远提交不了——偏好一条都记不上。
            # v0.52：先认否定/转变说法，再认基本说法。
            # 「我其实不讨厌辣了，现在挺能吃辣」以前抽成「讨厌 / 辣了」——极性反了，
            # 而且对象带着「了」，和旧的「不吃 / 辣」不是同一个键，旧值不会被覆盖。
            allergy = re.search(r"对\s*([^\s，。！？,.!?]{1,8}?)\s*过敏", content)
            if allergy:
                slots["value"] = "过敏"
                slots["object"] = allergy.group(1)
                return slots
            m = re.search(
                r"(不讨厌|不怕|不再讨厌|不再怕|挺能吃|很能吃|能吃|开始喜欢|爱上了?|"
                r"不喜欢|不再喜欢|不想吃|最喜欢|最爱|喜欢|偏爱|偏好|讨厌|爱吃|爱喝|怕|习惯|"
                r"不吃|不喝|不爱吃|不爱喝|受不了|不爱|忌口)(?!的)"
                r"\s*([^\s，。！？,.!?]+)",
                content,
            )
            if m:
                value = m.group(1).replace("爱上了", "爱上")
                obj = _clean_pref_object(m.group(2))
                if obj and _reliable_preference_object(value, obj):
                    slots["value"] = value
                    slots["object"] = obj
                elif m.group(2):
                    slots["_ambiguous_preference"] = True
            return slots
        if relation == "nickname":
            # 「你以后就叫我小鲸」——称呼在「叫我」后面。
            # 不抽的话 value 是空的，闸门判缺槽位，一句自我介绍永远落不了库。
            slots["subject"] = context.get("actor_id", "self")
            value = detect.address_claim(content)
            if value:
                slots["value"] = value
            return slots
        if op == "correct":
            # 修正句有两个数字，语序有两种：
            #   「不是五，是七」      旧 → 新，要**后面**那个
            #   「是五十，不是六十」  新 → 旧，要**前面**那个
            #
            # 一律取最后一个，会在第二种语序上"把 60 改成 60"——
            # 看起来什么都没发生，而用户以为改过了（实测踩到）。
            head, _, tail = content.partition("不是")
            scope = head if (tail and re.search(r"\d|[零一二两三四五六七八九十百千万]", head)) else content
            nums = re.findall(r"\d+|[零一二两三四五六七八九十百千万]+", scope)
            n = parse_number(nums[-1]) if nums else None
        else:
            # 先按「数字+量词」抽，再退回裸数字。
            # 「我一共欠你六本书」里的「一共」会被裸数字解析读成 1，
            # 账上就变成欠 1 本——量词锚定能挡住这一类（XM-05）。
            settled = ("还清" in content or "两清" in content) and "不欠" in content
            n = 0 if settled else quantity_cue(content)
            if n is None:
                n = parse_number(content)
        if n is not None:
            slots["quantity"] = n
        # 量词必须**紧跟在数字后面**。
        # 之前是裸搜「碗|个|次|点|条」，「凑个整，25 碗」会先命中"个"，
        # 于是"25 碗大米饭"变成"25 个大米饭"——数字对了，单位错了。
        unit = re.search(
            rf"(?:\d+|[零一二两三四五六七八九十百千万]+)\s*([{QUANTIFIERS}])", content
        )
        if unit:
            slots["unit"] = unit.group(1)
        # 句子里没量词时，从上文状态继承（「还你」跟在「10 碗」后面就是 10 碗）。
        # 状态由调用方放进 context；没有就不猜，交给闸门问。
        if slots.get("unit") is None and context.get("last_unit"):
            slots["unit"] = context["last_unit"]
        # 点对点对话里，债务人默认是发言者本人。
        # 口语里主语省略是常态（「还了」「免了」），要求出现「我」字会把正常句子判成缺槽位。
        # 真实编码器应从语义判断归属；这里是 RuleEncoder 的务实默认（见 XM-02）。
        slots.setdefault("debtor", context.get("actor_id", "self"))
        # 点对点对话里，"对方"通常就是 peer_id；不要求句子里出现"你"。
        # 这是 RuleEncoder 的简化，真实编码器应从语义判断指代。
        # v0.52：「我欠老李 500 块」——债主是老李，不是对话的另一方。
        third = re.search(
            r"欠\s*([^\s你我，。！？,.!?\d零一二两三四五六七八九十百千万]{1,4}?)"
            r"\s*(?=\d|[零一二两三四五六七八九十百千万]|的钱|钱)",
            content,
        )
        owner = None if third else re.search(
            r"(?:还给|还|给)\s*([^\s你我，。！？,.!?\d零一二两三四五六七八九十百千万了]{1,4}?)"
            r"\s*(?=\d|[零一二两三四五六七八九十百千万])"
            r"|([^\s你我，。！？,.!?\d]{1,4}?)的钱",
            content,
        )
        if third:
            slots["creditor"] = third.group(1).rstrip("的")
        elif owner:
            slots["creditor"] = (owner.group(1) or owner.group(2)).lstrip("把")
        elif "你" in content or context.get("peer_id"):
            slots["creditor"] = context.get("peer_id", "peer")
        slots.setdefault("item", context.get("item", "rice"))
        return slots


# ============================================================ 回放编码器
class ReplayEncoder:
    """从标注样本回放编码结果，用于可复现的 T02 / G0 测试。"""

    def __init__(self, samples: Sequence[dict[str, Any]]):
        self._by_text: dict[str, list[dict[str, Any]]] = {}
        for s in samples:
            self._by_text.setdefault(s["utterance"], []).append(s)

    def encode(self, trace_id: str, content: str, context: dict[str, Any]) -> list[Interpretation]:
        rows = self._by_text.get(content, [])
        out: list[Interpretation] = []
        for i, row in enumerate(rows):
            for j, interp in enumerate(row["interpretations"]):
                out.append(Interpretation(
                    interpretation_id=f"in_{trace_id[-8:]}_{i}_{j}",
                    trace_id=trace_id,
                    relation=interp["relation"],
                    modality=interp["modality"],
                    polarity=interp.get("polarity", "affirm"),
                    slots=interp.get("slots", {}),
                    signals=interp.get("signals", {}),
                    confidence=interp.get("confidence"),
                ))
        return out


# ============================================================ LLM 编码器
PROMPT_TEMPLATE = """判断这句话对记忆意味着什么，并抽取结构化信息。只输出 JSON。

可用的关系及其必填槽位：
{specs}

这句话里的线索指向这几个关系：{candidates}

三个出口，按顺序判断，不要跳步：
  A. 属于上面某个关系  → 照格式输出，relation 填那个关系
  B. 属于别的关系      → 也照格式输出，但在 signals 里写
                        "escape": ["为什么它不在候选里"]（会被统计）
  C. 跟记忆无关        → 输出 {{"interpretations":[]}}

**C 是正常答案，不是失败。** 聊天气、感叹、问问题、抱怨，一律走 C。
不要因为候选里只有一项，就硬把它套上去——那会把闲聊记成债务，
比漏记更难查。

但有一种情况**不属于 C**：对方刚说了一个数，你在重复它表示认可
（「一百碗啊……行行行一百碗」）。那不是新信息，是**确认一个已有的数**——
需要记下来，否则账上永远是空的。这种用 set_total，quantity 填那个数。

最近的对话（越靠后越新）：
{recent}

其他上下文：
{meta}

当前对话对方（peer_id）：{peer}

当前的记忆状态：
{state}

用户刚说的这句话：
{content}

输出格式：
{{"interpretations":[{{"relation":"关系名","semantic_op":"...","modality":"...","polarity":"affirm|negate","slots":{{}},"signals":{{}},"confidence":0.0}}]}}

semantic_op 只能取这七个值，它回答的是「这句话在说一件什么事」：
- incur      新增一笔欠款        「我又欠你 5 碗」「再欠 3 碗」
- repay      偿还                「还你 5 碗」「先还点」
- waive      免除                「免我 5 碗」「这三碗就别算了」
- set_total  总额就是 X          「就是三十碗」「凑个整，三十」「其实只欠 4 碗」
- correct    上一句的数说错了    「不是五，是七」「我刚才记错了」
- retract    上一句不算          「那个不算」「刚才那句划掉」
- promise    以后才还/才给       「下个月我还你两袋」「回头补上」「月底结清」

**promise 是这里最容易被判错的一格，因为它和 repay 长得几乎一样。**

  「我还你一袋」      → repay    现在给
  「我明天还你一袋」  → promise  明天给
  「下个月我再还两袋」→ promise  下个月给

  区别只有一个时间词，但后果差一整笔账：判成 repay，余额**现在就少了两袋**，
  而实际上那两袋还在你手里。之后每一句都跟着偏，看起来像"整段都错"，
  其实错源只有这一句。

  只要句子里出现「下 / 明天 / 后天 / 回头 / 改天 / 过两天 / 月底 / 年底 /
  下次 / 待会 / 发工资」这类指向未来的词，且说的是欠/还/给/补，就是 promise。
  给了数量的承诺，把数量填进 quantity——承诺也是有数的，只是还没兑现。

**反过来，过去的时间词不是 promise**：「上周我还了你两袋」是 repay，
  它已经发生了，该立刻入账。别把「上个月」「昨天」「刚才」也当成未来。

incur 和 set_total 的区别，是这里最容易错的地方：
  incur 是「在原来基础上再增加」。已经有 30 碗时说「我欠你 40 碗」，是又欠 40，共 70。
  set_total 是「不管原来多少，总数改成这个」。说「凑个整三十碗」，是总数变 30。

  **「我欠你四十碗」第一次说，也用 incur，不是 set_total。**
  是不是第一笔由系统自己判断（空账时 incur 会落成 set），不需要你来决定。
  只有明确表达「总数改成 X」时才用 set_total：「凑个整」「就按三十算」「其实只欠四碗」。

  判不出来的时候用 incur，不要用 set_total —— 把该累加的抹掉，比多算一笔更难发现。

correct 和 incur 的区别：
  「不是五，是七」是 correct，因为它在**改动刚才那句话**，不是新增。
  说成 incur 会让余额从 35 变成 42，而正确答案是 33。

slots 规则：
1. 只填该关系声明的必填槽位，不要塞别的字段。
2. 只填原话能直接支持的值。原话没提数量就不要填数量，不要猜。
3. 数量用阿拉伯数字。
4. **角色只用三种写法**：说话人「我」= `player`；对话对方「你」= 上面给的 peer_id；
   第三人称用原话里的名字（如「老张」）。**禁止**写 `assistant` / `user` / `self` / `peer`。
5. **量词必须填**：原句出现「碗/袋/本/个/块」就照抄进 `unit`；
   欠/还/免的句子没有 unit 视为缺槽位。
6. **item 用上下文给出的标识**（如 `rice`），不要写「白米饭」这种原话名词；
   上下文没给时才用原话。
7. 「我欠你 X」「我确认欠你 X」「再欠 X」都是 **incur**；
   只有「一共 / 总共 / 就是 X / 凑个整 / 改成 X」才是 set_total。

8. semantic_op 是 correct 或 retract 时，加一个 "target_hint"：
   - "last_repay"   改的是最近一次偿还
   - "last_incur"   改的是最近一次新增
   - "first_incur"  改的是**最开始那一笔**（「我一开始就欠 30 碗」「当初说的不是 25」）
   - "last"         不确定改的是哪条

   ⚠「我一开始就欠 30 碗，不是 25」是 correct + first_incur，**不是 set_total**。
   set_total 的意思是「现在总数是 30」，会把期间的还款一起抹掉；
   而这句话的意思是「当初那个数记错了」，还款仍然有效。
9. 涉及钱/物的语义（incur/repay/waive/set_total/correct/retract）放进 semantic_op，不要放进 slots。

modality 只能取：asserted / request / hypothetical / conditional / quoted / denied /
planned / joking / ironic / in_world / unclear

  祈使句、请求用 request：「给我免三碗」「能不能免一点」「这三碗别算了」。
  **不要**把请求判成 hypothetical —— hypothetical 是「假如我欠你一万碗」，
  说话人并不认为它现在成立；而「给我免三碗」是他真的在要求这件事。

  玩笑、反讽、假设、转述、角色台词都不是 asserted。
  不确定是不是在陈述事实时用 unclear。

  承诺（promise）用 planned——它说的是未来的事，不是现在的事实。

  **「是不是……来着」这类疑问句，不是提问，是求确认。**
  「我上个月是不是欠你五十枚金币来着？」说话人已经认定这件事成立，
  只是等对方点头。它带数量、带立场，**必须记**，不能走"跟记忆无关"的出口。
  判成 asserted，并在 signals 里写 "seeking_agreement": true。

  真正的提问（「我欠你多少来着？」）没有数量，那才走"跟记忆无关"。

polarity：肯定某件事用 affirm，否定用 negate。

如果这句话是在纠正或撤销上一句（「那个不算」「我刚才说错了」），
它自己通常不含任何领域词——此时 relation 用上下文里的 last_relation。

如果这句话跟上面这些关系完全无关（纯闲聊、感叹、聊天气、问问题），输出：
{{"interpretations":[]}}
"""


# 关系线索。用来**收窄候选**，不是用来判定。
#
# 为什么需要收窄：让模型从全部关系里自由选，它会被强信号带跑——
# 实测「哎阿鲸，那个……我好像还欠你四十碗白米饭是吧」被稳定判成
# `nickname`（称呼），三轮都一样。不是抽签抽坏了，是「阿鲸」这个名字
# 盖过了「欠……碗」这个真正的线索（XM-15）。
#
# 收窄是**提示**，不是硬限制：prompt 允许模型选列表外的关系，
# 但要求它写 `escape` 理由。这个计数就是「收窄是不是过窄」的指标。
RELATION_HINTS = {
    "debt": ("欠", "还", "借", "碗", "债", "免", "账", "赊", "粮"),
    "nickname": ("叫我", "称呼", "名字", "喊我"),
    "preference": ("喜欢", "讨厌", "爱吃", "受不了", "习惯"),
}

# 偏好关系的**判定**证据（比 RELATION_HINTS 宽一点：提示词只是收窄候选）。
# 没有这些词，一句话就不该被判成"在说喜好"——见 `_guess_relation`。
_PREFERENCE_WORDS = (
    "喜欢", "讨厌", "爱吃", "爱喝", "受不了", "习惯", "偏爱", "最爱", "不爱", "想吃",
    # 否定形式是最常见的一类（「我不吃辣」），
    # 少了它，这句话会继承上一句的关系——上一句是称呼，它就变成改称呼。
    "不吃", "不喝", "不爱吃", "不爱喝", "怕吃", "忌口", "过敏",
)


_OTHER_PREFERENCE_SUBJECT = re.compile(
    r"(?:我(?:家|的)?(?:兔子|猫|狗|泰迪|宠物|哥哥|姐姐|弟弟|妹妹|哥|姐|爸|妈|爸爸|妈妈|朋友|家人|孩子|儿子|女儿|老婆|丈夫|妻子|男友|女友)|你|(?<!其)他|她|它)"
)


def _self_preference_text(content: str) -> str:
    """只屏蔽能明确识别为他人或宠物的分句，保留原话以供检索。"""
    parts = re.split(r"([，。！？,.!?；;])", content)
    for i in range(0, len(parts), 2):
        if re.search(r"(?:没(?:有)?说(?:过)?|并没(?:有)?说|不要认为|别以为)", parts[i]):
            parts[i] = ""
            continue
        match = _OTHER_PREFERENCE_SUBJECT.search(parts[i])
        if not match:
            continue
        if any(word in parts[i][:match.start()] for word in _PREFERENCE_WORDS):
            continue
        following_self = parts[i].find("我", match.end())
        parts[i] = parts[i][following_self:] if following_self >= 0 else ""
    return "".join(parts)


def _has_preference_evidence(content: str) -> bool:
    content = _self_preference_text(content)
    # “设计偏好编号”是话题标签，不是“我偏好某物”的陈述。
    if any(re.search(re.escape(word) + r"(?!的)", content) for word in _PREFERENCE_WORDS):
        return True
    return bool(re.search(r"(?:^|[，。！？,.!?]|我|本人|自己|更|较|比较)\s*偏好", content))


def candidate_relations(content: str, context: dict[str, Any], registry) -> list[str]:
    """按关键词和上文给几个候选，不给结论。"""
    known = {spec.relation for spec in registry.all()}
    hits = [
        rel for rel, words in RELATION_HINTS.items()
        if rel in known and any(w in content for w in words)
    ]
    last = context.get("last_relation")
    if last and last in known and last not in hits:
        hits.append(last)
    return hits


def render_recent(context: dict[str, Any]) -> str:
    """把最近几轮渲染成人话，带上「谁说的」。

    之前是把 `{"recent":[{"role":"assistant","content":"..."}]}` 整个丢进去。
    模型要自己去猜 role=assistant 是哪一方，而"复述确认"恰恰依赖这件事：
    「一百碗」是对方先说的，我只是重复——**不知道谁说的，就判不出来**。
    """
    recent = context.get("recent") or []
    if not recent:
        return "（这是第一句）"
    lines = []
    for turn in recent:
        who = "我" if turn.get("role") == "user" else "对方"
        content = str(turn.get("content", "")).replace("\n", " ")
        lines.append(f"[{who}] {content}")
    return "\n".join(lines)


def render_meta(context: dict[str, Any]) -> str:
    """recent / state 之外的结构字段（对方是谁、什么物品等）。

    这些必须单独列出来。之前把它们和 recent 一起塞在 `{context}` 里，
    改成渲染人话时**整块丢掉了**——模型不知道对方是谁，
    填不出 creditor/item，闸门一律判缺槽位，主对话直接掉到 0/12。
    一次格式重构，丢了一个字段，全盘皆输。
    """
    meta = {
        k: v for k, v in context.items()
        if k not in ("recent", "state", "last_relation")
    }
    if not meta:
        return "（无）"
    return "、".join(f"{k} = {v}" for k, v in meta.items())


def render_state(context: dict[str, Any]) -> str:
    """当前状态摘要：**只说有没有值，不说值是多少**。

    为什么不说值：给了具体数字，模型会拿它当锚。
    账上是 33，用户说「再欠 5 碗」，它可能判成「在确认 33」——
    实测主对话逐步正确率因此从 12 降到 8（最终余额倒是对的，
    因为后面有句子把数掰回来了）。

    但也不能完全不给：不知道"账上有没有数"，就判不出
    「一百碗啊……行行行一百碗」是在**建立基数**还是在复述（XM-16）。

    所以给的是**有/无**，不是数值。前者决定"要不要建账"，后者只会误导。
    """
    states = context.get("state") or {}
    if not states:
        return "（还没有任何已确认的值）"
    return "、".join(
        f"{k}：{'已有一个值' if isinstance(v, (int, float)) else v}"
        for k, v in states.items()
    )


def describe_specs(
    registry, only: list[str] | None = None, prioritize: list[str] | None = None
) -> str:
    """把关系规格压成几行人话，喂给模型。

    只给结构，不给例句——给了例句模型就会照着句子形状套，
    测出来的不是它的理解力，是它的模仿力。

    `prioritize` **只排序，不过滤**。
    早先试过"只给候选关系"，主对话中位数从 4–8 提到 12，但小狗段从 9 掉到 0：
    「一百碗啊……行行行一百碗，不过话说回来你那个电饭煲…」这种**混合意图**的句子，
    候选里只有 debt，模型觉得整句不匹配，直接返回空。
    **把别的选项拿掉，等于不让人说"不"。**
    """
    specs = [sp for sp in registry.all() if not only or sp.relation in only]
    if prioritize:
        specs.sort(key=lambda sp: 0 if sp.relation in prioritize else 1)

    def block(spec) -> list[str]:
        return [
            f"- {spec.relation}（风险等级 {spec.risk_tier}）",
            f"  必填槽位：{', '.join(spec.required_slots)}",
            f"  允许的 modality：{', '.join(spec.allowed_modalities)}",
            *([f"  数量单位：{spec.unit}"] if spec.unit else []),
            *([f"  说明：{spec.note}"] if spec.note else []),
        ]

    if not prioritize:
        return "\n".join(line for spec in specs for line in block(spec))

    head = [sp for sp in specs if sp.relation in prioritize]
    tail = [sp for sp in specs if sp.relation not in prioritize]
    lines = [line for spec in head for line in block(spec)]
    if tail:
        lines.append("")
        lines.append("其他关系（这句话确实在说它们时才用）：")
        lines.extend(line for spec in tail for line in block(spec))
    return "\n".join(lines)


class HybridEncoder:
    """规则优先，缺槽位时才问模型。

    为什么不是"全用 LLM"：实测在 60 句开发集上，规则版的关系/意图更稳
    （93.3%/81.7% vs 78.3%/55.0%），而模型抽 item/unit 更准。
    所以只用它补规则版明显缺的东西，成本也降到 1/6 左右。
    """

    def __init__(self, rule: "RuleEncoder", llm: "LLMEncoder"):
        self.rule = rule
        self.llm = llm

    @property
    def calls(self) -> int:
        return self.llm.calls

    @property
    def usage(self) -> list[dict[str, Any]]:
        return self.llm.usage

    @property
    def agreements(self) -> list[str]:
        return self.llm.agreements

    def encode(self, trace_id: str, content: str, context: dict[str, Any]) -> list[Interpretation]:
        base = self.rule.encode(trace_id, content, context)
        if not any(self._needs_llm(item) for item in base):
            return base
        try:
            alt = self.llm.encode(trace_id, content, context)
        except Exception:  # noqa: BLE001 —— 模型挂了就用规则结果，不影响写路径
            return base
        if not alt:
            return base
        return self._merge(base, alt)

    @staticmethod
    def _needs_llm(item: Interpretation) -> bool:
        return item.relation == "debt" and bool(item.signals.get("missing_slots"))

    @staticmethod
    def _merge(base: list[Interpretation], alt: list[Interpretation]) -> list[Interpretation]:
        by_relation = {}
        for item in alt:
            by_relation.setdefault(item.relation, item)
        for item in base:
            other = by_relation.get(item.relation)
            if other is None:
                continue
            # 只补空槽位；规则版已有的判断（relation / semantic_op）不动。
            for key, value in other.slots.items():
                if item.slots.get(key) is None and value is not None:
                    item.slots[key] = value
            if item.signals.get("missing_slots"):
                item.signals["missing_slots"] = [
                    k for k in item.signals["missing_slots"] if item.slots.get(k) is None
                ]
            item.signals["hybrid"] = "rule+llm"
        return base


class LLMEncoder:
    """调用 OpenAI 兼容接口。需要使用者自己提供 key 与 base_url。

    这里刻意不内置任何默认服务商：这是使用者的选择，不是内核的选择。
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        registry=None,
        specs_summary: str = "",
        timeout: float = 60.0,
        samples: int = 1,
        max_tokens: int = 1024,
        extra_params: dict[str, Any] | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.registry = registry
        self.specs_summary = specs_summary or (describe_specs(registry) if registry else "")
        # 候选处理方式：
        #   "rank" 把候选关系排到前面，其余仍列出（默认）
        #   "only" 只列候选——**已证明有害**，见 describe_specs 的注释
        #   "off"  完全不管候选，用来做 A/B 对照
        self.narrow = "rank"
        self.timeout = timeout
        # 同一句话问几次，按多数投票（self-consistency）。
        #
        # 为什么需要：这个模型的输出在 temperature=0 下仍然会变。
        # 实测同一段对话三轮跑出 1/12、12/12、8/12——**能力上限存在，但方差太大**。
        # 降方差的标准手段就是多次采样取多数，代价是成本 ×N。
        #
        # samples=1 时行为与之前完全一致。
        self.samples = max(1, samples)
        # 推理型模型（MiMo 等）默认会"雷霆大思考"，把输出预算全烧在 reasoning 上。
        # 这两个参数让调用方显式压低思考与输出（例如 reasoning_effort=low / none）。
        self.max_tokens = max(16, int(max_tokens))
        self.extra_params = dict(extra_params or {})
        self.calls = 0          # 便于评估时统计真实调用次数
        self.usage: list[dict[str, Any]] = []   # 每次调用的 token 用量（成本核算）
        self.agreements: list[str] = []   # 每次编码的投票一致度，便于事后统计

    def encode(self, trace_id: str, content: str, context: dict[str, Any]) -> list[Interpretation]:
        if self.samples <= 1:
            return self._encode_once(trace_id, content, context)

        runs = [self._encode_once(trace_id, content, context) for _ in range(self.samples)]

        def signature(interps: list[Interpretation]) -> tuple[str, str, str]:
            # 按「第一候选」投票。多候选的完整集合投票太复杂，
            # 而第一候选通常就是主解释——先做能验证的最小版本。
            if not interps:
                return ("", "", "")
            head = interps[0]
            return (head.relation, head.semantic_op or "", head.modality)

        votes = Counter(signature(r) for r in runs)
        best, n = votes.most_common(1)[0]
        self.agreements.append(f"{n}/{self.samples}")
        chosen = next(r for r in runs if signature(r) == best)

        # 重新编号：多次调用拿到的 interpretation_id 是一样的（同一个 trace），
        # 直接返回会撞主键。
        for k, interp in enumerate(chosen):
            interp.interpretation_id = f"in_{trace_id[-8:]}_{k}_v{n}"
            interp.signals = {**interp.signals,
                              "vote": f"{n}/{self.samples}"}
        return chosen

    def _render_prompt(self, content: str, context: dict[str, Any]) -> str:
        """拼 prompt。抽出来是为了能单独测——**上一次改格式丢了字段，主对话掉到 0/12**。"""
        cands = (candidate_relations(content, context, self.registry)
                 if self.registry is not None else [])
        if self.narrow == "only" and cands:
            specs_text = describe_specs(self.registry, only=cands)
        elif self.narrow == "rank" and cands:
            specs_text = describe_specs(self.registry, prioritize=cands)
        else:
            specs_text = self.specs_summary
        cands_text = (
            f"{'、'.join(cands)}（或「无关」）"
            if cands else "（没有明显线索，从全部关系里判断）"
        )
        return PROMPT_TEMPLATE.format(
            specs=specs_text,
            candidates=cands_text,
            recent=render_recent(context),
            meta=render_meta(context),
            peer=context.get("peer_id", "peer"),
            state=render_state(context),
            content=content,
        )

    def _encode_once(
        self, trace_id: str, content: str, context: dict[str, Any]
    ) -> list[Interpretation]:
        import time
        import urllib.error
        import urllib.request

        prompt = self._render_prompt(content, context)
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            **self.extra_params,
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        self.calls += 1
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
                if isinstance(data.get("usage"), dict):
                    self.usage.append(data["usage"])
                raw = data["choices"][0]["message"]["content"]
                return self._parse(trace_id, raw)
            except (urllib.error.HTTPError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))

        # 三次都失败：**降级为 unclear，不是当作"没有这句话"**。
        # 编码失败和"这句话没有记忆价值"是两件事，混在一起会让评估结果失真。
        return [Interpretation(
            interpretation_id=f"in_{trace_id[-8:]}_err",
            trace_id=trace_id, relation="", modality="unclear",
            polarity="affirm", slots={}, signals={"encoder_error": str(last_error)},
        )]

    def _parse(self, trace_id: str, raw: str) -> list[Interpretation]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return [Interpretation(
                interpretation_id=f"in_{trace_id[-8:]}_bad",
                trace_id=trace_id, relation="", modality="unclear",
                polarity="affirm", slots={}, signals={"parse_error": True},
            )]

        out = []
        for i, item in enumerate(payload.get("interpretations", [])):
            if not isinstance(item, dict):
                continue
            # slots 必须是 dict。模型偶尔返回 ["quantity"] 这种列表，
            # 直接 dict() 会抛异常，透传到闸门会变成 AttributeError——
            # 一个编码格式问题不该让整条写路径崩掉。
            raw_slots = item.get("slots")
            slots: dict[str, Any] = {}
            if isinstance(raw_slots, dict):
                slots = dict(raw_slots)
            elif raw_slots:
                slots = {}          # 形状不认识就丢，让闸门按缺槽位挂起
            # 兼容模型把 intent 写在 interpretation 顶层的情况（第一次跑就踩了）
            if "intent" in item and "intent" not in slots:
                slots["intent"] = item["intent"]
            # target_hint 同理
            if "target_hint" in item and "target_hint" not in slots:
                slots["target_hint"] = item["target_hint"]
            # 模型经常把数字写成字符串（"30"）。在这里归一，别让它污染内核。
            q = slots.get("quantity")
            if isinstance(q, str):
                try:
                    slots["quantity"] = float(q) if "." in q else int(q)
                except ValueError:
                    slots.pop("quantity", None)

            op = item.get("semantic_op")
            if op not in _SEMANTIC_OPS:
                # 模型编了一个没定义的操作。置 None 让它走回落，
                # 而不是把未知值透传给内核——后者会让 resolve 落到默认分支，
                # 悄悄当成 incur 记一笔。
                op = None

            # `item.get(k, default)` 挡不住**显式给了 null** 的情况：
            # `{"modality": null}` 会让 .get 返回 None，然后撞 DB 的 NOT NULL。
            # 实测崩过一次（小狗段第 3 轮）。所以用 `or`，不用默认参数。
            out.append(Interpretation(
                interpretation_id=f"in_{trace_id[-8:]}_{i}",
                trace_id=trace_id,
                relation=item.get("relation") or "",
                modality=item.get("modality") or "unclear",
                polarity=item.get("polarity") or "affirm",
                slots=slots,
                signals=(item.get("signals") if isinstance(item.get("signals"), dict) else {}),
                confidence=item.get("confidence"),
                semantic_op=op,
            ))
        return out


# ------------------------------------------------------------ v0.52 辅助
_QUESTION_TAIL = re.compile(r"(吗|嘛|么|呢)\s*[？?。.！!~～…]*\s*$")
_QUESTION_WORDS = ("谁", "什么", "哪里", "哪儿", "哪个", "哪些", "为什么",
                   "怎么", "多少", "几个", "几次", "记得吗", "记不记得", "是否")


def is_question(text: str) -> bool:
    """这句话是不是在**问**。只认很明显的问句，宁漏不错。"""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith(("?", "？")):
        return True
    if _QUESTION_TAIL.search(t):
        return True
    return any(w in t for w in _QUESTION_WORDS) and not any(
        w in t for w in ("我叫", "叫我", "记住"))


_TIME_WORDS = re.compile(
    r"(周|星期|礼拜)[一二三四五六日天末]"
    r"|(?:\d+|[零一二两三四五六七八九十]+)\s*(?:月|号|日|点钟|点半|点|分钟|小时|年|岁|周|天|个月)"
)


def strip_time_words(text: str) -> str:
    """去掉日期/时刻/年龄里的数字，免得被当成数量。"""
    return _TIME_WORDS.sub(" ", text or "")


def _reliable_preference_object(value: str, obj: str) -> bool:
    """无法确定对象时只保留原话，避免把片段提升为事实。"""
    if len(obj) > 16 or not any(ch.isalnum() for ch in obj):
        return False
    # 书名内部的“喜欢”不是偏好动词，截出不完整书名时不能生成事实。
    if obj.count('《') != obj.count('》'):
        return False
    if value == "习惯" and obj.startswith("了"):
        return False
    if value in ("不吃", "不喝") and obj in ("饿", "饱"):
        return False
    if value == "不爱" and obj in ("吃", "喝"):
        return False
    if value == "讨厌" and obj.startswith("别说"):
        return False
    return obj not in ("这个", "那个", "什么", "这样", "那样", "吗", "么")


def _clean_pref_object(obj: str) -> str:
    """偏好对象去掉尾巴：「辣了」→「辣」，「喝奶茶」→「奶茶」。"""
    obj = (obj or "").strip()
    # 对象到下一个主语/连词为止：「不吃辣我不吃辣」→「辣」，「猫但是怕狗」→「猫」
    obj = re.split(r"但|因为|所以|而且|不过|可是|(?:我|你|他|她|也|就|都|还是)(?=喜欢|讨厌|不)", obj, maxsplit=1)[0]
    obj = re.sub(r"[了的啊呀呢吧哦啦嘛]+$", "", obj)
    obj = re.sub(r"^(吃|喝|看|玩|听|用|去)(?=..)", "", obj)
    return obj
