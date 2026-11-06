"""成本与延迟埋点（M0.3.2）。

四个时间点，口径必须固定，否则跨版本的数字没法比：

    t0  收到请求          （进入路由的第一行）
    t1  组包完成          （M13 之前就是 t0，因为还没 pack）
    t2  上游首字节         （流式：第一行 SSE；非流式：拿到响应头）
    t3  结束

单位：t0~t3 存 **epoch 毫秒**，`metrics.ts` 存秒。
混用是刻意的：毫秒是为了看清"组包到底花了 1ms 还是 200ms"，
秒是为了和 events/plans 那些表对得上时间轴。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# DeepSeek 官方定价（元 / 百万 token）。**会变**，所以放 config，
# 这里只是兜底默认值，别拿它当合同。
DEFAULT_PRICING = {"input_miss_per_m": 2.0, "input_hit_per_m": 0.5, "output_per_m": 8.0}


def now_ms() -> int:
    return int(time.time() * 1000)


def new_req_id() -> str:
    return f"r-{uuid.uuid4().hex[:12]}"


@dataclass
class Span:
    """一次请求的生命周期。线程/协程内局部变量，不共享。"""

    req_id: str = field(default_factory=new_req_id)
    session: str | None = None
    t0: int = field(default_factory=now_ms)
    t1: int | None = None
    t2: int | None = None
    t3: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    model: str = ""
    stream: bool = False
    # 这一轮注入的记忆 token。不进 metrics 表（那张表是"钱"，这是"内容"），
    # 但排查"为什么这轮特别贵"时第一个要看的就是它。
    pack_tokens: int = 0

    def mark(self, name: str) -> None:
        # 只记第一次。重复 mark 说明有分支重入，保留最早的那个才是真实首字节。
        if getattr(self, name, None) is None:
            setattr(self, name, now_ms())

    def pack_done(self) -> None:
        self.mark("t1")

    def first_byte(self) -> None:
        self.mark("t2")

    def finish(self) -> None:
        self.mark("t3")

    def absorb_usage(self, usage: dict[str, Any] | None) -> None:
        """吃 OpenAI / DeepSeek 两种形状的 usage。"""
        if not usage:
            return
        self.prompt_tokens = int(usage.get("prompt_tokens") or 0)
        self.completion_tokens = int(usage.get("completion_tokens") or 0)

        cached = usage.get("prompt_cache_hit_tokens")
        if cached is None:
            details = usage.get("prompt_tokens_details") or {}
            cached = details.get("cached_tokens")
        self.cached_tokens = int(cached or 0)

    def cost(self, pricing: dict[str, float] | None = None) -> float:
        p = pricing or DEFAULT_PRICING
        miss = max(0, self.prompt_tokens - self.cached_tokens)
        return round(
            miss * p.get("input_miss_per_m", 2.0) / 1e6
            + self.cached_tokens * p.get("input_hit_per_m", 0.5) / 1e6
            + self.completion_tokens * p.get("output_per_m", 8.0) / 1e6,
            8,
        )

    def as_row(self, *, pricing: dict[str, float] | None = None) -> dict[str, Any]:
        return {
            "ts": int(time.time()),
            "session": self.session,
            "req_id": self.req_id,
            "t0": self.t0,
            "t1": self.t1,
            "t2": self.t2,
            "t3": self.t3,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "cost": self.cost(pricing),
        }

    def summary(self) -> dict[str, Any]:
        """给日志用的一行摘要，别把整个 span 序列化进去。"""
        return {
            "req_id": self.req_id,
            "pack_ms": (self.t1 - self.t0) if self.t1 else None,
            "ttfb_ms": (self.t2 - self.t0) if self.t2 else None,
            "total_ms": (self.t3 - self.t0) if self.t3 else None,
            "ptok": self.prompt_tokens,
            "ctok": self.completion_tokens,
            "hit": self.cached_tokens,
            "cost": self.cost(),
        }


def record_metric(store_path: str | Any, span: Span, *, pricing: dict[str, float] | None = None) -> int:
    """用短连接写一条 metrics。

    为什么不复用 app 里的长连接：sqlite3 的连接不是协程安全的，
    而 metrics 是 fire-and-forget 的低频写。开一个连接、写一行、关掉，
    成本约 0.1ms，换来的是"永远不会因为共享连接踩到串行化错误"。
    """
    from hippo.core.store import Store

    with Store.open(store_path) as store:
        return store.add_metric(**span.as_row(pricing=pricing))
