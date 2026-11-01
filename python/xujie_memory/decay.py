"""衰减：召回优先级与存储层级。

设计文档 10：
    衰减只影响召回优先级和存储层级，不改变事实是否成立。
    「很久没提过欠款」不意味着欠款自动消失。

所以这里的任何函数都不写 `states`。状态是否过期由规格的 validity 决定，
不由权重决定——两件事必须分开，否则「忘了」会变成「不算了」。
"""

from __future__ import annotations

import itertools
import json
import math
import threading
from datetime import datetime, timedelta, timezone

from .engine import canonical_json, digest

# 默认参数。alpha 控制使用次数的增益，beta 控制时间衰减速度。
DEFAULT_ALPHA = 1.0
DEFAULT_BETA = 0.05          # 约 14 天权重折半

# 权重事件主键的进程内序号。
#
# **必须是模块级，不能是实例级**：`Lifecycle` 是个便宜的包装类，
# 调用方每处都 `Lifecycle(engine)` 新建一个（`Recall.pull` 就是）。
# 实例级序号每次都从 0 开始，于是同一秒内两次拉回同一条记忆时，
# 算出的 event_id 完全一样，`weight_events` 主键直接冲突。
# 这不是"理论上的并发问题"——它一次普通的连续检索就能触发。
_WEV_SEQ = itertools.count(1)
_WEV_LOCK = threading.Lock()


def _next_wev_seq() -> int:
    with _WEV_LOCK:
        return next(_WEV_SEQ)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def elapsed_days(last_used_at: str | None, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    then = _parse(last_used_at)
    if then is None:
        return 0.0
    return max(0.0, (now - then).total_seconds() / 86400.0)


def priority(
    base: float,
    use_count: int,
    last_used_at: str | None,
    now: datetime | None = None,
    alpha: float = DEFAULT_ALPHA,
    beta: float = DEFAULT_BETA,
) -> float:
    """priority = base × (1 + α·log(1+use_count)) × exp(-β·elapsed_days)

    I2 不约束这个值：召回顺序不可重放（8.2 已声明）。

    `last_used_at` 为空时**必须**由调用方给一个起点（通常是 created_at）。
    早先这里直接传 None，elapsed_days 返回 0，于是「从没被访问过的记忆」
    优先级恒为 1.0——**永远不会被遗忘，而它们恰恰是最该被忘的那批。**
    """
    days = elapsed_days(last_used_at, now)
    return base * (1.0 + alpha * math.log(1 + max(0, use_count))) * math.exp(-beta * days)


def stable_order(items: list[dict], key: str = "priority") -> list[dict]:
    """同分时用稳定次序，保证输出可复现（8.2）。"""
    return sorted(items, key=lambda x: (-x.get(key, 0.0), x.get("ref_id", "")))


class Lifecycle:
    """痕迹的生命周期元信息。不改内容，只改权重与层级。"""

    def __init__(self, engine):
        self.engine = engine

    # ------------------------------------------------------------- 读
    def get(self, ref_id: str) -> dict:
        row = self.engine._conn().execute(
            "SELECT * FROM lifecycle WHERE ref_type='trace' AND ref_id=?", (ref_id,)
        ).fetchone()
        if row is None:
            return {
                "ref_id": ref_id, "base": 1.0, "use_count": 0, "last_used_at": None,
                "anchor": 0, "level": "hot", "priority": 1.0,
            }
        d = dict(row)
        # 锚点不衰减：优先级恒等于 base（10.4）。这不是"排最前"，
        # 而是"时间拿不走它"——两者语义不同，别混。
        #
        # 起点取 last_used_at，没有就取 created_at。
        # 用 None 会让 elapsed_days 返回 0，等于「从没被访问的记忆永不衰减」。
        since = d["last_used_at"] or d["created_at"]
        d["priority"] = d["base"] if d["anchor"] else priority(
            d["base"], d["use_count"], since, now=_parse(self.engine.now())
        )
        return d

    # ------------------------------------------------------------- 写
    def ensure(self, scope_id: str, ref_id: str, base: float = 1.0) -> None:
        self.engine._conn().execute(
            "INSERT OR IGNORE INTO lifecycle(ref_type, ref_id, scope_id, base, created_at)"
            " VALUES ('trace', ?, ?, ?, ?)",
            (ref_id, scope_id, base, self.engine.now()),
        )

    def _record_weight_event(self, scope_id: str, ref_id: str, kind: str,
                             detail: dict | None = None) -> None:
        """记一条**权重事实**事件。

        为什么记事件而不是直接改计数：`use_count` 是一个**计数**，
        不是**历史**。计数回答不了"三个月前这条记忆有多重要"。
        曲线（v0.40.0）要的就是历史。

        为什么不存 priority：priority 是派生值，存下来就有两个真相源
        （CONTRIBUTING 红线四）。这里只记"发生了什么"，
        权重的值由 `curve()` 重放算出来。

        幂等：同一秒内的同一事件可能重复触发（重试、并发）。
        这里不去重——重放出来的曲线多一个点不影响形状，
        而去重需要额外的主键设计（时间戳做主键天然不可靠）。
        """
        self.engine._conn().execute(
            "INSERT INTO weight_events(event_id, ref_type, ref_id, scope_id, kind,"
            " occurred_at, detail_json) VALUES (?, 'trace', ?, ?, ?, ?, ?)",
            (
                # 主键不能是「ref_id + kind + 时间戳」：同一秒内的重复事件
                # （重试、并发）会撞主键，INSERT 直接报错。
                # 进程内单调序号保证每次都是新行——重复事件多一个点，
                # 不影响曲线形状（这正是"不存派生值"的代价：靠事件重放，
                # 不用去重表）。
                digest("wev", ref_id, kind, self.engine.now(),
                       str(_next_wev_seq())),
                ref_id, scope_id, kind, self.engine.now(),
                canonical_json(detail) if detail else None,
            ),
        )

    def set_importance(self, ref_id: str, importance: float,
                       scope_id: str | None = None, *,
                       reasons: list[str] | None = None,
                       source: str = "manual") -> dict:
        """手动标注「这条记忆有多重要」（1.0 普通，5.0 最重要）。

        它改的是 `base`，所以会乘在衰减曲线上：重要的事淡得慢。
        和锚点的区别：锚点是「时间拿不走它」，重要性是「淡得慢一点」。
        """
        value = max(0.1, min(5.0, float(importance)))
        conn = self.engine._conn()
        scope = scope_id or self._scope_of(ref_id)
        exists = conn.execute(
            "SELECT 1 FROM lifecycle WHERE ref_type='trace' AND ref_id=?", (ref_id,)
        ).fetchone()
        if exists is None and scope:
            self.ensure(scope, ref_id, value)
        conn.execute(
            "UPDATE lifecycle SET base=? WHERE ref_type='trace' AND ref_id=?",
            (value, ref_id),
        )
        if scope:
            self._record_weight_event(
                scope, ref_id, "importance",
                {"value": value, "source": source, "reasons": list(reasons or [])},
            )
        return self.get(ref_id)

    def touch(self, ref_id: str, from_search: bool = True,
              scope_id: str | None = None) -> None:
        """强化发生在「被用」，不是「被搜到」（10.3）。

        from_search=True 表示这次只是检索命中，**不增加 use_count**，
        只更新 last_used_at——否则自动预取会让错误记忆自我强化（T13）。
        """
        conn = self.engine._conn()
        now = self.engine.now()
        if from_search:
            conn.execute(
                "UPDATE lifecycle SET last_used_at=? WHERE ref_type='trace' AND ref_id=?",
                (now, ref_id),
            )
        else:
            conn.execute(
                "UPDATE lifecycle SET use_count=use_count+1, last_used_at=?"
                " WHERE ref_type='trace' AND ref_id=?",
                (now, ref_id),
            )
        scope = scope_id or self._scope_of(ref_id)
        if scope:
            self._record_weight_event(
                scope, ref_id, "search" if from_search else "use",
            )

    def _scope_of(self, ref_id: str) -> str | None:
        row = self.engine._conn().execute(
            "SELECT scope_id FROM lifecycle WHERE ref_type='trace' AND ref_id=?",
            (ref_id,),
        ).fetchone()
        return row["scope_id"] if row else None

    # ------------------------------------------------------------- 批量读
    def batch_priority(self, ref_ids: list[str]) -> dict[str, dict]:
        """一次算多条痕迹的当前权重。

        为什么要有这个：召回一次要比较几十条候选，
        一条一条 `get()` 就是几十次查询——而且 `get()` 每次都重算一次
        priority，同一批里每条的衰减起点还不一样。
        这里一次读完，再统一按**同一个 now** 算，排序才有可比性。
        """
        if not ref_ids:
            return {}
        now = _parse(self.engine.now())
        marks = ",".join("?" * len(ref_ids))
        rows = self.engine._conn().execute(
            f"SELECT * FROM lifecycle WHERE ref_type='trace' AND ref_id IN ({marks})",
            ref_ids,
        ).fetchall()
        by_id = {r["ref_id"]: r for r in rows}
        out: dict[str, dict] = {}
        for rid in ref_ids:
            row = by_id.get(rid)
            if row is None:
                out[rid] = {"ref_id": rid, "base": 1.0, "use_count": 0,
                            "anchor": 0, "priority": 0.0, "level": "hot",
                            "last_used_at": None}
                continue
            since = row["last_used_at"] or row["created_at"]
            p = row["base"] if row["anchor"] else priority(
                row["base"], row["use_count"], since, now=now
            )
            out[rid] = {
                "ref_id": rid, "base": row["base"], "use_count": row["use_count"],
                "anchor": row["anchor"], "priority": p, "level": row["level"],
                "last_used_at": row["last_used_at"],
            }
        return out

    def anchored(self, scope_id: str) -> list[dict]:
        """这个作用域里被设为锚点的痕迹，按设定时间排。

        「重点锚点」要能被看见——不然它只是库里一个没人读的列。
        """
        rows = self.engine._conn().execute(
            "SELECT ref_id FROM lifecycle WHERE ref_type='trace'"
            " AND scope_id=? AND anchor=1",
            (scope_id,),
        ).fetchall()
        weights = self.batch_priority([r["ref_id"] for r in rows])
        return [weights[r["ref_id"]] for r in rows if r["ref_id"] in weights]

    def note_recall(self, ref_id: str, scope_id: str | None = None) -> None:
        """记一条「这条被拉回过」的事实事件。

        为什么单独记一种事件，不直接用 `touch(from_search=True)`：
        `touch` 会更新 `last_used_at`，而 `last_used_at` 是**衰减的起点**——
        改了它，这条记忆的衰减就重新开始算。
        于是「自动预取把每条都捞一遍」会让**所有记忆都不再衰减**，
        遗忘就整个失效了（10.3 的原意：被搜到不等于被需要）。

        所以这里只记事实：**这条被召回命中过一次**。
        它不改变 use_count、不改变 last_used_at、不改变 anchor。
        曲线重放时它只是一个「这天它浮上来过」的点。
        """
        scope = scope_id or self._scope_of(ref_id)
        if scope:
            self._record_weight_event(scope, ref_id, "recalled")

    def set_anchor(self, ref_id: str, anchor: bool) -> None:
        """锚点只影响保留与排序，不代表更高可信度（I7）。"""
        conn = self.engine._conn()
        exists = conn.execute(
            "SELECT 1 FROM lifecycle WHERE ref_type='trace' AND ref_id=?", (ref_id,)
        ).fetchone()
        if exists is None:
            trace = self.engine.get_trace(ref_id)
            if trace:
                self.ensure(trace["scope_id"], ref_id)
        conn.execute(
            "UPDATE lifecycle SET anchor=? WHERE ref_type='trace' AND ref_id=?",
            (1 if anchor else 0, ref_id),
        )
        scope = self._scope_of(ref_id)
        if scope:
            self._record_weight_event(
                scope, ref_id, "anchor" if anchor else "unanchor",
            )

    # ------------------------------------------------------------- 曲线
    def curve(self, ref_id: str, points: int = 12,
              now: datetime | None = None) -> list[dict]:
        """这条记忆的权重曲线。

        **返回的是重放出来的历史**，不是库里存的某个值——
        `lifecycle` 里没有 priority 这一列（红线四）。

        怎么算：
        1. 读出这条记忆的全部权重事件，按时间排；
        2. 从 `base` 出发，每遇到一个 `use` 事件就把 use_count 加一、
           把 last_used_at 推到那一刻；`search` 只推 last_used_at；
           `anchor` 把锚点状态打开（锚点不衰减）；
        3. 在时间轴上等距取点，每点算一次 priority。

        为什么要有这条曲线：光看"现在权重多少"答不了
        "它是什么时候变得重要的"。而后者才是"重点锚点"和
        "权重回升"这两件事能被用户看见的地方。
        """
        now = now or _parse(self.engine.now())
        row = self.engine._conn().execute(
            "SELECT * FROM lifecycle WHERE ref_type='trace' AND ref_id=?", (ref_id,)
        ).fetchone()
        if row is None:
            return []

        events = self.engine._conn().execute(
            "SELECT kind, occurred_at, detail_json FROM weight_events"
            " WHERE ref_type='trace' AND ref_id=? ORDER BY occurred_at",
            (ref_id,),
        ).fetchall()

        # ---- 重放出状态时间轴
        # 每段是 (从这一刻起, use_count, last_used_at, anchor, base)
        timeline: list[tuple[datetime, int, datetime | None, bool, float]] = []
        use_count = 0
        last_used: datetime | None = None
        anchor = bool(row["anchor"])
        # 有重要性事件时，起点按默认 1.0 还原；没有就用当前 base。
        has_importance = any(ev["kind"] == "importance" for ev in events)
        current_base = 1.0 if has_importance else float(row["base"])

        created = _parse(row["created_at"]) or now
        timeline.append((created, 0, None, bool(row["anchor"]), current_base))

        for ev in events:
            at = _parse(ev["occurred_at"]) or created
            if ev["kind"] == "use":
                use_count += 1
                last_used = at
            elif ev["kind"] == "search":
                last_used = at
            elif ev["kind"] == "anchor":
                anchor = True
            elif ev["kind"] == "unanchor":
                anchor = False
            elif ev["kind"] == "importance":
                try:
                    current_base = float(json.loads(ev["detail_json"] or "{}").get("value", current_base))
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            # "recalled"：被召回命中过。它**不改变任何状态**——
            # 不是为了重置衰减，只是让"这天它浮上来过"这件事留下痕迹。
            elif ev["kind"] == "recalled":
                pass
            timeline.append((at, use_count, last_used, anchor, current_base))

        # ---- 在时间轴上等距取点
        start = timeline[0][0]
        span = max((now - start).total_seconds(), 0.0)
        out: list[dict] = []
        for i in range(points):
            frac = i / max(points - 1, 1)
            at = start + timedelta(seconds=span * frac)

            # 该时刻的 use_count / last_used_at / anchor：取最后一个不晚于 at 的段
            use_at = 0
            used_at = None
            anchored = False
            base_at = current_base
            for t_at, t_use, t_last, t_anchor, t_base in timeline:
                if t_at <= at:
                    use_at = t_use
                    used_at = t_last
                    anchored = t_anchor
                    base_at = t_base
                else:
                    break

            if anchored:
                # 锚点不衰减（10.4）：优先级恒等于 base。
                # 这不是"排最前"，是"时间拿不走它"——两者语义不同。
                p = base_at
            else:
                since = (used_at or start).isoformat()
                p = priority(base_at, use_at, since, now=at)

            out.append({
                "at": at.isoformat(),
                "priority": p,
                "use_count": use_at,
                "anchor": anchored,
                "base": base_at,
                "days": round((at - start).total_seconds() / 86400.0, 2),
            })
        return out

    # ------------------------------------------------------------- 归档
    def archive(self, ref_id: str) -> bool:
        """归档前必须验证可恢复性（12.1）。

        单库实现里「归档」是逻辑标记，不迁物理位置。真迁移属部署层。
        这里要验的是：原文还在，且没有被擦除——否则归档等于销毁。
        """
        trace = self.engine.get_trace(ref_id)
        if trace is None or trace["erased_at"]:
            return False
        self.engine._conn().execute(
            "UPDATE lifecycle SET level='archived' WHERE ref_type='trace' AND ref_id=?",
            (ref_id,),
        )
        return True

    def restore(self, ref_id: str) -> bool:
        row = self.engine._conn().execute(
            "SELECT level FROM lifecycle WHERE ref_type='trace' AND ref_id=?", (ref_id,)
        ).fetchone()
        if row is None or row["level"] != "archived":
            return False
        self.engine._conn().execute(
            "UPDATE lifecycle SET level='hot' WHERE ref_type='trace' AND ref_id=?",
            (ref_id,),
        )
        return True

    def sweep(
        self,
        scope_id: str,
        threshold: float = 0.05,
        min_age_days: float = 30.0,
        limit: int = 100,
        dry_run: bool = False,
    ) -> list[str]:
        """把低权重且足够旧的痕迹标为 cold / archived。

        锚点永不被扫走。活跃状态所依赖的痕迹也不扫——那是 XM-07 的边界，
        这里只按权重做粗筛，调用方需要自己排除「仍被有效状态引用」的痕迹。

        `dry_run=True` 只算不动，返回"本会动哪些"。整合周期（consolidate）
        需要先在候选里剔掉"还在给有效状态作证"的那批，才能真归档；
        没有这个开关，它只能先扫一遍再挨个还原回去。
        """
        rows = self.engine._conn().execute(
            "SELECT * FROM lifecycle WHERE scope_id=? AND level='hot' AND anchor=0 LIMIT ?",
            (scope_id, limit),
        ).fetchall()

        # 必须用**引擎时钟**，不能用真实时钟。
        #
        # 早先这里两个调用都没传 now，于是 elapsed_days 退回 datetime.now()。
        # 后果不是算错，是**两处算法不一致**：`Lifecycle.get()` 用引擎时钟，
        # `sweep()` 用真实时钟——同一条痕迹，一个说优先级 0.9，一个说该归档。
        #
        # 更糟的是它让假时钟失效：演示里"过了四个月"根本不成立，
        # 因为真实时间早就过去了。而假时钟存在的唯一理由就是让遗忘可复现。
        now = _parse(self.engine.now())

        moved: list[str] = []
        for row in rows:
            since = row["last_used_at"] or row["created_at"]
            days = elapsed_days(since, now)
            if days < min_age_days:
                continue
            p = priority(row["base"], row["use_count"], since, now=now)
            if p >= threshold:
                continue
            level = "archived" if p < threshold / 4 else "cold"
            if not dry_run:
                self.engine._conn().execute(
                    "UPDATE lifecycle SET level=? WHERE ref_type='trace' AND ref_id=?",
                    (level, row["ref_id"]),
                )
            moved.append(row["ref_id"])
        return moved
