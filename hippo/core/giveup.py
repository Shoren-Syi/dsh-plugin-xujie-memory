# -*- coding: utf-8 -*-
"""M22 弃权系统：标签协议 / 流式拦截改写 / 能力边界记录。

三条硬约束落在这里：

1. **标签零泄漏**：`<giveup .../>` 只存在于模型输出到代理层这一段，
   用户侧永远看不到。流式剥离必须做跨块缓冲（逐块正则会在
   `<giv` / `eup` 分块时漏网）。
2. **弃权不算失败**：协议文本要明确说"放弃不算失败"，否则模型宁可
   硬编（幻觉）也不弃权——那比弃权糟得多。
3. **retry_alt 不告诉用户**：换法再试是内部动作，用户只该看到结果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LEVELS = ("retry_alt", "ask_help", "partial", "decline")

# 放 A 段末尾。**协议是功能不是风格**——用户写自己的人设文件时也照样
# 追加，不然弃权出口就没了（_persona 里做，这里只管文本）。
GIVEUP_PROTOCOL = """## 你可以放弃

放弃不算失败。如果一件事你确实做不到，输出
<giveup level="..." reason="..."/> 然后停止。
level: retry_alt(换法再试) / ask_help(需要信息) / partial(只能做一部分) / decline(做不到)
放弃时必须给 reason，一句话说清卡在哪。"""


# ============================================================ 解析
# 宽松匹配：模型会写成 <giveup level="decline" reason="..."/>、
# <giveup level=decline reason=...>、大小写混用、单引号、空格不规整。
_TAG = re.compile(
    r"<\s*giveup\b(?P<attrs>[^>]*?)/?\s*>",
    re.IGNORECASE,
)
_ATTR = re.compile(
    r"""(?P<k>level|reason)\s*=\s*(?P<q>["']?)(?P<v>.*?)(?P=q)(?=\s+\w+\s*=|$|\s*/)""",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class Giveup:
    level: str
    reason: str
    raw: str          # 原始标签文本（日志用）
    valid: bool = True
    problem: str = ""  # valid=False 时说明原因


def _attrs(blob: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR.finditer(blob.strip()):
        out[m.group("k").lower()] = m.group("v").strip()
    return out


def parse_tag(text: str) -> Giveup | None:
    """提取第一个 giveup 标签。没有返回 None。

    校验（手册 M22.1 验收 #2/#3）：
      - level 必须在枚举内，非法 → 降级为 decline 并记 problem
      - decline 必须带 reason，缺失 → 补默认 reason 并记 problem
        （宁可给用户一句"我说不清卡在哪"，也不能让标签漏出去）
    """
    m = _TAG.search(text or "")
    if not m:
        return None
    a = _attrs(m.group("attrs") or "")
    level = (a.get("level") or "").lower()
    reason = a.get("reason") or ""
    raw = m.group(0)

    if level not in LEVELS:
        return Giveup("decline", reason or "模型没给出明确原因",
                      raw, valid=False, problem=f"非法 level: {level!r}")
    if level == "decline" and not reason:
        return Giveup(level, "模型没给出明确原因", raw,
                      valid=False, problem="decline 缺 reason")
    return Giveup(level, reason, raw)


def strip_tags(text: str) -> str:
    """把一个响应里所有标签抹掉（非流式路径）。"""
    return _TAG.sub("", text or "")


def audit(text: str) -> list[str]:
    """剥离后校验：返回残留问题列表（空 = 干净）。验收 #1 的口径。"""
    problems: list[str] = []
    low = (text or "").lower()
    for needle in ("<giveup", "</giveup", "reason="):
        if needle in low:
            problems.append(f"残留 {needle}")
    return problems


# ============================================================ 流式剥离
# 需要 hold back 的最长前缀 = len("<giveup") —— 块边界恰好切在标签
# 中间时，最后 7 个字符必须等下一块才能判断。
_HOLD = len("<giveup")
_OPEN = "<giveup"


class StreamFilter:
    """流式剥离器。feed(chunk) 返回**可以立刻转发**的文本。

    状态机只有两种模式：
      正常模式——输出到 buf[:-7]，保留尾部 7 字符（可能是标签前缀）
      标签模式——遇到 `<giveup` 后停止输出，等 `>` 闭合，吞掉整个标签

    为什么不用逐块正则替换：块边界会切碎标签（`<giv` | `eup ...`），
    逐块处理会漏网。跨块缓冲是唯一正确解。
    """

    def __init__(self) -> None:
        self.buf = ""
        self.tags: list[Giveup] = []      # 已捕获的标签（按出现顺序）
        self.held = 0                     # 当前 hold back 的字符数（延迟观测）
        self.max_held = 0
        self.incomplete = False           # 流结束时还有未闭合标签

    # -- 内部：从 buf 头部尽量吐出安全文本
    def _drain(self, final: bool) -> str:
        out: list[str] = []
        while True:
            idx = self.buf.find("<")
            if idx < 0:
                if final:
                    out.append(self.buf)
                    self.buf = ""
                else:
                    keep = _HOLD if len(self.buf) >= _HOLD else len(self.buf)
                    cut = len(self.buf) - keep
                    out.append(self.buf[:cut])
                    self.buf = self.buf[cut:]
                break

            # '<' 之前的部分是安全的
            out.append(self.buf[:idx])
            self.buf = self.buf[idx:]

            low = self.buf.lower()
            # 可能是标签前缀（"<" / "<g" / ... / "<giveup"）→ 等更多
            if not final and _OPEN.startswith(low[:len(_OPEN)]) and len(low) < _OPEN.__len__():
                break
            if low.startswith(_OPEN):
                end = self.buf.find(">")
                if end < 0:
                    if final:
                        # 未闭合：吞掉（零泄漏优先），记一笔
                        self.incomplete = True
                        self.buf = ""
                    break
                tag_text = self.buf[:end + 1]
                self.buf = self.buf[end + 1:]
                g = parse_tag(tag_text)
                if g is not None:
                    self.tags.append(g)
                continue
            # 普通 '<'（不是标签）→ 吃一个字符继续
            out.append(self.buf[:1])
            self.buf = self.buf[1:]
        return "".join(out)

    def feed(self, chunk: str) -> str:
        self.buf += chunk or ""
        text = self._drain(final=False)
        self.held = len(self.buf)
        self.max_held = max(self.max_held, self.held)
        return text

    def flush(self) -> str:
        text = self._drain(final=True)
        return text

    @property
    def tail(self) -> float:
        """平均延迟字符数的上界（验收 #4：<30）。"""
        return float(self.max_held)


# ============================================================ 改写
def rewrite(g: Giveup, delivered: str = "") -> str | None:
    """按 level 改写成人话。返回 None 表示**不输出任何东西**（内部重试）。

    手册四档：
      retry_alt  内部重试，用户无感 → None
      ask_help   "这块我卡住了——我需要你告诉我 X，不然我只能瞎猜。"
      partial    正常交付 + 末尾"另外 Y 部分我没做，因为 Z。"
      decline    "这个我做不到。原因：{reason}。"
    """
    if g.level == "retry_alt":
        return None
    if g.level == "ask_help":
        return f"这块我卡住了——我需要你告诉我 {g.reason}，不然我只能瞎猜。"
    if g.level == "partial":
        body = (delivered or "").rstrip()
        tail = f"另外，{g.reason}"
        return f"{body}\n\n{tail}" if body else tail
    return f"这个我做不到。原因：{g.reason}。"


def finish(delivered: str, filter_: StreamFilter) -> str:
    """流结束的统一收口：flush + 取第一个标签改写 + 残留校验失败则兜底。"""
    rest = filter_.flush()
    text = (delivered or "") + rest
    if filter_.tags:
        g = filter_.tags[0]
        rewritten = rewrite(g, text)
        # retry_alt：用户什么都不该看到（内部重试由调用方负责）
        text = "" if rewritten is None else rewritten
    text = strip_tags(text)
    if audit(text):
        # 理论上到不了这里（前面都剥过）。真到了就整段丢掉——
        # 半截标签比没有回复更糟。
        return "这个我暂时答不上来。"
    return text


# ============================================================ M22.3 记录
def record(store, session: str, g: Giveup, task: str, *,
           now_ts: int | None = None, threshold: int = 5) -> tuple[int, str]:
    """弃权落一条 kind=giveup 的 cold 事件。返回 (event_id, 提醒文本)。

    **不进常规注入**（tier=3 + 检索排除表）。能力边界是"统计给用户看的
    东西"，不是"模型该在对话里提的事"——每一次 prompt 都塞一行
    "我上次做不了 X"，等于让 AI 反复道歉。

    提醒只在同一 reason 攒够 threshold 次时给一次（手册验收 #3）。
    """
    from .keywords import clean_keywords

    reason = g.reason or "未说明"
    eid = store.add_event(
        session=session,
        raw=f"（弃权）{task}",
        summary=f"做不了：{task} / 原因：{reason}",
        keywords=clean_keywords([task]),
        kind="giveup",
        tier=3,                       # cold：不参与常驻注入
        w0=0.1,
        source="ai",                  # 是 AI 的弃权，不是用户说的话
        giveup_level=g.level,
        **({"event_time": now_ts} if now_ts is not None else {}),
    )
    warn = ""
    try:
        rows = store.giveup_stats(session=session, top=100)
        n = sum(r["n"] for r in rows if r["reason"] == reason)
        if store.giveup_warn_once(session, reason, n, threshold):
            warn = (f"这类事它已经做不了 {n} 次了（{reason}）——"
                    f"要不要换个方式？")
    except Exception:      # noqa: BLE001
        pass                # 提醒失败不影响主流程；记录已经落库
    return eid, warn


def format_stats(rows: list[dict]) -> str:
    """`hippo stats --giveup` 的输出体（手册样例格式）。"""
    if not rows:
        return "（还没有弃权记录）"
    return "\n".join(f"{r['level']}: {r['reason']} ({r['n']})" for r in rows)
