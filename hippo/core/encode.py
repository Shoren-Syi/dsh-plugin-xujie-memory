"""抽取调用（M5.2）：把窗口送进模型，拿回结构化记忆。

这个文件只做三件事，每一件都有明确的失败出口：

    1. 组 prompt（含已认识的人，避免重复建实体）
    2. 调模型，JSON 不合法就重试一次，还不行就走降级
    3. 降级时**至少把原文写成一条 cold 事件**

设计上的三个"不做"：
  - 不流式。抽取是后台活，用户看不见，流式没有意义，反而多一层解析。
  - 不在失败时抛异常给上层。抽取失败不该让聊天挂掉，它只该让记忆变少。
  - 不重试超时以外的网络错误。429/5xx 由上层熔断管，
    在这里重试只会让一次抖动变成三次。

为什么重试要带上"上次输出"：只发一句"请重新输出 JSON"，
模型大概率重复同一个错误（相同 prompt + 相同温度 ≈ 相同输出）。
把它的错误输出放进 assistant 位置，它才能看见自己错在哪。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlsplit

import httpx

from ..config import Config
from ..log import get_logger
from .schema import (
    ExtractResult, ParseError, anchor_raw, load_template, parse_result, render_prompt,
)
from .summary import (
    REPAIR_HEAD, repair_block, scan, truncate,
)
from .telemetry import Span
from .turns import Window

log = get_logger("encode")

# 重试时追加的提示。措辞要短——它每次都要花 token，而且模型对长指令的
# 遵从度反而下降。
_RETRY_HINT = (
    "你上一次的输出不是合法的 json（或者 JSON 没闭合）。"
    "请只输出一个 json 对象，不要任何解释、不要 markdown 围栏。"
)


@dataclass
class EncodeOutcome:
    """一次抽取的结果。字段多，但每个都有人用：

    status 进 extract_runs；raw_text 进日志（排查"它到底吐了什么"）；
    attempts 用来发现"最近重试率变高了"；verbatim_fail 是 prompt 退化的先行指标。
    """

    # ★ 初值不能是 "ok"。这个字段在 encode() 末尾用
    #   `if out.status != "ok": out.status = "degraded"` 收口，
    #   如果初值就是 "ok"，失败路径永远不会触发——整个降级逻辑静默失效。
    #   （这个坑真实发生过，见施工记录事故 19。）
    status: str = "pending"            # pending / ok / degraded
    result: ExtractResult | None = None
    raw_text: str = ""
    attempts: int = 1
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 命中上下文缓存的前缀 token。不吃这个数，成本会按"全未命中"算——
    # mimo 的命中价是 1/50，漏记账等于把成本高估 50 倍，
    # 然后你会基于一个假的成本数字去做错误的取舍。
    cached_tokens: int = 0
    # 模型自报的思考 token。关思考时应为 0，不为 0 说明开关没生效。
    reasoning_tokens: int = 0
    cost: float = 0.0
    latency_ms: int = 0
    error: str | None = None
    # raw 锚定失败的条数：模型改写了原文。这个数长期上升 = prompt 在退化
    verbatim_fail: int = 0
    # 触发了几次摘要修复（0 或 1）。它直接乘在成本上：修一次 = 多一次调用。
    # 不记这个数，你会看到"成本涨了但不知道为什么"。
    repairs: int = 0
    # 修复前的所有问题类型。用来判断该改 prompt 还是该改阈值。
    repair_kinds: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.result is not None


def build_conversation(window: Window) -> str:
    """窗口 → prompt 里的对话段。

    截断掉 assistant 的超长回复：抽取关心的是用户说了什么，
    而 AI 一次能吐 3000 字，会把窗口撑爆、把用户的 20 个字淹掉。
    """
    lines: list[str] = []
    for t in window.turns:
        content = t.content.strip()
        if not content:
            continue
        if t.role == "assistant" and len(content) > 400:
            content = content[:400] + "…（后略）"
        lines.append(f"{t.role}: {content}")
    return "\n".join(lines)


def build_known_entities(entities: Sequence[Any], limit: int = 40) -> str:
    """已认识的人 → prompt 里的表。

    别名一起给出，是因为模型看到"王哥"时需要一个理由才会归到"老王"名下。
    只给正名，它会老老实实新建一个"王哥"。
    """
    if not entities or limit <= 0:
        return "（暂无）"
    lines = []
    for e in list(entities)[:limit]:
        aliases = [a for a in (getattr(e, "aliases", None) or []) if a]
        suffix = f"（别名：{'、'.join(aliases)}）" if aliases else ""
        lines.append(f"- {e.canonical}{suffix}")
    return "\n".join(lines) if lines else "（暂无）"


class Encoder:
    """一次会话一个实例。持有 client 与 api_key。"""

    def __init__(
        self,
        cfg: Config,
        client: httpx.AsyncClient,
        api_key: str,
        *,
        template: str | None = None,
    ):
        self.cfg = cfg
        self.client = client
        self.api_key = api_key
        self._template = template

    # ---------------------------------------------------------------- 组装
    def build_messages(self, window: Window, known: str = "（暂无）") -> list[dict[str, str]]:
        conversation = build_conversation(window)
        prompt = render_prompt(
            conversation,
            known_entities=known,
            now_hint=_now_hint(self.cfg.timezone),
            template=self._template,
        )
        return [{"role": "user", "content": prompt}]

    async def chat(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
        """通用对话调用（M15 摘要重生成用）。

        复用 `_post` 而不是另起一条 HTTP 路径：认证、超时、错误归一化都在那里，
        复制一份的后果是"改超时的时候一定会漏掉一份"，而漏掉的那份
        要到线上才发现（抽取改好了，巩固还在用旧超时，夜里批量超时失败）。
        """
        return await self._post(messages)

    def _payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        ec = self.cfg.extract
        payload: dict[str, Any] = {
            "model": self.cfg.deepseek.extract_model,
            "messages": messages,
            "temperature": ec.temperature,
            "max_tokens": ec.max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        # 只对官方 Flash 默认关闭思考；其他兼容服务可能不接受该参数。
        thinking = (self.cfg.deepseek.thinking or "").strip().lower()
        if (self.cfg.deepseek.thinking == ""
                and urlsplit(self.cfg.deepseek.base_url).hostname == "api.deepseek.com"
                and self.cfg.deepseek.extract_model == "deepseek-flash"):
            thinking = "disabled"
        if thinking in ("enabled", "disabled"):
            payload["thinking"] = {"type": thinking}
        return payload

        # ---------------------------------------------------------------- 调用
    async def encode(self, window: Window, *, known: str = "（暂无）",
                     extra_system: str = "") -> EncodeOutcome:
        """抽取一个窗口。**不抛异常**——失败返回 status=degraded。"""
        t0 = time.perf_counter()
        out = EncodeOutcome()
        messages = self.build_messages(window, known)
        if extra_system and messages:
            messages[0]["content"] = str(messages[0].get("content", "")) + "\n\n" + extra_system
        span = Span()
        last_err: str | None = None

        for attempt in range(1, self.cfg.extract.retries + 2):
            out.attempts = attempt
            try:
                text, usage = await self._post(messages)
            except httpx.TimeoutException as exc:
                last_err = f"timeout: {exc!r}"
                # 超时不重试。30s 都超时的上游，再等 30s 只会让积压更严重。
                break
            except httpx.HTTPStatusError as exc:
                last_err = f"http {exc.response.status_code}: {_short(exc.response.text)}"
                # 4xx 是配置/额度问题，重试无意义；5xx 交给上层熔断
                break
            except httpx.HTTPError as exc:
                last_err = f"network: {exc!r}"
                break
            except _BadResponse as exc:
                # 响应结构不对（没有 choices / content 不是字符串 / 不是 JSON）。
                # 这类错误重试有意义：上游偶发会吐空 choices。
                last_err = f"bad response: {exc}"
                out.attempts = attempt
                if attempt <= self.cfg.extract.retries:
                    continue
                break

            out.raw_text = text
            # 累加而不是覆盖：重试时两次调用都真花了钱，只算最后一次
            # 会让"重试率上升"这件事在成本报表里隐形。
            out.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            out.completion_tokens += int(usage.get("completion_tokens") or 0)
            out.cached_tokens += _cached_of(usage)
            out.reasoning_tokens += _reasoning_of(usage)
            span.prompt_tokens = out.prompt_tokens
            span.completion_tokens = out.completion_tokens
            span.cached_tokens = out.cached_tokens

            try:
                out.result = parse_result(text)
                out.status = "ok"
                last_err = None
                await self._polish(out, span)
                break
            except ParseError as exc:
                last_err = str(exc)
                log.warning("extract.parse.failed", extra={"fields": {
                    "attempt": attempt, "err": str(exc), "head": exc.head}})
                if attempt <= self.cfg.extract.retries:
                    # 把错误输出放进 assistant 位，让模型看见自己错在哪
                    messages = messages + [
                        {"role": "assistant", "content": (text or "")[:2000]},
                        {"role": "user", "content": _RETRY_HINT},
                    ]

        if out.status != "ok":
            out.status = "degraded"
            out.error = last_err or "unknown"
            log.warning("extract.degraded", extra={"fields": {
                "session": window.session, "idxs": [window.start_idx, window.end_idx],
                "attempts": out.attempts, "err": out.error}})

        out.cost = span.cost(self.cfg.pricing.as_dict())
        out.latency_ms = int((time.perf_counter() - t0) * 1000)
        return out

    async def _polish(self, out: EncodeOutcome, span: Span) -> None:
        """摘要修复回路（M6.2）。**最多一次**，失败不影响主流程。

        为什么不重试到底：每多一轮就多一次成本与延迟，而摘要不是关键路径——
        它决定"检索命中后要不要展开原文"，差一点不会让记忆变错，
        只让检索的手感差一点。为它把预算烧光不划算。

        为什么给模型 raw 而不是旧摘要：旧摘要就是错的产物。
        用错的东西去改错的东西，只会让错误变得通顺，且每轮掉一层信息。
        """
        res = out.result
        if res is None or not res.events:
            return
        issues = scan(res.events)
        if not issues:
            return

        if not self.cfg.extract.summary_repair:
            self._truncate_bad(res, issues)
            return

        kinds = sorted({k for it in issues for k in it.kinds})
        note = {"session": None, "n": len(issues), "kinds": kinds}
        out.repairs += 1
        out.repair_kinds = ",".join(kinds)
        try:
            text, usage = await self._post([{
                "role": "user",
                "content": REPAIR_HEAD + repair_block(res.events, issues),
            }])
        except (httpx.HTTPError, _BadResponse) as exc:
            # 修复失败不算抽取失败：摘要还留着原样，只是没变好。
            log.warning("summary.repair.failed", extra={"fields": {
                **note, "err": repr(exc)[:200]}})
            self._truncate_bad(res, issues)
            return

        out.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        out.completion_tokens += int(usage.get("completion_tokens") or 0)
        out.cached_tokens += _cached_of(usage)
        out.reasoning_tokens += _reasoning_of(usage)
        span.prompt_tokens = out.prompt_tokens
        span.completion_tokens = out.completion_tokens
        span.cached_tokens = out.cached_tokens

        applied = _apply_repair(res, text)
        log.info("summary.repair", extra={"fields": {**note, "applied": applied}})

        # 修完再审一遍：模型可能修好了长度、又丢了人名。
        left = scan(res.events)
        if left:
            self._truncate_bad(res, left)

    @staticmethod
    def _truncate_bad(res: ExtractResult, issues: list) -> None:
        """仍然不合格的：截断 + 打标记。

        标记比截断本身重要——不标记的话，"摘要只有半句"会被当成模型写得差，
        而实际是我们的兜底逻辑吃掉了后半句。三个月后没人能还原这个判断。

        两个标记分开打：
          summary_truncated → 我们砍掉了内容（信息丢失）
          summary_flagged   → 内容还在，但人称/实体不对（信息可疑）
        """
        from .summary import MAX_LEN

        for it in issues:
            ev = res.events[it.idx]
            if len(ev.summary) > MAX_LEN:
                ev.summary = truncate(ev.summary)
                ev.summary_truncated = 1
            elif ev.summary:
                ev.summary_flagged = 1

    async def _post(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
        url = _endpoint(self.cfg.deepseek.base_url)
        resp = await self.client.post(
            url,
            json=self._payload(messages),
            headers={
                "authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
            },
            timeout=self.cfg.extract.timeout,
        )
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{resp.status_code}", request=resp.request, response=resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise _BadResponse(f"响应不是 JSON: {exc}") from exc
        choices = data.get("choices") or []
        if not choices:
            raise _BadResponse(
                f"没有 choices: {json.dumps(data, ensure_ascii=False)[:200]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise _BadResponse(f"content 不是字符串: {str(message)[:200]}")
        return content, (data.get("usage") or {})


def _apply_repair(res: ExtractResult, text: str) -> int:
    """把修复结果写回 events。返回应用了几条。

    下标用的是模型输出的下标（和 evidence 同一套），所以只认**在原范围内**的 idx：
    模型偶尔会给出超出范围的编号（比如凭空写个 [9]），
    直接索引会抛 IndexError，把一次成功的抽取变成降级。
    """
    import json as _json

    try:
        data = _json.loads(_strip_fence(text))
    except ValueError:
        return 0
    items = data.get("summaries")
    if not isinstance(items, list):
        return 0

    applied = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        summary = item.get("summary")
        if not (0 <= idx < len(res.events)) or not isinstance(summary, str):
            continue
        s = summary.strip()
        if not s:
            continue
        res.events[idx].summary = s
        applied += 1
    return applied


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


class _BadResponse(Exception):
    """上游回了 200，但响应体不是我们要的形状。"""


def _cached_of(usage: dict[str, Any]) -> int:
    """缓存命中的前缀 token 数。两个上游两种写法，都要认。

    DeepSeek: prompt_cache_hit_tokens
    mimo:     prompt_tokens_details.cached_tokens
    未命中时 mimo 的 prompt_tokens_details 是 **null**（不是 {}），
    所以这里必须 or {} 兜一次。
    """
    v = usage.get("prompt_cache_hit_tokens")
    if v:
        return int(v)
    details = usage.get("prompt_tokens_details") or {}
    return int(details.get("cached_tokens") or 0)


def _reasoning_of(usage: dict[str, Any]) -> int:
    details = usage.get("completion_tokens_details") or {}
    return int(details.get("reasoning_tokens") or 0)


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/chat/completions"


def _short(text: str, n: int = 200) -> str:
    return (text or "").replace("\n", " ")[:n]


def _now_hint(tz: str) -> str:
    """给模型的当前时间。

    只给到"周几 + 日期 + 时段"，不给精确到分：
    精确时间对抽取没用，而模型看到"22:47"会开始算"那是几小时前"，
    然后自己换算相对时间——这正是规则 8 明令禁止的事。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        now = datetime.now(ZoneInfo(tz or "Asia/Shanghai"))
    except Exception:  # noqa: BLE001 - tz 写错不该让抽取挂掉
        now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    hour = now.hour
    part = ("凌晨" if hour < 6 else "早上" if hour < 9 else "上午" if hour < 12
            else "中午" if hour < 14 else "下午" if hour < 18
            else "晚上" if hour < 23 else "深夜")
    return f"{now:%Y-%m-%d}（周{week}）{part}"


# ---------------------------------------------------------------- 降级
def degrade_event_text(window: Window, tokens: Sequence[str] = ()) -> dict[str, Any]:
    """降级时写进 events 的那一条。纯函数，方便测试。

    raw 用整个窗口的对话原文（带角色前缀），summary 留空——
    宁可检索不到，也不要用模型编的摘要冒充原文。
    """
    raw = window.text()
    if not raw.strip():
        raw = f"[窗口 {window.start_idx}-{window.end_idx} 无内容]"
    return {
        "raw": raw,
        "summary": "",
        "keywords": list(tokens)[:8],
        "tier": 3,
        "w0": 0.15,
        "arousal": 0.0,
        "kind": "raw",
    }


__all__ = [
    "Encoder", "EncodeOutcome", "build_conversation", "build_known_entities",
    "degrade_event_text", "load_template", "anchor_raw",
]
