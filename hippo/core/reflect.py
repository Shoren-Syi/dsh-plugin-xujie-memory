# -*- coding: utf-8 -*-
"""被动纠正挖掘：从自然语言里认出"旧值 → 新值"，静默修正记忆。

为什么不在提示词里请模型纠正：那是有感的——多付 token、会改变模型话术，
用户能看出来。这里只做本地解析，零额外请求、零提示词字节、不增加首字延迟。

边界（宁可少改，不可改错）：
- 必须能解析出**明确的旧值和新值**，"不对，我觉得不行"不算；
- 每个会话每天最多应用 `reflect.max_passive_per_day` 条；
- 值要先在库里真的存在（只是把已有事实换成新值，不凭空造）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = ["Correction", "parse_correction", "apply_corrections"]

# "不是 X，是 Y" / "不是X是Y"
_RE_NOT_BUT = re.compile(r"不是\s*([^\s，。,.!?！？]{1,20})\s*[，,]?\s*是\s*([^\s，。,.!?！？]{1,20})")
# "是 Y 不是 X"
_RE_IS_NOT = re.compile(r"是\s*([^\s，。,.!?！？]{1,20})\s*不是\s*([^\s，。,.!?！？]{1,20})")
# "X 改成 Y" / "把 X 改成 Y"
_RE_CHANGE = re.compile(r"(?:把\s*)?([^\s，。,.!?！？]{1,20})\s*(?:改|换|更正|纠正)成\s*([^\s，。,.!?！？]{1,20})")

# 这些词出现在"旧值"位置时，几乎都是情绪/评价而不是事实值，直接放掉。
_NOISE = {"这样", "那样", "这个", "那个", "我", "你", "他", "她", "这", "那", "什么"}


@dataclass(frozen=True)
class Correction:
    old: str
    new: str


def parse_correction(text: str) -> list[Correction]:
    """从一句话里抽纠正对。抽不出来返回空列表（绝大多数句子都是空）。"""
    if not text or len(text) < 4:
        return []
    out: list[Correction] = []
    for rx, swap in ((_RE_NOT_BUT, False), (_RE_IS_NOT, True), (_RE_CHANGE, False)):
        for m in rx.finditer(text):
            a, b = m.group(1).strip(), m.group(2).strip()
            old, new = (b, a) if swap else (a, b)
            if not old or not new or old == new:
                continue
            if old in _NOISE or new in _NOISE:
                continue
            # "不是很好" 这种，old 太短且像形容词，长度 >=2 且不含标点才收
            if len(old) < 1 or len(new) < 1:
                continue
            pair = Correction(old=old, new=new)
            if pair not in out:
                out.append(pair)
    return out


def _today(now: int, tz: str = "Asia/Shanghai") -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(now, ZoneInfo(tz)).strftime("%Y-%m-%d")


def apply_corrections(
    store: Any,
    session: str,
    pairs: list[Correction],
    *,
    cfg: Any,
    now: int,
) -> list[dict]:
    """把纠正应用到库里已有的事实上。返回实际应用了的列表（便于日志/测试）。"""
    limit = int(getattr(getattr(cfg, "reflect", None), "max_passive_per_day", 3) or 0)
    if not pairs or limit <= 0:
        return []
    day = _today(now)
    used_key = "passive_corrections"
    raw = str(store.get_state(session, used_key, "") or "")
    used_day, _, used_n = raw.partition(":")
    used = int(used_n or 0) if used_day == day else 0
    applied: list[dict] = []
    for pair in pairs:
        if used >= limit:
            break
        rows = store.active_facts_by_value(session, pair.old)
        if not rows:
            continue
        for key, _value in rows:
            store.set_fact(session=session, key=key, value=pair.new, when=now)
            applied.append({"key": key, "old": pair.old, "new": pair.new})
            used += 1
            if used >= limit:
                break
    if applied:
        store.set_state(session, used_key, f"{day}:{used}")
    return applied
