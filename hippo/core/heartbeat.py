"""M24 心跳：每 60 秒醒一次，绝大多数时候什么都不做。

## 这不是"定时任务框架"，是一个开关

整套方案的经济性押在一件事上：**挂机不花钱**。所以心跳的每一行代码
都要能回答"你为什么要起来"——查计划、扫过期疑问、把情绪衰减落库、
重算分层、夜里合并重复。这些全是 SQL 和算术。

唯一允许花钱的出口是 `Escalator`，三道闸：白名单、上下文 ≤300 token、
每天 ≤10 次（M24.3）。所以 24 小时挂机时 `metrics` 新增 LLM 调用 = 0
不是"优化出来的"，是结构保证的——循环里根本没有别的 `await llm(...)`。

## 六步为什么是这个顺序

    ① plans       到点就得响，晚一秒都是错
    ② doubts      过期的疑问要变成"我忘了"的体感
    ③ mood        情绪惰性衰减（每 5 分钟，纯数学）
    ④ tier        重算 hot/warm/cold（每 60 分钟，一次 SQL 扫描）
    ⑤ consolidate 合并重复（每天 03:00，离线）
    ⑥ curiosity   好奇发言（M25，额度内）

前三步每轮都跑（都是毫秒级 SQL/算术），后面按频率。顺序的依据是
"晚了会怎样"：计划晚了用户直接损失，分层晚一小时没人察觉。
consolidate 放最后：它是唯一可能跑几秒的步骤，不该挡住提醒。

## 每步独立 try 的理由

一步抛异常把整轮打死，后果是"这一轮后面的步骤全没了"，而且从外面
看只是"日志里有个错"。更糟的是如果异常发生在 ①，计划就永远不响。
所以六步各自 try/except，单步失败记 `errors` 并继续。

## 优雅退出

`run_forever` 用 `wait_for(stop.wait(), timeout=剩余时间)`，stop 一置位
立即返回，最坏延迟是一个 await 的调度开销（微秒级）。SIGTERM 由宿主
（uvicorn）转成 lifespan 退出，这里只需要 stop 事件。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..log import get_logger
from .affect import BASELINE, MOODS, TAU_MINUTES, decay_moods
from .scheduler import Scheduler
from .turns import estimate_tokens

log = get_logger("heartbeat")

__all__ = [
    "Heartbeat", "TickReport", "StepResult", "Escalator", "Escalation",
    "ESCALATION_REASONS",
]

# 允许升级到一次模型调用的理由（M24.3）。除此之外一律不升级。
# 这份名单只能由人来改——"每次 tick 顺手总结一下"是最容易破功的写法。
ESCALATION_REASONS = ("plan", "curiosity", "user_message")

_SYS = "_sys"          # 非会话级的系统状态（计数、每日标记）都记在这


@dataclass
class StepResult:
    """一步的结果。`ran=False` = 没到频率，不是失败。"""

    name: str
    ran: bool = False
    n: int = 0
    ms: float = 0.0
    err: str = ""


@dataclass
class TickReport:
    at: int
    steps: list[StepResult] = field(default_factory=list)
    errors: int = 0
    jitter_ms: float = 0.0

    @property
    def ran(self) -> list[str]:
        return [s.name for s in self.steps if s.ran]

    def get(self, name: str) -> StepResult | None:
        return next((s for s in self.steps if s.name == name), None)


class Heartbeat:
    """心跳循环。一个 store 一个实例，宿主在 lifespan 里起一个 task。"""

    def __init__(self, store: Any, *, cfg: Any = None,
                 scheduler: Scheduler | None = None,
                 tick_seconds: int | None = None,
                 wall: Callable[[], float] | None = None,
                 clock: Callable[[], float] | None = None,
                 on_curiosity: Callable[[], Any] | None = None):
        hb = getattr(cfg, "heartbeat", None)
        self.store = store
        self.cfg = cfg
        self.tick_seconds = int(tick_seconds or getattr(hb, "interval_seconds", 60))
        self.mood_seconds = int(getattr(hb, "mood_seconds", 300))
        self.tier_seconds = int(getattr(hb, "tier_seconds", 3600))
        self.consolidate_hour = int(getattr(hb, "consolidate_hour", 3))
        self.tz = getattr(cfg, "timezone", "Asia/Shanghai") or "Asia/Shanghai"
        self.scheduler = scheduler or Scheduler(store, tick_seconds=self.tick_seconds)
        # wall = 墙上时间（秒），clock = 单调时钟（秒）。分成两个是为了
        # 测试能"快进 24 小时"而不影响时间戳语义：jitter 必须用单调钟算，
        # 用墙钟算的话 NTP 校时一次就会让抖动看起来像 3 秒。
        self.wall = wall or time.time
        self.clock = clock or time.monotonic
        self.on_curiosity = on_curiosity
        # Escalator 只在真有外发通道时才有意义（QQ 桥接 / CLI 排查）。
        # 插件本身没有"主动给用户发消息"的通道，所以**不在这里创建**——
        # 以前每个 tick 都建一个却从不调用，属于"设计了但没用"。
        self._escalator: Escalator | None = None
        self._escalator_cfg = (store, cfg)
        self._last: dict[str, float] = {}
        self.last_report: TickReport | None = None
        # counts 的每一项都是"执行次数"，不是"处理条数"：
        # 混着记的话，"tier=3"到底是重算了三次还是改了三层，没人说得清。
        # 条数放 *_n 里，两个维度分开。
        self.counts = {"ticks": 0, "plans": 0, "plans_fired": 0, "doubts": 0,
                       "doubts_n": 0, "mood": 0, "mood_n": 0, "tier": 0,
                       "tier_changed": 0, "consolidate": 0, "clusters": 0,
                       "curiosity": 0, "errors": 0, "llm_calls": 0}

    # ------------------------------------------------------------ 频率
    def _due(self, name: str, every: int, now: int) -> bool:
        """按"上次真正跑的时刻"判频率，不按 tick 序号。

        按序号判的话，tick 一旦漏跑（进程卡了 3 秒），后面的节奏全部
        错位；按时间判天然自愈。
        """
        if every <= 0:
            return True
        last = self._last.get(name)
        if last is None or now - last >= every:
            self._last[name] = now
            return True
        return False

    def _daily(self, name: str, now: int) -> bool:
        """每天某个整点跑一次。用 state 存日期，重启不会重复跑。"""
        dt = datetime.fromtimestamp(now, ZoneInfo(self.tz))
        if dt.hour != self.consolidate_hour:
            return False
        key = f"ran:{name}"
        today = dt.date().isoformat()
        if self.store.get_state(_SYS, key) == today:
            return False
        self.store.set_state(_SYS, key, today)
        return True

    # ------------------------------------------------------------ 主循环体
    @property
    def escalator(self) -> "Escalator":
        """按需创建：只有真要用的人（CLI/QQ 桥接）才会拿到实例。"""
        if self._escalator is None:
            store, cfg = self._escalator_cfg
            self._escalator = Escalator(store, cfg=cfg, wall=self.wall)
        return self._escalator

    def tick(self, *, now: int | None = None,
             jitter_ms: float = 0.0) -> TickReport:
        """跑一轮六步。任何一步抛异常都不影响其余五步。"""
        t = int(now if now is not None else self.wall())
        rep = TickReport(at=t, jitter_ms=jitter_ms)
        self.counts["ticks"] += 1

        self._step(rep, "plans", self._plans, t)
        self._step(rep, "doubts", self._doubts, t)
        self._step(rep, "mood", self._mood, t, every=self.mood_seconds)
        self._step(rep, "tier", self._tier, t, every=self.tier_seconds)
        self._step(rep, "consolidate", self._consolidate, t,
                   gate=lambda: self._daily("consolidate", t))
        self._step(rep, "curiosity", self._curiosity, t)

        self.counts["errors"] += rep.errors
        # 观测口：一行 UPSERT，让外部（测试/运维/报告）能证明"心跳活着"。
        # 不记 ticks 累计数——那个数重启就归零，没有意义；last_tick 才是
        # "最近一次醒过来是什么时候"，而这个正是排查"心跳死了"的唯一证据。
        try:
            self.store.set_state(_SYS, "hb:last_tick", str(t))
        except Exception:         # noqa: BLE001 - 观测失败不许影响心跳
            pass
        return rep

    def _step(self, rep: TickReport, name: str, fn: Callable[[int], int],
              now: int, *, every: int = 0,
              gate: Callable[[], bool] | None = None) -> None:
        if gate is not None:
            if not gate():
                rep.steps.append(StepResult(name))
                return
        elif not self._due(name, every, now):
            rep.steps.append(StepResult(name))
            return

        t0 = self.clock()
        try:
            n = int(fn(now) or 0)
        except Exception as exc:      # noqa: BLE001 - 单步失败不许杀循环
            rep.errors += 1
            rep.steps.append(StepResult(name, True, 0,
                                        (self.clock() - t0) * 1000,
                                        repr(exc)[:200]))
            log.error("heartbeat.step_failed", extra={"fields": {
                "step": name, "err": repr(exc)[:200]}})
            return
        ms = (self.clock() - t0) * 1000
        self.counts[name] += 1
        self.counts[f"{name}_n"] = self.counts.get(f"{name}_n", 0) + n \
            if name in ("doubts", "mood") else self.counts.get(f"{name}_n", 0)
        rep.steps.append(StepResult(name, True, n, ms))

    # ------------------------------------------------------------ 六步
    def _plans(self, now: int) -> int:
        fired = self.scheduler.tick(now=now)
        self.counts["plans_fired"] += len(fired)
        return len(fired)

    def _doubts(self, now: int) -> int:
        """过期疑问 → cold 记忆。失败不阻断（下一轮还会扫到）。"""
        from .doubt import expire_to_memory
        return len(expire_to_memory(self.store, now_ts=now))

    def _mood(self, now: int) -> int:
        """把情绪衰减**落一次库**。

        平时读情绪走 lazy（`pack` 现算），库里的值本来就是"陈旧但正确"的。
        这里落库只为一件事：让 `updated` 跟上。不落的话，一年后第一次
        读要算 525600 分钟——数学上没问题，但 `updated` 停在一年前，
        任何"最近情绪怎么样"的查询都会拿到一个过期时间戳。
        """
        base = getattr(getattr(self.cfg, "affect", None), "baseline", None) or BASELINE
        tau = getattr(getattr(self.cfg, "affect", None), "tau_minutes", None) or TAU_MINUTES
        n = 0
        for s in self._affect_sessions():
            a = self.store.get_affect(s)
            if a is None or not a.updated:
                continue
            minutes = (now - a.updated) / 60.0
            if minutes < 1:
                continue
            before = {m: getattr(a, m) for m in MOODS}
            after = decay_moods(before, baseline=base, tau=tau, minutes=minutes)
            if max(abs(after[m] - before[m]) for m in MOODS) >= 0.5:
                self.store.upsert_affect(s, updated=now, **after)
                n += 1
        return n

    def _affect_sessions(self) -> list[str]:
        """既有对话、又有情绪行的会话集合。

        只查 turns 会漏掉"对话已清、情绪还在"的会话；
        只查 affect 会漏掉刚建还没写情绪的。并集最稳。
        """
        out = set(self.store.list_sessions())
        out.update(self.store.affect_sessions())
        return sorted(out)

    def _tier(self, now: int) -> int:
        from .dynamics import recompute
        weight_cfg = getattr(self.cfg, "weight", None)
        rep = recompute(self.store, now, cfg=weight_cfg)
        changed = int(getattr(rep, "changed", 0))
        self.counts["tier_changed"] += changed
        return changed

    def _consolidate(self, now: int) -> int:
        """夜间巩固。不传 summarizer：只做结构合并，零 token。"""
        from . import consolidate
        n = 0
        for s in self.store.list_sessions():
            r = consolidate.run(self.store, s, now=now)
            n += int(getattr(r, "clusters", 0) or 0)
        self.counts["clusters"] += n
        return n

    def _curiosity(self, now: int) -> int:
        """M25 的钩子。没接就什么都不做——不接不等于降级。"""
        if self.on_curiosity is None:
            return 0
        r = self.on_curiosity()
        if asyncio.iscoroutine(r):
            # 心跳是同步 tick；好奇发言要等模型，不能在这里阻塞。
            try:
                asyncio.get_running_loop().create_task(r)
            except RuntimeError:
                return 0
            return 1
        return int(r or 0)

    # ------------------------------------------------------------ 循环
    async def run_forever(self, *, stop: asyncio.Event | None = None,
                          max_ticks: int | None = None,
                          first_delay: float = 0.0) -> None:
        """主循环。`max_ticks` 只给测试和模拟器用。

        节奏用单调钟排：`next_at += interval` 而不是"睡满 60 秒"。
        后者会把每轮的执行耗时累加进去（tick 花 30ms，60 轮就飘 2 秒），
        而验收要求抖动 <2s。

        `first_delay` 让宿主"晚一个周期再开始"。生产上晚 60 秒没区别，
        但对集成测试是决定性的：测试进程往往活不过 60 秒，心跳若立刻
        跑第一轮，就会在别人的断言中间改 tier。
        """
        stop = stop or asyncio.Event()
        interval = float(self.tick_seconds)
        if first_delay:
            try:
                await asyncio.wait_for(stop.wait(), timeout=float(first_delay))
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                return
        next_at = self.clock()
        ticks = 0
        while not stop.is_set():
            if max_ticks is not None and ticks >= max_ticks:
                break
            jitter = max(0.0, (self.clock() - next_at) * 1000)
            try:
                self.last_report = self.tick(jitter_ms=jitter)
            except Exception as exc:      # tick 内部已逐步兜底，这层是最后保险
                log.error("heartbeat.tick_failed", extra={"fields": {
                    "err": repr(exc)[:200]}})
            ticks += 1
            next_at += interval
            delay = next_at - self.clock()
            if delay < 0:
                # 落后超过一整轮：说明进程被挂起了（笔记本合盖/容器冻结）。
                # 不补跑，直接对齐到下一个整点——补跑 100 轮只会雪上加霜。
                if delay < -interval:
                    next_at = self.clock() + interval
                    delay = interval
                else:
                    delay = 0
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


class Escalation:
    """一次升级判定的结果。`ok=False` 时 `why` 说明被哪道闸拦下。"""

    __slots__ = ("ok", "reason", "text", "tokens", "why", "left")

    def __init__(self, ok: bool, reason: str, *, text: str = "", tokens: int = 0,
                 why: str = "", left: int = 0):
        self.ok = ok
        self.reason = reason
        self.text = text
        self.tokens = tokens
        self.why = why
        self.left = left

    def __repr__(self) -> str:      # 日志里好读
        return (f"Escalation(ok={self.ok}, reason={self.reason!r}, "
                f"tokens={self.tokens}, why={self.why!r}, left={self.left})")


class Escalator:
    """升级闸门（M24.3）。三道闸全过，才允许花一次调用。

    它**不调模型**。真正的调用由宿主传 `call` 进来——闸门只负责
    "该不该"和"记一笔"，把"怎么调"留给调用方。这样测试不需要网。
    """

    def __init__(self, store: Any, *, cfg: Any = None,
                 wall: Callable[[], float] | None = None):
        hb = getattr(cfg, "heartbeat", None)
        self.store = store
        self.max_per_day = int(getattr(hb, "max_llm_calls_per_day", 10))
        self.max_context = int(getattr(hb, "escalate_max_context", 300))
        self.max_tokens = int(getattr(hb, "escalate_max_tokens", 120))
        self.temperature = float(getattr(hb, "escalate_temperature", 0.8))
        self.tz = getattr(cfg, "timezone", "Asia/Shanghai") or "Asia/Shanghai"
        self.wall = wall or time.time

    # ---- 计数
    def _day(self, now: int | None) -> str:
        t = now if now is not None else int(self.wall())
        return datetime.fromtimestamp(t, ZoneInfo(self.tz)).date().isoformat()

    def used(self, now: int | None = None) -> int:
        raw = self.store.get_state(_SYS, f"hb_calls:{self._day(now)}", "0")
        try:
            return int(raw)
        except ValueError:
            return 0

    def left(self, now: int | None = None) -> int:
        return max(0, self.max_per_day - self.used(now))

    def fit(self, context: str) -> str:
        """把上下文压到 ≤300 token。按行砍，保留开头（开头是任务）。"""
        text = (context or "").strip()
        if estimate_tokens(text) <= self.max_context:
            return text
        lines = text.split("\n")
        while len(lines) > 1 and estimate_tokens("\n".join(lines)) > self.max_context:
            lines.pop()
        text = "\n".join(lines)
        # 单行也超：按字符退，留一点余量给截断标记
        while text and estimate_tokens(text) > self.max_context:
            text = text[:-10]
        return text

    def escalate(self, reason: str, context: str = "", *,
                 call: Callable[..., Any] | None = None,
                 now: int | None = None) -> Escalation:
        """判定 + 记账。`call(text, max_tokens=, temperature=)` 在放行后执行。"""
        left = self.left(now)
        if reason not in ESCALATION_REASONS:
            # 告警只走 _warn_once：同一天同一种拦截一条，不刷屏
            self._warn_once(now, f"reason:{reason or '-'}",
                            "heartbeat.escalate_blocked 非白名单理由")
            return Escalation(False, reason, why="not_whitelisted", left=left)
        if left <= 0:
            self._warn_once(now, "cap", "heartbeat.escalate_capped 今日调用额度用尽")
            return Escalation(False, reason, why="daily_cap", left=0)

        text = self.fit(context)
        tokens = estimate_tokens(text)
        if call is None:
            # 只判不放行：调用方拿这个对象自己决定怎么发请求
            return Escalation(True, reason, text=text, tokens=tokens,
                              why="dry", left=left)
        try:
            call(text, max_tokens=self.max_tokens, temperature=self.temperature)
        except Exception as exc:      # noqa: BLE001
            log.warning("heartbeat.escalate_failed", extra={"fields": {
                "reason": reason, "err": repr(exc)[:160]}})
            return Escalation(False, reason, text=text, tokens=tokens,
                              why="call_failed", left=left)
        self._bump(now)
        return Escalation(True, reason, text=text, tokens=tokens,
                          why="ok", left=left - 1)

    def _bump(self, now: int | None) -> None:
        day = self._day(now)
        self.store.set_state(_SYS, f"hb_calls:{day}", str(self.used(now) + 1))

    def _warn_once(self, now: int | None, tag: str, msg: str) -> None:
        """同一天同一种拦截只告警一次。

        1440 轮 tick 每轮都 WARNING 会把日志冲爆，最后没人看——
        而这正是"超额了却没人知道"的成因。
        """
        key = f"hb_warned:{self._day(now)}:{tag}"
        if self.store.get_state(_SYS, key):
            return
        self.store.set_state(_SYS, key, "1")
        log.warning(msg, extra={"fields": {"day": self._day(now), "tag": tag}})
