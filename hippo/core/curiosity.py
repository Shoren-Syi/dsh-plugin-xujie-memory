"""M25 好奇机制：从记忆的缺口里长出问题——只长问题，不长动作。

三层约束，缺一不可：

  1. **问什么**由 SQL 决定（四类结构性缺口），不由模型即兴发挥。
     模型不知道自己不知道什么，它只会顺着上一句话编；真正的缺口
     是库里的空位：一行过期没解、一个人只有名字、一条事实失效了
     却没有新值。这些只有 SQL 看得见。
  2. **什么时候问**由 Quota 决定：额度 / 静默时段 / 冷遇 / 不打断。
  3. **问的时候**没有写权限（M25.3 的 explore_mode）。

一句话：好奇负责产生问题，动手永远要用户点头。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from ..log import get_logger
from .doubt import answer_overlap, trigram_similarity
from .store import fact_keys

log = get_logger("curiosity")

# 缺口的"过期尺度"：用来把"多久没管了"折算成 0~1 的紧迫度。
# 尺度不同是因为四类缺口坏得速度不一样：一个过期疑问放一周就凉了，
# 一条错的角色卡放两个月也只是"略有偏差"。
KIND_WINDOW_DAYS = {"doubt": 7.0, "entity": 30.0, "trait": 60.0, "fact": 14.0}

# 缺口的底价：这条缺口就算刚出现，问出来也值不值。
# 失效事实最贵（下游会把旧地址当现值用），半信半疑的角色卡最便宜
# （记错一句性格，最多是不懂他）。
KIND_IMPACT = {"entity": 0.75, "trait": 0.45, "fact": 0.70}

# 新颖度的地板：问过三次以上的缺口不再归零，而是压到 0.25。
# 归零会让"问过但没答上"的缺口永远沉底——那正是最该再问一次的。
NOVELTY_FLOOR = 0.25

# 去重阈值（trigram）。同一个缺口换个说法就能再问一遍的话，
# "只问一次"就成了摆设。
DEDUPE_SIM = 0.80

# 接话的"时间证据"：问题刚说完三分钟内用户就开口，就算接了这个话头，
# 哪怕字面上一句都对不上（"嗯对""差不多吧"）。纯字面匹配会让这类
# 简短回应全被记成"没接"，两次之后 AI 就白闭嘴三天。
ANSWER_GRACE = 180


@dataclass(frozen=True)
class Gap:
    """一条"想问点什么"的候选。"""

    kind: str           # doubt / entity / trait / fact
    ref: str            # 来源行，如 doubt:12
    question: str
    impact: float
    urgency: float
    novelty: float
    detail: str = ""    # 给 CLI 看的"为什么问这个"

    @property
    def priority(self) -> float:
        return round(self.impact * self.urgency * self.novelty, 3)

    def why(self) -> str:
        return (f"impact {self.impact:.2f} × urgency {self.urgency:.2f}"
                f" × novelty {self.novelty:.2f}")


# ------------------------------------------------------------------ 打分
def _urgency(age_seconds: float, kind: str) -> float:
    """0.15 起步，按缺口尺度线性涨到 1。

    起步不给 0：刚出现的缺口也该有一点紧迫度，否则它要等一周才排得上
    队；也不给 1：刚冒头的事不该压过搁置了一个月的事。
    """
    window = KIND_WINDOW_DAYS.get(kind, 30.0) * 86400
    return round(min(1.0, 0.15 + max(0.0, age_seconds) / window), 3)


def _novelty(times: int) -> float:
    return max(NOVELTY_FLOOR, round(0.5 ** max(0, int(times)), 3))


def _ccfg(cfg: Any) -> Any:
    """取 curiosity 配置段。cfg=None（测试常用）时用代码里的默认值，
    而不是"没有配置所以全关"——后者会让 `Quota` 在测试里静默变成
    "额度 0、不静默"，行为与线上完全两样。"""
    section = getattr(cfg, "curiosity", None)
    if section is not None:
        return section
    from ..config import CuriosityCfg
    return CuriosityCfg()


def _clip(text: str, limit: int = 40) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit] + "…"


def _as_question(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    return t if t.endswith(("？", "?", "。", "！", "!")) else t + "？"


# ------------------------------------------------------------------ 四类缺口
def _doubt_gaps(rows: list[dict], now: int, times: dict[str, int]) -> list[Gap]:
    out = []
    for r in rows:
        ref = f"doubt:{r['id']}"
        age = now - int(r["expires"])
        out.append(Gap(
            kind="doubt", ref=ref,
            question=_as_question(str(r["question"])),
            impact=round(float(r["impact"] or 0.5), 3),
            urgency=_urgency(age, "doubt"),
            # 疑问在对话里可能已经被问过（asked_count），那条通道的
            # 失败经验必须算进来：问过两遍没人答，第三遍换成半夜主动
            # 开口，也只是换个地方招人烦。
            novelty=_novelty(times.get(ref, 0) + int(r["asked_count"] or 0)),
            detail=f"过期 {max(0, age) // 86400} 天未解",
        ))
    return out


def _entity_gaps(rows: list[dict], now: int, times: dict[str, int]) -> list[Gap]:
    out = []
    for r in rows:
        ref = f"entity:{r['id']}"
        last = int(r["last_event"] or r["last_seen"] or now)
        out.append(Gap(
            kind="entity", ref=ref,
            question=f"{r['canonical']}最近怎么样了？",
            impact=KIND_IMPACT["entity"],
            urgency=_urgency(now - last, "entity"),
            novelty=_novelty(times.get(ref, 0)),
            detail=f"你钉住的事里提过 {int(r['mentions'])} 次，角色卡是空的",
        ))
    return out


def _trait_gaps(rows: list[dict], now: int, times: dict[str, int]) -> list[Gap]:
    out = []
    for r in rows:
        ref = f"trait:{r['id']}"
        name = str(r["canonical"] or "他")
        out.append(Gap(
            kind="trait", ref=ref,
            question=f"我记得{name}{_clip(str(r['trait']), 18)}，还准吗？",
            # 越不确定越值得问：置信度 0.2 的推断，问一句就能扶正
            impact=round(min(0.90, KIND_IMPACT["trait"] + (0.5 - float(r["confidence"]))), 3),
            urgency=_urgency(now - int(r["ts"] or 0), "trait"),
            novelty=_novelty(times.get(ref, 0)),
            detail=f"置信度 {float(r['confidence']):.2f}，"
                   f"{(now - int(r['ts'] or now)) // 86400} 天没更新",
        ))
    return out


def _fact_gaps(rows: list[dict], now: int, times: dict[str, int]) -> list[Gap]:
    """失效事实。

    `key` 存的是英文正名（city / employer），问人的时候必须换成人话
    （居住城市 / 工作单位）——"你之前说 city 是杭州"这种句子一张嘴
    就暴露了它是从数据库里念出来的。
    """
    table = fact_keys()
    out = []
    for r in rows:
        ref = f"fact:{r['id']}"
        k = table.get(str(r["key"]))
        name = k.cn if k is not None else str(r["key"])
        out.append(Gap(
            kind="fact", ref=ref,
            question=f"你之前说{name}是「{_clip(str(r['value']), 12)}」，现在还是吗？",
            impact=KIND_IMPACT["fact"],
            urgency=_urgency(now - int(r["valid_to"] or now), "fact"),
            novelty=_novelty(times.get(ref, 0)),
            detail=f"{r['label'] or '你'}的{name}已失效，且没有新值",
        ))
    return out


def collect_gaps(store: Any, session: str, *, now: int | None = None,
                 cfg: Any = None) -> list[Gap]:
    """四类缺口 → Gap 列表（未排序、未去重）。零 token。"""
    now = now if now is not None else int(time.time())
    ccfg = _ccfg(cfg)
    stale_days = int(getattr(ccfg, "stale_days", 30))
    refractory = int(getattr(ccfg, "refractory_days", 3)) * 86400

    rows = store.curiosity_gaps(session, now=now, stale_days=stale_days,
                                trait_days=stale_days, fact_days=stale_days)
    hist = store.recent_curiosity_refs(session, since=now - 120 * 86400)

    times: dict[str, int] = {}
    recent_refs: set[str] = set()
    recent_qs: list[str] = []
    for ref, q, created in hist:
        times[ref] = times.get(ref, 0) + 1
        if created >= now - refractory:
            recent_refs.add(ref)
            recent_qs.append(q)

    gaps: list[Gap] = []
    for build, key in ((_doubt_gaps, "doubt"), (_entity_gaps, "entity"),
                       (_trait_gaps, "trait"), (_fact_gaps, "fact")):
        for g in build(rows.get(key, []), now, times):
            if g.ref in recent_refs:
                continue        # 刚问过同一个缺口，换下一个
            if not g.question:
                continue
            if any(trigram_similarity(g.question, q) > DEDUPE_SIM for q in recent_qs):
                continue        # 换个说法的同一件事，也算问过
            gaps.append(g)
    return gaps


def build_queue(store: Any, session: str, *, now: int | None = None,
                cfg: Any = None, limit: int | None = None) -> list[Gap]:
    """按 priority 排序、按问题相似度再去一次重，取前 limit 条。"""
    ccfg = _ccfg(cfg)
    limit = int(limit or getattr(ccfg, "queue_size", 5))
    gaps = collect_gaps(store, session, now=now, cfg=cfg)
    gaps.sort(key=lambda g: (-g.priority, g.kind, g.ref))
    out: list[Gap] = []
    for g in gaps:
        if any(trigram_similarity(g.question, k.question) > DEDUPE_SIM for k in out):
            continue
        out.append(g)
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ 额度
class Quota:
    """什么时候可以开口。四条规矩，缺一条就会变成话痨。"""

    def __init__(self, store: Any, cfg: Any = None, *, tz: str | None = None,
                 wall: Callable[[], float] | None = None):
        ccfg = _ccfg(cfg)
        self.store = store
        self.daily = int(getattr(ccfg, "daily_quota", 3))
        qh = list(getattr(ccfg, "quiet_hours", None) or [])
        self.quiet: tuple[int, int] | None = (
            (int(qh[0]), int(qh[1])) if len(qh) == 2 else None)
        self.busy_minutes = int(getattr(ccfg, "busy_minutes", 30))
        self.response_seconds = int(getattr(ccfg, "response_hours", 24)) * 3600
        self.tz = tz or getattr(cfg, "timezone", "Asia/Shanghai") or "Asia/Shanghai"
        self.wall = wall or time.time

    # ---- 自然日
    def day(self, now: int | float | None = None) -> str:
        ts = int(now if now is not None else self.wall())
        return datetime.fromtimestamp(ts, ZoneInfo(self.tz)).strftime("%Y-%m-%d")

    def day_bounds(self, now: int | float | None = None) -> tuple[int, int]:
        ts = int(now if now is not None else self.wall())
        dt = datetime.fromtimestamp(ts, ZoneInfo(self.tz))
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp()), int(start.timestamp()) + 86400

    def used(self, now: int | None = None) -> int:
        """今天一共说了几次。**跨会话计数**：额度是"这个人一天被主动
        搭话几次"，不是"每个会话各几次"——三个会话各问三次，还是九次。"""
        s, e = self.day_bounds(now)
        return self.store.count_curiosity_spoken(since=s, until=e)

    def left(self, now: int | None = None) -> int:
        return max(0, self.daily - self.used(now))

    # ---- 静默时段
    def in_quiet(self, now: int | None = None) -> bool:
        if self.quiet is None:
            return False
        start, end = self.quiet
        if start == end:
            return False
        hour = datetime.fromtimestamp(
            int(now if now is not None else self.wall()), ZoneInfo(self.tz)).hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    # ---- 正在聊
    def busy(self, session: str, now: int | None = None) -> bool:
        ts = int(now if now is not None else self.wall())
        last = self.store.last_user_turn(session)
        return last is not None and 0 <= ts - last < self.busy_minutes * 60

    # ---- 冷遇
    def snooze_key(self, now: int | None = None) -> str:
        return f"cur:snooze:{self.day(now)}"

    def snoozed(self, session: str, now: int | None = None) -> bool:
        return self.store.get_state(session, self.snooze_key(now), "") == "1"

    def snooze(self, session: str, now: int | None = None) -> None:
        self.store.set_state(session, self.snooze_key(now), "1")

    def allow(self, session: str, now: int | None = None) -> tuple[bool, str]:
        """能不能开口 + 不能的理由。理由码要能被日志和测试辨认。"""
        if self.snoozed(session, now):
            return False, "snoozed"
        if self.left(now) <= 0:
            return False, "quota"
        if self.in_quiet(now):
            return False, "quiet"
        if self.busy(session, now):
            return False, "busy"
        return True, "ok"


# ------------------------------------------------------------------ 引擎
class Curiosity:
    """把"缺口 → 问题 → 说出口"串起来。tick 里 prepare，pack 里 take。"""

    def __init__(self, store: Any, cfg: Any = None, *, tz: str | None = None,
                 wall: Callable[[], float] | None = None,
                 sessions: Callable[[], list[str]] | None = None):
        self.store = store
        self.cfg = cfg
        self.quota = Quota(store, cfg, tz=tz, wall=wall)
        self.wall = wall or time.time
        self._sessions = sessions
        self.last: Gap | None = None
        ccfg = _ccfg(cfg)
        # 排队久了就作废：三天前想问的一句"你搬家了吗"，今天突然冒出来
        # 只会让人莫名其妙。
        self.pending_max_age = int(getattr(ccfg, "pending_hours", 6)) * 3600

    # ---- 会话集合
    def sessions(self, now: int | None = None) -> list[str]:
        if self._sessions is not None:
            return list(self._sessions())
        ts = int(now if now is not None else self.wall())
        return self.store.active_sessions(since=ts - 7 * 86400)

    # ---- 有没有一条还没落地的
    def outstanding(self, session: str, now: int) -> bool:
        """队列里有一条，或者刚问出去还没等到回应。

        不叠问是硬要求：连着三条"你最近怎么样"不是关心，是催债。
        """
        if self.store.pending_curiosity(session, limit=1,
                                        max_age=self.pending_max_age, now=now):
            return True
        for r in self.store.curiosity_rows(session, limit=3):
            if r.outcome == "spoken" and r.spoken_at \
                    and now - r.spoken_at < self.quota.response_seconds:
                return True
        return False

    # ---- 入队
    def prepare(self, session: str, now: int | None = None) -> Gap | None:
        """心跳里调：判断现在能不能开口，能就把一条问题放进队列。"""
        now = int(now if now is not None else self.wall())

        if self.store.gave_up_curiosity(session, streak=2, now=now):
            # 连着两次没被接话，今天就不说了。额度清零不是惩罚，
            # 是"他今天不想聊这个"——明天重新开始。
            self.quota.snooze(session, now)
            log.info("curiosity.cold_shoulder", extra={"fields": {"session": session}})
            return None

        ok, why = self.quota.allow(session, now)
        if not ok:
            return None
        if self.outstanding(session, now):
            return None

        queue = build_queue(self.store, session, now=now, cfg=self.cfg, limit=1)
        if not queue:
            return None
        g = queue[0]
        self.store.add_curiosity(
            session=session, question=g.question, gap_kind=g.kind,
            gap_ref=g.ref, impact=g.impact, urgency=g.urgency,
            novelty=g.novelty, priority=g.priority, now=now)
        self.last = g
        log.info("curiosity.queued", extra={"fields": {
            "session": session, "kind": g.kind, "priority": g.priority,
            "question": g.question[:30]}})
        return g

    # ---- 说出口
    def take(self, session: str, now: int | None = None) -> str | None:
        """打包时调：把队列头交出去。**这里才计额度**。

        入队不计、出口才计：静默时段和"正在聊"会让队列堆着问不出去的
        问题，把入队当发言的话，一晚上能记出几十次，额度当场失效。
        """
        now = int(now if now is not None else self.wall())
        if self.quota.in_quiet(now):
            return None
        if self.quota.left(now) <= 0:
            return None
        g = self.store.next_curiosity(session, max_age=self.pending_max_age, now=now)
        if g is None:
            return None
        if not self.store.mark_curiosity_spoken(g.id, now):
            return None
        log.info("curiosity.spoken", extra={"fields": {
            "session": session, "kind": g.gap_kind, "id": g.id}})
        return g.question

    # ---- 用户接话
    def note_user_turn(self, session: str, text: str,
                       now: int | None = None) -> bool:
        """用户开口了：如果他接的是那个话头，标 answered。

        判据用实义块覆盖率（M18.3 的老工具）而不是"说了话就算回应"：
        用户回一句"嗯"就当作他回应了追问，冷遇检测就永远归零。
        """
        now = int(now if now is not None else self.wall())
        for r in self.store.curiosity_rows(session, limit=5):
            if r.outcome != "spoken":
                continue
            fresh = r.spoken_at is not None and 0 <= now - r.spoken_at <= ANSWER_GRACE
            if fresh or answer_overlap(r.question, text) >= 0.25:
                self.store.mark_curiosity_answered(r.id, now)
                return True
            return False          # 只比最近一条，别把历史问题也算进来
        return False

    # ---- 心跳入口
    def tick(self, now: int | None = None) -> int:
        now = int(now if now is not None else self.wall())
        self.store.sweep_curiosity_ignored(window=self.quota.response_seconds, now=now)
        n = 0
        for s in self.sessions(now):
            if self.prepare(s, now) is not None:
                n += 1
        return n

    # ---- 统计
    def stats(self, session: str = "", now: int | None = None) -> dict[str, Any]:
        now = int(now if now is not None else self.wall())
        s, _ = self.quota.day_bounds(now)
        rows = self.store.curiosity_rows(session, limit=50, since=s)
        spoken = [r for r in rows if r.spoken_at]
        answered = [r for r in rows if r.outcome == "answered"]
        ignored = [r for r in rows if r.outcome == "ignored"]
        return {
            "day": self.quota.day(now),
            "used": self.quota.used(now),
            "quota": self.quota.daily,
            "pending": len([r for r in rows if r.outcome == "pending"]),
            "spoken": len(spoken),
            "answered": len(answered),
            "ignored": len(ignored),
            "queue": self.store.pending_curiosity(session or "_", limit=5, now=now)
            if session else [],
        }


# ------------------------------------------------------------------ 便捷入口
def speak(store: Any, session: str, *, now: int | None = None,
          cfg: Any = None, tz: str | None = None) -> str | None:
    """打包时调：有排队的问题、额度也够，就把它交出去。"""
    return Curiosity(store, cfg, tz=tz).take(session, now)


def note_user_turn(store: Any, session: str, text: str, *,
                   now: int | None = None, cfg: Any = None) -> bool:
    """用户开口时调：看他是不是接了我们上次那个话头。"""
    if not text:
        return False
    return Curiosity(store, cfg).note_user_turn(session, text, now)


# ------------------------------------------------------------------ 只读约束
WRITE_TOOLS = ("write_event", "write_fact", "write_trait", "edit_file",
               "send_message", "run_shell")
READ_TOOLS = ("recall", "search_archive")


def toolset(explore: bool = False) -> tuple[str, ...]:
    """探索模式的工具集：空的。

    不是"过滤掉写工具"，是一个都不给。过滤式白名单的漏洞在于名字——
    明天加一个 `append_note`，它不在黑名单里就漏过去了。
    """
    return () if explore else READ_TOOLS + WRITE_TOOLS


@contextmanager
def explore_scope(store: Any) -> Iterator[None]:
    """进入只读模式。期间任何写库操作直接 PermissionError。

    在 store.tx() 里拦，而不是在 curiosity 里拦：写路径一共 38 个方法，
    在调用侧逐个记得加守卫是不可能的；tx() 是它们共同的闸门。
    """
    prev = getattr(store, "explore_mode", False)
    store.explore_mode = True
    try:
        yield
    finally:
        store.explore_mode = prev


def run_explore(store: Any, question: str,
                call: Callable[..., Any], **kwargs: Any) -> Any:
    """在只读模式下跑一次探索。想动手的，出去问用户。"""
    with explore_scope(store):
        return call(question, tools=toolset(explore=True), **kwargs)


def propose_check(store: Any, session: str, question: str,
                  now: int | None = None) -> str:
    """想查点库外的东西 → 先问用户。落一条 doubt 留痕。

    **必须在进入 explore_scope 之前调**：只读模式里连这条记录都写不进去，
    这正是"想查先问、问完再动"的物理保证。
    """
    now = now if now is not None else int(time.time())
    store.add_doubt(session=session, question=_as_question(question),
                    context="curiosity:check", confidence=0.5,
                    impact=0.6, cost=0.3, now=now)
    return f"这个我得查一下才能答：{_as_question(question)}要我去看看吗？"


# ------------------------------------------------------------------ 展示
def format_queue(gaps: list[Gap], *, limit: int = 5) -> str:
    if not gaps:
        return "好奇队列是空的（要么没什么想问的，要么都问过了）"
    lines = []
    for i, g in enumerate(gaps[:limit], 1):
        lines.append(f"{i}. [{g.priority:.2f}] {g.question}")
        lines.append(f"   {g.kind} · {g.why()}" + (f" · {g.detail}" if g.detail else ""))
    return "\n".join(lines)


def format_stats(stats: dict[str, Any], rows: list[Any] | None = None) -> str:
    lines = [f"{stats.get('day', '')}　已开口 {stats.get('used', 0)}"
             f" / {stats.get('quota', 0)} 次"]
    lines.append(f"等机会 {stats.get('pending', 0)} 条 · 说了 {stats.get('spoken', 0)} 条"
                 f" · 接话 {stats.get('answered', 0)} 条"
                 f" · 没接 {stats.get('ignored', 0)} 条")
    for r in (rows or [])[:5]:
        mark = {"pending": "…", "spoken": "→", "answered": "✓",
                "ignored": "·"}.get(str(r.outcome), "?")
        lines.append(f"  {mark} [{r.priority:.2f}] {r.question}")
    if not rows:
        lines.append("  （还没有。它一般不说话。）")
    return "\n".join(lines)
