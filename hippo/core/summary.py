"""摘要校验（M6.2）。

摘要是三级表示里的**中间层**：检索先看关键词，命不中就靠摘要模糊匹配，
最后才读原文。所以摘要的职责不是"好看"，是"读这一句就知道要不要展开原文"。

四个可机器判定的失败模式：
  1. 太长（>70 字）—— 它不是摘要，是原文的劣质副本，还挤占 prompt 预算。
  2. 丢实体 —— 原文里出现了"老王"，摘要里没有。检索时这条永远挂不到老王名下。
  3. 第一人称 —— 摘要的读者是三个月后的 AI，"我"指谁不明确。
  4. 空 —— 等于没有中间层。

保真度（"有没有说错"）机器判不了，只能人看。这里只保证"不说错"的必要条件：
实体在、长度合适、人称对。**不做的事**：用摘要去改摘要。那样每修一次就掉一层信息，
三轮之后摘要和原文没关系了，而且不会有任何报错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

MAX_LEN = 70          # 超过就重写一次
SOFT_LEN = 60         # prompt 里要求的长度
TRUNCATE_AT = 68      # 重写失败后截断到这里，留一个省略号的位置

# 第三人称视角里不该出现的自称。注意只认"我"，不认"我们"以外的复数——
# "我们"在转述里是合理的（"用户和老王一起去了"），"我"不是。
_FIRST_PERSON = re.compile(r"我(?!们)")


@dataclass(frozen=True)
class SummaryIssue:
    idx: int          # 在 result.events 里的下标
    kinds: tuple[str, ...]
    detail: str

    def text(self) -> str:
        return "、".join(self.kinds) + (f"（{self.detail}）" if self.detail else "")


def too_long(summary: str) -> bool:
    return len((summary or "").strip()) > MAX_LEN


def missing_entities(summary: str, raw: str, entities: Iterable[str]) -> list[str]:
    """原文里出现、摘要里没有的实体名。

    只查"原文里真的有"的实体：模型可能给一个原文没提的人（幻觉），
    那种情况该在 M5 的锚定阶段就拦住，不该在这里二次惩罚。
    """
    s = summary or ""
    out = []
    for name in entities:
        n = (name or "").strip()
        if len(n) >= 2 and n in (raw or "") and n not in s:
            out.append(n)
    return out


def is_first_person(summary: str) -> bool:
    return bool(_FIRST_PERSON.search(summary or ""))


def issues_of(idx: int, summary: str, raw: str,
              entities: Sequence[str] = ()) -> SummaryIssue | None:
    """一条事件的摘要问题；没有问题返回 None。"""
    s = (summary or "").strip()
    kinds: list[str] = []
    detail: list[str] = []

    if not s:
        kinds.append("empty")
    else:
        if too_long(s):
            kinds.append("too_long")
            detail.append(f"{len(s)}字")
        miss = missing_entities(s, raw, entities)
        if miss:
            kinds.append("missing_entity")
            detail.append("缺" + "/".join(miss))
        if is_first_person(s):
            kinds.append("first_person")

    return SummaryIssue(idx, tuple(kinds), "，".join(detail)) if kinds else None


def scan(events: Sequence[object]) -> list[SummaryIssue]:
    """扫一遍抽取结果，返回所有需要修的摘要。"""
    out = []
    for i, ev in enumerate(events):
        names = [getattr(r, "name", "") for r in (getattr(ev, "entities", None) or [])]
        issue = issues_of(i, getattr(ev, "summary", ""), getattr(ev, "raw", ""), names)
        if issue:
            out.append(issue)
    return out


def truncate(summary: str, limit: int = TRUNCATE_AT) -> str:
    """重写失败后的退路：截断到句读处，加省略号。

    优先在标点处断，因为"用户昨天下午搬家了，从朝阳搬到海…"比
    "用户昨天下午搬家了，从朝阳搬到海淀房" 更像话。
    """
    s = (summary or "").strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    for ch in "。！？；，、":
        p = cut.rfind(ch)
        if p >= limit // 2:
            return cut[:p + 1].rstrip() + "…"
    return cut.rstrip() + "…"


def keep_rate(summaries: Sequence[str], raws: Sequence[str],
              entities: Sequence[Sequence[str]]) -> float:
    """实体保留率：全部"原文里出现过的实体"中，有多少也出现在摘要里。

    分母是"实体出现次数"而不是"条数"：一条事件里提到三个人、摘要只保住一个，
    应该算 1/3，不是 0/1。
    """
    total = kept = 0
    for s, raw, names in zip(summaries, raws, entities):
        for n in names:
            n = (n or "").strip()
            if len(n) < 2 or n not in (raw or ""):
                continue
            total += 1
            if n in (s or ""):
                kept += 1
    return round(kept / total, 4) if total else 1.0


# ---------------------------------------------------------------- 数字保真
# 摘要是"读一句就知道要不要展开原文"，所以**最容易被丢、又最不能丢**的是数字：
# 时间、金额、年龄、年限。实体丢了这条记忆挂不到人身上，
# 数字丢了它挂到了人身上但内容是错的——后者更难发现。
#
# 两类必须排除，否则指标会骗人（这两条是看过真实输出后才加的，
# 原始的朴素口径是 68.4%，报告里两个数都写了）：
#   1. 量词性数字：一次、一点、一个——它们是语气，不是信息。
#   2. 换算写法：原文"百分之三十"、摘要"30%"——这是正确转写，不是丢失。
_FILLER_NUM = frozenset({
    "一次", "一点", "一些", "一下", "一个", "一样", "一直", "一起",
    "一份", "一边", "一阵", "一段",
})

_NUM = re.compile(
    r"百分之[一二三四五六七八九十百零\d]+"
    r"|[一二三四五六七八九十\d]+成(?![员本为年长])"
    r"|\d+(?:[.,]\d+)?%?"
    r"|[一二三四五六七八九十百千万两]+(?:点|岁|块|元|千|万|号|月|日|天|年|个|次|小时|分钟|周)"
)

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(text: str) -> int | None:
    """把"三十""一百二十"转成整数。只处理 0~999，够用了。"""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total, unit_val = 0, 0
    for ch in text:
        if ch in _CN_DIGIT:
            unit_val = _CN_DIGIT[ch]
        elif ch == "十":
            total += (unit_val or 1) * 10
            unit_val = 0
        elif ch == "百":
            total += (unit_val or 1) * 100
            unit_val = 0
        else:
            return None
    return total + unit_val


def _accepted_forms(token: str) -> list[str]:
    """一个数字的所有"算它保留"的写法。

    中文里同一个数至少有四种说法：30% / 百分之三十 / 三成 / 三折（折扣反着算，不算）。
    任何一种出现都算保留——我们关心的是"信息还在不在"，不是"写法一样不一样"。
    """
    forms = [token]
    if token.endswith("成") and not token.endswith(("成本", "成员")):
        n = _cn_to_int(token[:-1])
        if n is not None:
            forms += [f"{n * 10}%", f"百分之{_int_to_cn(n * 10)}", f"{n * 10}％"]
    if token.startswith("百分之"):
        n = _cn_to_int(token[3:])
        if n is not None:
            forms += [f"{n}%", f"{n}％"]
            if n % 10 == 0 and 0 < n < 100:
                forms.append(f"{_int_to_cn(n // 10)}成")
    elif token.endswith("%"):
        forms.append(token)
        n = token.rstrip("%")
        if n.isdigit():
            cn = _int_to_cn(int(n))
            if cn:
                forms.append("百分之" + cn)
    return forms


def _int_to_cn(n: int) -> str:
    if n < 10:
        return "零一二三四五六七八九"[n]
    if n < 20:
        return "十" + ("零一二三四五六七八九"[n % 10] if n % 10 else "")
    if n < 100:
        return "零一二三四五六七八九"[n // 10] + "十" + \
               ("零一二三四五六七八九"[n % 10] if n % 10 else "")
    return ""


def numbers_of(text: str, *, keep_filler: bool = False) -> list[str]:
    out = [m.group(0) for m in _NUM.finditer(text or "")]
    if keep_filler:
        return out
    return [t for t in out if t not in _FILLER_NUM]


def number_keep_rate(summaries: Sequence[str], raws: Sequence[str],
                     *, keep_filler: bool = False) -> float:
    """原文里的数字，有多少以某种等价写法出现在摘要里。

    这是保真度的第二个可机器判定的代理指标（第一个是实体）。
    两个都过、长度也合规，才谈得上"摘要没写错"；
    "摘要写得好不好"仍然只能人看。
    """
    total = kept = 0
    for s, raw in zip(summaries, raws):
        for n in numbers_of(raw, keep_filler=keep_filler):
            total += 1
            if any(f in (s or "") for f in _accepted_forms(n)):
                kept += 1
    return round(kept / total, 4) if total else 1.0


# ---------------------------------------------------------------- 修复 prompt
REPAIR_HEAD = """你在修一份抽取结果的"摘要"字段。逐条按原文重写，不要参考旧摘要。

硬性要求：
1. 每条 60 字以内，一句话说完"谁-做了什么-关键细节"。
2. 第三人称。用户本人写"用户"，不要写"我"。
3. **原文里出现的人名必须出现在摘要里**，一个都不能少。
4. 只写摘要，不要加评价、不要加推测。

只输出一个 json 对象：{"summaries":[{"i":0,"summary":"..."}]}

待修条目：
"""


def repair_block(events: Sequence[object], issues: Sequence[SummaryIssue],
                 raw_limit: int = 200) -> str:
    """把要修的条目拼成一段紧凑文本。

    给的是 **raw**，不是旧摘要——旧摘要正是要丢掉的东西。
    raw 截到 200 字：摘要只要求 60 字，200 字足够判断，再多就是白花钱。
    """
    lines = []
    for it in issues:
        ev = events[it.idx]
        raw = (getattr(ev, "raw", "") or "").replace("\n", " ")[:raw_limit]
        names = [getattr(r, "name", "") for r in (getattr(ev, "entities", None) or [])]
        lines.append(
            f"[{it.idx}] 原文：{raw}\n"
            f"     问题：{it.text()}\n"
            f"     必须出现：{'、'.join(n for n in names if n) or '（无）'}")
    return "\n".join(lines)
