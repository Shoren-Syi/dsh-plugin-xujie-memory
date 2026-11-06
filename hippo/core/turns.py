"""会话落库与切片（M3）。

两个问题，两个解法：

**问题 1：客户端每轮发完整历史，哪几条是新增的？**
用「最长公共后缀」对齐已知 turns 与请求 messages 的尾部。
不用「找最后一条 assistant 在哪」，因为一句话重复出现时会错位。

**问题 2：抽取器一次吃多少？**
按轮数与 token 双重上限切窗口。切太碎 → 上下文不全，抽不出关系；
切太大 → 单次成本高、且越界的内容会被模型忽略。

这里所有函数都是**纯函数**（除了显式接 store 的那两个），
方便 M5 的抽取器在测试里直接喂构造数据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .models import Turn

# 单条消息的硬上限。防的是"用户粘贴一整本小说"这种把库撑爆的情况。
# 64k 字符 ≈ 3 万汉字，正常对话永远碰不到。
MAX_CHARS = 64_000

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def text_of(content: Any) -> str:
    """content 可能是 str，也可能是 [{"type":"text","text":...}] 的分段结构。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def estimate_tokens(text: str) -> int:
    """粗估 token，**故意偏高**。

    中文 1 字 ≈ 1.5 token，英文 4 字符 ≈ 1 token，再乘 1.3 安全系数。
    宁可多算：低估的后果是超窗 400，高估的后果只是窗口小一点。
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return int((cjk * 1.5 + other / 4.0) * 1.3) + 1


def _common_prefix_len(a: Sequence[Any], b: Sequence[Any]) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return n


def plan_delta(
    messages: Sequence[Mapping[str, Any]],
    known: Sequence[Turn],
) -> list[tuple[str, str]]:
    """请求 messages 里，库中尚未记录的 (role, content)。

    **用最长公共前缀，不是公共后缀。** 这一点我想错过一次：

    正常新一轮的 messages 是 `[u0, a1, u2]`，库里的 known 是 `[u0, a1]`。
    公共**后缀**比对 `a1` 与 `u2`，永远不匹配，于是每轮都判定"全是新的"。
    看起来没错（因为最终只取最后一条 user），但那是因为另一层兜底在救，
    而不是这层算法在工作。

    前缀才符合事实：客户端把历史按顺序重发，known 应该是 msgs 的前缀。

    客户端截断历史（只发最近 N 条）时前缀会失配，delta 变成全量——
    这仍然安全，因为 pick_user_text 只取最后一条 user。
    """
    msgs = [
        (m.get("role"), text_of(m.get("content")))
        for m in messages
        if isinstance(m, Mapping) and m.get("role") in ("user", "assistant")
    ]
    known_pairs = [(t.role, t.content) for t in known]
    matched = _common_prefix_len(known_pairs, msgs)
    return msgs[matched:]


def pick_user_text(
    messages: Sequence[Mapping[str, Any]],
    known: Sequence[Turn],
) -> str:
    """本轮真正要记的用户输入。

    **只取最后一条**，不取整个 delta。这是刻意的冗余防线：
    如果客户端改写了历史（重生成、编辑消息），公共后缀会失配，
    delta 会变成整段历史。全写进去就是几百条重复记忆。
    只取最后一条，最坏情况是漏记一条，而不是污染整个库。
    """
    delta = plan_delta(messages, known)
    for role, content in reversed(delta):
        if role == "user" and content.strip():
            return content
    # delta 为空（客户端重发同一轮）时，退回到整个 messages 的最后一条 user
    for m in reversed(list(messages)):
        if isinstance(m, Mapping) and m.get("role") == "user":
            return text_of(m.get("content"))
    return ""


def clip(text: str, limit: int = MAX_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "…[truncated]", True


# ---------------------------------------------------------------- 窗口
@dataclass
class Window:
    """一个待抽取的窗口。

    has_pending=True 表示「还没攒够，先别抽」——要么太短，要么最后一条
    是 user（说明 assistant 的回复还没落库）。M5 见到它应该跳过。
    """

    session: str
    idxs: list[int]
    token_est: int
    has_pending: bool
    turns: list[Turn] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """对话是否"收得住"：最后一条是 assistant（AI 已经回过了）。

        和 has_pending 的区别很重要：
          has_pending = 不完整 **或** 太短（两个原因合在一个标志里）
          complete    = 只看完整性
        M5.4 必须分清：不完整 → 下次还要抽（不标记）；
        太短但完整 → 直接放弃（标记），否则永久占着 pending。
        """
        return bool(self.turns) and self.turns[-1].role != "user"

    @property
    def start_idx(self) -> int:
        return self.idxs[0]

    @property
    def end_idx(self) -> int:
        return self.idxs[-1]

    def text(self) -> str:
        """拼成抽取器直接可读的形式。"""
        return "\n".join(f"{t.role}: {t.content}" for t in self.turns)


def slice_windows(
    turns: Sequence[Turn],
    *,
    session: str = "",
    per_window: int = 5,
    max_tokens: int = 1200,
    min_tokens: int = 60,
) -> list[Window]:
    """把未抽取的 turns 切成窗口。

    **切点只在 user 边界。** 这一点第一版写错过：我在"第 5 条 user 刚进来"
    时切，结果那一轮的 assistant 被甩进下一个窗口——抽取器看到的是
    "用户说了话，没人回"，关系全断。

    正确做法是等到**下一轮 user 进来时**才收尾上一窗。代价是单轮超长时
    窗口会超 max_tokens 一点；收益是每一窗都是完整对话，这个更值钱。
    """
    windows: list[Window] = []
    buf: list[Turn] = []
    tok = 0
    rounds = 0

    def flush(pending: bool) -> None:
        nonlocal buf, tok, rounds
        if not buf:
            return
        windows.append(Window(
            session=session or buf[0].session,
            idxs=[t.idx for t in buf],
            token_est=tok,
            has_pending=pending,
            turns=list(buf),
        ))
        buf, tok, rounds = [], 0, 0

    for t in turns:
        if t.role == "user":
            # 新的一轮要开始了：先看看上一窗是不是该收
            # 连续用户消息属于尚未得到回复的一组，不能在组中间切开。
            if buf and buf[-1].role != "user" and (rounds >= per_window or tok >= max_tokens):
                flush(False)
            rounds += 1
        buf.append(t)
        tok += estimate_tokens(t.content)

    if buf:
        # 尾巴：太短、或最后一条是 user（回复还没到），都不该抽
        flush(tok < min_tokens or buf[-1].role == "user")

    return windows


# ---------------------------------------------------------------- 落库
def build_items(
    user_text: str,
    assistant_text: str,
    *,
    partial: bool = False,
    max_chars: int = MAX_CHARS,
) -> list[tuple[str, str, int]]:
    """一轮对话 → turns 行。

    partial=True 表示 assistant 的内容不完整（客户端断开 / 上游报错）。
    这条信息很重要：M15 巩固时不能拿半截话当事实。
    """
    items: list[tuple[str, str, int]] = []
    if user_text.strip():
        u, cut = clip(user_text, max_chars)
        items.append(("user", u, 1 if cut else 0))
    if assistant_text.strip():
        a, cut = clip(assistant_text, max_chars)
        items.append(("assistant", a, 1 if (cut or partial) else 0))
    elif partial and items:
        # 一个字都没收到：只记 user，但把它标成 partial，
        # 避免 M5 把「用户说了话但没有回复」误判成一段完整对话
        role, content, _ = items[0]
        items[0] = (role, content, 1)
    return items


def record_pair(
    store_path: Any,
    session: str,
    user_text: str,
    assistant_text: str,
    *,
    partial: bool = False,
    max_chars: int = MAX_CHARS,
) -> list[int]:
    """写一轮对话的便捷入口（CLI / 测试 / 批处理用）。

    请求路径不走这里——代理要在**同一个连接**里同时写 turns 和 metrics，
    开两次库会多一次 fsync（实测 5ms → 2.5ms）。
    """
    from .store import Store

    items = build_items(user_text, assistant_text, partial=partial, max_chars=max_chars)
    if not items:
        return []
    with Store.open(store_path) as store:
        return store.add_turns(session, items)


def plan_windows(store: Any, session: str, **kwargs: Any) -> list[Window]:
    """便捷入口：直接从库里取未抽取的 turns 并切窗口。"""
    return slice_windows(store.pending_turns(session), session=session, **kwargs)
