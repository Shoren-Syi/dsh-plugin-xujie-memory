"""把澄清请求渲染成人话——角色口吻，不是表单。

内核只产出结构化数据（relation / semantic_op / slots），
**措辞在这里**。这样角色设定（叫"主人"还是"你"、自称"鲸鲸"还是"我"）
不会污染内核，换个 persona 就能换一种说话方式。

    persona = Persona(address="主人", names={"kitty": "鲸鲸", "rice": "大米饭"})
    ask(request, persona)
    → 主人，你说的给鲸鲸 40 碗大米饭，是真的吗？

为什么不把话术写进内核：
内核的测试全在断言状态机，不该因为改了一句台词而变红。
反过来，话术也不该因为改了一个状态机分支而重新审视。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .due import describe as describe_due


@dataclass
class Persona:
    """角色设定。全部有默认值，什么都不配也能用。"""

    address: str = "你"                 # 怎么称呼用户
    names: dict[str, str] = field(default_factory=dict)  # 实体 → 显示名

    def name_of(self, value: Any, default: str = "?") -> str:
        if value in (None, ""):
            return default
        return self.names.get(str(value), str(value))


# 槽位的中文说法。缺槽位时会问「主人，{东西}是多少呀？」
_SLOT_LABEL = {
    "quantity": "数量",
    "unit": "单位",
    "item": "是什么东西",
    "debtor": "是谁欠的",
    "creditor": "欠谁的",
    "subject": "说的是谁",
    "object": "说的是什么",
    "value": "具体是什么",
}


def describe(detail: dict[str, Any], persona: Persona) -> str:
    """把一条解释渲染成短语：「给鲸鲸 40 碗大米饭」。"""
    slots = detail.get("slots") or {}
    relation = detail.get("relation") or ""
    op = detail.get("semantic_op")

    if relation == "debt":
        who = persona.name_of(slots.get("creditor"), "对方")
        item = persona.name_of(slots.get("item"), "")
        qty = _quantity(slots, persona)
        body = f"{qty}{item}".strip()
        due = slots.get("due_hint")
        # 承诺要带上时间，否则用户听到的「还 2 袋」和已经还了没区别。
        # 「下个月要还给鲸鲸的 2 袋」——一听就知道还没发生。
        promised = f"{due}要还给{who}的 {body}" if due else f"要还给{who}的 {body}"
        return {
            "repay": f"还给{who}的 {body}",
            "promise": promised,
            "waive": f"免掉给{who}的 {body}",
            "set_total": f"欠{who}的一共 {body}",
            "correct": f"改成 {body}",
            "retract": "刚才那条不算",
            None: f"给{who} {body}",
        }.get(op, f"给{who} {body}")

    if relation == "nickname":
        return f"叫你「{slots.get('value', '')}」"

    if relation == "preference":
        obj = slots.get("object") or slots.get("item") or ""
        return f"{obj}这件事" if obj else "这件事"

    item = persona.name_of(slots.get("item"), "")
    qty = _quantity(slots, persona)
    return f"{qty}{item}".strip() or relation or "这件事"


def _quantity(slots: dict[str, Any], persona: Persona) -> str:
    q = slots.get("quantity")
    if q is None:
        return ""
    unit = persona.name_of(slots.get("unit"), "")
    return f"{q}{unit}"


def ask(detail: dict[str, Any], persona: Persona | None = None) -> str:
    """把一条澄清请求渲染成一句人话。

    四种 kind 各有各的问法，但都不出现"确认/取消"这种界面词——
    **确认应该藏在对话里，不该变成一次表单提交。**
    """
    p = persona or Persona()
    kind = detail.get("kind") or "unconfirmed"

    if kind == "ambiguous_candidates":
        names = [
            _relation_label(c.get("relation"), c.get("semantic_op"))
            for c in detail.get("candidates", [])
        ]
        names = [n for n in names if n]
        if len(names) >= 2:
            return f"{p.address}，你是在说{names[0]}，还是{names[1]}呀？"
        return f"{p.address}，这句我有点拿不准，你能再说清楚一点吗？"

    if kind == "balance_check":
        said = detail.get("said")
        actual = detail.get("actual")
        unit = detail.get("unit") or ""
        said_t = f"{said:g}" if isinstance(said, (int, float)) else str(said or "?")
        actual_t = f"{actual:g}" if isinstance(actual, (int, float)) else str(actual or "?")
        return (
            f"{p.address}，你说还剩 {said_t}{unit}，"
            f"可我记的是 {actual_t}{unit}。哪个对？"
        )

    if kind == "settled_gap":
        # 「你说还清了，可账上还差 3」——不指责，只把两个说法摆在一起。
        qty = detail.get("outstanding")
        qty_text = f"{qty:g}" if isinstance(qty, (int, float)) else str(qty or "")
        unit = detail.get("unit") or ""
        return (
            f"{p.address}，你说还清了，可账上还差 {qty_text}{unit}。"
            f"是还有一笔没记上吗？"
        )

    if kind == "total_reading":
        # 两个数都念出来。少念一个，用户就是在猜我指的是哪一个。
        gross = detail.get("gross")
        remaining = detail.get("remaining")
        repaid = detail.get("repaid")
        g = f"{gross:g}" if isinstance(gross, (int, float)) else "?"
        r = f"{remaining:g}" if isinstance(remaining, (int, float)) else "?"
        p_ = f"{repaid:g}" if isinstance(repaid, (int, float)) else "?"
        return (
            f"{p.address}，你说的 {g}，是现在还欠 {g}，"
            f"还是当初一共欠 {g}？"
            f"（已经还过 {p_}，如果是后者，那就还剩 {r}）"
        )

    if kind == "total_overrides":
        n = detail.get("erased_count") or 0
        total = detail.get("erased_total")
        total_text = f"{total:g}" if isinstance(total, (int, float)) else "若干"
        return (
            f"{p.address}，这么改会把之前 {n} 笔还款（共 {total_text}）一起作废，"
            f"你确定吗？"
        )

    if kind == "unresolvable_target":
        return f"{p.address}，你说的「不是 / 不算」，我这边没找到对应的那一笔，是指哪一次？"

    if kind == "missing_slot":
        missing = detail.get("missing") or []
        labels = [_SLOT_LABEL.get(m, m) for m in missing]
        what = "和".join(labels) if labels else "细节"
        return f"{p.address}，{what}是多少呀？"

    # unconfirmed：最常见的一种
    body = describe(detail, p)
    return f"{p.address}，你说的{body}，是真的吗？"


def _relation_label(relation: str | None, op: str | None) -> str:
    return {
        "debt": "欠账的事",
        "nickname": "称呼的事",
        "preference": "喜好",
    }.get(relation or "", "")


def acknowledge(detail: dict[str, Any], persona: Persona | None = None) -> str:
    """确认之后的一句话。不是"已提交"，是"记下了"。"""
    p = persona or Persona()
    body = describe(detail, p)
    if detail.get("semantic_op") == "promise":
        # 「记下了，下个月要还给鲸鲸的 2 袋」——这句必须听起来像"记了一笔
        # 待办"，而不是"账已结清"。中文里"记下了"两者都能接，
        # 所以补一句时间，把语义钉死。
        return f"记下了，{body}，到日子我提醒你。"
    if not body or body == "刚才那条不算":
        return "嗯，记下了。"
    return f"记下了，{body}。"


def declined(persona: Persona | None = None) -> str:
    """用户否认之后的一句话。不追问，等他自己说。"""
    p = persona or Persona()
    return f"好，那这条先不算。{p.address}你再说一次？"


# ============================================================ 提醒
def _split_key(state_key: str) -> tuple[str, str]:
    """从 state_key 里取债主和标的。

    `sc_xxx|player|debt|kitty|item=rice` → ("kitty", "rice")
    取不出来就给空——提醒宁可少一个名字，也不能因为解析失败而崩掉整轮对话。
    """
    parts = (state_key or "").split("|")
    creditor = parts[3] if len(parts) > 3 and parts[3] != "-" else ""
    item = ""
    if len(parts) > 4 and parts[4].startswith("item="):
        item = parts[4][5:]
    return creditor, item


def remind(item: dict[str, Any], persona: Persona | None = None) -> str:
    """一句到期提醒。

    **不出现"任务/待办/逾期"这类词**。承诺是人和人之间的事，
    不是工单系统。所以句型跟他当初说的那句话尽量贴近：
    「你说月底还我两袋」，而不是「您有一条逾期记录」。
    """
    p = persona or Persona()
    creditor, item_name = _split_key(item.get("state_key", ""))
    who = p.name_of(creditor, "你")
    thing = p.name_of(item_name, "")

    qty = item.get("remaining")
    if qty in (None, 0):
        qty = item.get("quantity")
    qty_text = f"{qty:g}" if isinstance(qty, (int, float)) else str(qty or "")
    unit = item.get("unit") or ""
    body = f"{qty_text}{unit}{thing}".strip()

    when = describe_due(item.get("due_at"))
    if item.get("due_at"):
        return f"{p.address}，你说{when}要还{who} {body}，今天到期了。"
    return f"{p.address}，你答应过还{who} {body}，还没还呢。"


# ============================================================ 查账的答案
def _conflict(reason: str | None) -> str:
    """把内核的冲突原因翻成人话。

    只有真正见过的几种才翻。翻了不认识的，就把原文念出来——
    **宁可说得笨，不可说得像懂了。**
    """
    if not reason:
        return "有个地方对不上"
    if reason.startswith("unit_mismatch:"):
        _, _, pair = reason.partition(":")
        left, _, right = pair.partition("!=")
        return f"前面记的是「{left}」，这次说的是「{right}」，两个单位我不敢直接加减"
    if reason.startswith("non_negative_violated"):
        return "这么算会变成负数"
    return reason

def answer(detail: dict[str, Any], persona: Persona | None = None) -> str:
    """「还剩几捆？」/「我一共欠多少？」——把账上的数念出来。

    三种答案**必须是三句不同的话**，不能合并成一句"没有"：

        known      3 捆
        unknown    记过，但依据被撤了 —— 不是 0，是"现在说不准"
        not_found  从来没记过 —— 不是"你没欠"，是"你没跟我说过"

    把后两种都说成「没有」，就是把"系统的问题"和"事实的问题"
    讲成同一件事。而这套系统存在的全部理由，就是分得清这两者。
    """
    p = persona or Persona()
    status = detail.get("status")
    creditor, item_from_key = _split_key(detail.get("state_key") or "")

    if status == "not_found":
        return f"{p.address}，这件事你没跟我说过，我这儿没记着。"

    if status == "disputed":
        return f"{p.address}，这笔账我这儿对不上（{_conflict(detail.get('reason'))}），不敢报数。"

    if status == "unknown":
        return (
            f"{p.address}，这笔我记过，"
            f"但当时那条依据后来被撤了，现在说不准。"
        )

    value = detail.get("value")
    unit = p.name_of(detail.get("unit"), "")
    item = p.name_of(detail.get("item") or item_from_key, "")
    who = p.name_of(detail.get("creditor") or creditor, "对方")
    value_t = f"{value:g}" if isinstance(value, (int, float)) else str(value)

    body = f"{value_t} {unit}{item}".strip()
    return f"{p.address}，还欠{who} {body}。"
