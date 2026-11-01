"""自动重要性：不等用户说「记住」，先给每句话打个分。

分不是拍脑袋的「AI 觉得」——至少 v1 不是。它是一组**可解释的线索**加权：

    身份/自称        「我是…」「我叫…」          +2.0
    强偏好           「最喜欢/最讨厌/过敏」       +1.6
    承诺/计划        「答应/决定/下个月」         +1.4
    健康/钱/变动     「手术/房租/辞职」           +1.3
    关系人物         「老婆/孩子/老板」           +1.2
    情绪强度         「特别/害怕/永远」           +1.0
    日期事件         「2026 年/生日」             +0.8

    寒暄/天气/查询    「你好/天气/帮我查」        −1~−2

分数映射到 1–5 的 base：普通 1.0，值得记的 1.5–3.0，最高 4.0。
**5.0 和锚点留给用户明说**——自动判断只决定「淡得慢一点」，不决定「永远不忘」。
这样误判的代价只是「多留一阵」，不是「钉死一条错的」。

以后要换成模型打分，只要实现同样的 `score(text)` 返回结构即可；
调用方（编排器）不需要知道分数是怎么来的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .decay import Lifecycle

# (名字, 正则, 权重)
_POSITIVE = (
    ("identity", r"我(?:是|叫|姓|住在|来自|今年)|我的生日|我养的", 2.0),
    ("preference", r"最喜欢|最讨厌|最爱|受不了|吃不惯|过敏|不吃|不喝", 1.6),
    ("commitment", r"答应|约好|决定|计划|以后|下次|明天|后天|下周|下个月|月底|回头", 1.8),
    ("health_money", r"生病|医院|手术|吃药|过敏|工资|房租|存款|搬家|辞职|入职|考试", 1.3),
    ("relationship", r"老婆|老公|男朋友|女朋友|孩子|女儿|儿子|爸妈|父母|朋友|同事|老板", 1.2),
    ("emotion", r"特别|非常|永远|恨|害怕|担心|难过|开心|孤独|焦虑|后悔", 1.0),
    ("date", r"\d{4}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*日|生日|纪念日", 0.8),
)

_NEGATIVE = (
    ("chitchat", r"^\s*(你好|您好|在吗|晚安|早安|哈哈|嗯|哦|好的|谢谢)", -2.0),
    ("weather", r"天气|下雨|下雪|好热|好冷|风真大", -1.2),
    ("meta", r"帮我(?:查|写|翻译|算)|解释一下|什么是|怎么用", -0.8),
)


@dataclass
class Importance:
    score: float                      # 0–1
    value: float                      # 1.0–4.0（base）
    reasons: list[str] = field(default_factory=list)


def score(text: str) -> Importance:
    text = (text or "").strip()
    if not text:
        return Importance(0.0, 1.0, ["empty"])

    raw = 0.0
    reasons: list[str] = []
    for name, pattern, weight in _POSITIVE:
        if re.search(pattern, text):
            raw += weight
            reasons.append(name)
    for name, pattern, weight in _NEGATIVE:
        if re.search(pattern, text):
            raw += weight
            reasons.append(name)

    if len(text) >= 15:
        raw += 0.4
        reasons.append("long")
    elif len(text) <= 4:
        raw -= 0.8
        reasons.append("too_short")

    score_value = max(0.0, min(1.0, raw / 5.0))
    if score_value < 0.25:
        value = 1.0
    elif score_value < 0.4:
        value = 1.5
    elif score_value < 0.6:
        value = 2.0
    elif score_value < 0.8:
        value = 3.0
    else:
        value = 4.0
    return Importance(round(score_value, 4), value, reasons)


def apply(engine, scope_id: str, trace_id: str, text: str,
          *, source: str = "auto") -> Importance | None:
    """给刚写下的这句话打重要性。普通句不写事件，避免每轮都堆一条。"""
    lifecycle = Lifecycle(engine)
    current = lifecycle.get(trace_id)
    if current.get("anchor"):
        return None                 # 用户已钉死，自动分不覆盖
    info = score(text)
    if info.value <= 1.0:
        return info
    lifecycle.set_importance(trace_id, info.value, scope_id,
                             reasons=info.reasons, source=source)
    return info
