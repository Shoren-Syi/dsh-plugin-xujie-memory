"""会话识别（M2.3）。

一次"会话"是记忆的主键，所以这个函数的错误会污染整个库。
三级兜底，优先级从高到低：

  1. 请求头 `X-Hippo-Session`（名字可配）—— 显式，最可靠
  2. body.user —— OpenAI 有这个字段，SillyTavern 之类常填
  3. sha256(第一条 user 消息)[:12] —— 兜底

第 3 条有个刻意为之的副作用：**同一个人开新窗口会落到同一个 session**。
这是特性不是 bug —— 记忆本就该跨窗口延续。想隔离就显式传 header。

反面教材：拿整个 messages 的哈希当 session。每轮 messages 都在变，
结果是每轮一个新会话，记忆永远为 0。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from ..core.turns import text_of as _text_of

_UNSAFE = re.compile(r"[^A-Za-z0-9_.:\-]")
_MAX_LEN = 64


def _clean(value: str) -> str:
    return _UNSAFE.sub("_", value.strip())[:_MAX_LEN]


def first_user_text(messages: list[dict[str, Any]] | None) -> str:
    for m in messages or []:
        if not isinstance(m, Mapping):
            continue
        if m.get("role") == "user":
            return _text_of(m.get("content"))
    return ""


def identify(
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    *,
    header_name: str = "X-Hippo-Session",
    default: str = "main",
) -> tuple[str, str]:
    """返回 (session_id, 来源)。来源要记日志，调不通时一眼看出是谁的锅。"""
    raw = headers.get(header_name) or headers.get(header_name.lower())
    if raw and raw.strip():
        return _clean(raw), "header"

    user = body.get("user")
    if isinstance(user, str) and user.strip():
        return _clean(user), "body.user"

    text = first_user_text(body.get("messages"))
    if text:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        return f"auto-{digest}", "first_user_hash"

    return default, "default"
