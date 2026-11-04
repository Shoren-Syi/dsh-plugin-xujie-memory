"""有界读取（设计文档 11）。

四类查询，四种结果状态，硬预算。

    状态     state     「现在欠多少」——读投影，不用 top-k 临时加总
    历史     history   「都还过几次」——读事件流
    片段     episode   「上次聊海边那次」——线索检索
    原文     verbatim  「我当时原话怎么说的」——按 trace_id 下钻

结果的四种状态必须分开（I5）：
    found / partial / not_found / unavailable

not_found 表示「查过，没有」；unavailable 表示「没能查」。
把后者说成前者，就是把系统故障伪装成记忆缺陷。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from .engine import Engine, StateView
from .decay import Lifecycle

FOUND = "found"
PARTIAL = "partial"
NOT_FOUND = "not_found"
UNAVAILABLE = "unavailable"


@dataclass
class Budget:
    max_rounds: int = 2          # 最多几轮下钻
    max_candidates: int = 20     # 最多扫多少候选
    max_items: int = 5           # 最多返回几条
    max_chars: int = 2000        # 上下文总字符上限
    timeout_ms: int = 500

    def as_dict(self) -> dict[str, int]:
        return {
            "max_rounds": self.max_rounds, "max_candidates": self.max_candidates,
            "max_items": self.max_items, "max_chars": self.max_chars,
            "timeout_ms": self.timeout_ms,
        }


@dataclass
class RecallQuery:
    kind: str                              # state / history / episode / verbatim
    text: str | None = None                # episode 的检索词
    state_key: str | None = None           # state 的精确键
    relation: str | None = None            # state 的前缀过滤
    subject: str | None = None             # state 的前缀过滤
    trace_id: str | None = None            # verbatim 的目标
    as_of: str | None = None
    knowledge_as_of: str | None = None
    budget: Budget = field(default_factory=Budget)


@dataclass
class RecallResult:
    status: str
    query_kind: str
    scope_id: str
    states: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    traces: list[dict] = field(default_factory=list)
    pending_items: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    truncated: bool = False
    truncation_reason: str | None = None
    budget_used: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    evaluated_as_of: str = ""

    # ------------------------------------------------------------- 装配
    def render(self, max_chars: int | None = None) -> str:
        """分区装配。reconstruct 只装配，不创作事实（11.7）。"""
        blocks: list[str] = []

        accepted = [s for s in self.states if s["status"] in ("known", "stale")]
        if accepted:
            lines = ["【已接受状态】"]
            for s in accepted:
                lines.append(f"  {s['state_key'].split('|', 1)[-1]} = {s['value']}"
                             f"  ({s['status']})")
            blocks.append("\n".join(lines))

        events = [s for s in self.states if s["status"] == "event"]
        if events:
            lines = ["【事件流（按生效时间）】"]
            for e in events:
                lines.append(f"  #{e['version']}  {e['value']}")
            blocks.append("\n".join(lines))

        disputed = [s for s in self.states if s["status"] == "disputed"]
        if disputed:
            # 值也一起给。有争议不等于没有值——把值藏起来，
            # 用户会以为系统"什么都不知道"，而它其实算出了一个数、
            # 只是这个数建立在一条有问题的记录上。
            # **「40，但这笔存疑」比「不知道」有用得多。**
            lines = ["【争议中：值仅供参考，不能当作事实】"]
            for s in disputed:
                value = s.get("value")
                shown = f" = {value}" if value is not None else ""
                lines.append(f"  {s['state_key'].split('|', 1)[-1]}{shown}"
                             f"  原因 {s.get('conflict_reason')}")
            blocks.append("\n".join(lines))

        if self.pending_items:
            lines = ["【待确认，尚未生效】"]
            for p in self.pending_items:
                lines.append(f"  {p.get('excerpt', p.get('relation', ''))}"
                             f"  ({p.get('kind', '')})")
            blocks.append("\n".join(lines))

        if self.observations:
            lines = ["【曾说过，但不是事实】"]
            for o in self.observations:
                lines.append(f"  「{o['excerpt']}」  模态={o['modality']}")
            blocks.append("\n".join(lines))

        if self.traces:
            lines = ["【相关经历】"]
            for t in self.traces:
                lines.append(f"  [{t['trace_id'][:12]}] {t['content'][:120]}")
            blocks.append("\n".join(lines))

        if not blocks:
            blocks.append("【无】没有找到可用材料。")

        text = "\n\n".join(blocks)
        if self.truncated:
            text += f"\n\n（注意：结果不完整，原因 {self.truncation_reason}）"
        limit = max_chars or 2000
        return text if len(text) <= limit else text[:limit] + "…（已截断）"


class Recall:
    def __init__(self, engine: Engine):
        self.engine = engine

    # ============================================================ 入口
    def query(self, scope_id: str, q: RecallQuery) -> RecallResult:
        started = time.monotonic()
        used = {"rounds": 0, "candidates": 0, "items": 0, "chars": 0}
        result = RecallResult(
            status=FOUND, query_kind=q.kind, scope_id=scope_id,
            budget_used=used, evaluated_as_of=q.as_of or self.engine.now(),
            coverage={"scope_id": scope_id, "budget": q.budget.as_dict()},
        )

        try:
            handler = {
                "state": self._query_state,
                "history": self._query_history,
                "episode": self._query_episode,
                "verbatim": self._query_verbatim,
            }.get(q.kind)
            if handler is None:
                raise ValueError(f"unknown query kind {q.kind!r}")
            handler(scope_id, q, result, used)
        except sqlite3.Error as exc:
            result.status = UNAVAILABLE
            result.errors.append(f"storage_error: {exc}")
            return result
        except ValueError as exc:
            result.status = NOT_FOUND
            result.errors.append(str(exc))
            return result

        used["rounds"] += 1
        elapsed_ms = (time.monotonic() - started) * 1000
        result.coverage["elapsed_ms"] = round(elapsed_ms, 2)

        # ---- 超时
        if elapsed_ms > q.budget.timeout_ms:
            result.status = PARTIAL
            result.truncated = True
            result.truncation_reason = "timeout"
            return result

        # ---- 空结果
        if not (result.states or result.traces or result.observations or result.pending_items):
            result.status = NOT_FOUND
            return result

        # ---- 预算截断
        if result.truncated:
            result.status = PARTIAL
        return result

    # ============================================================ 四类
    def _query_state(self, scope_id: str, q: RecallQuery, r: RecallResult, used: dict) -> None:
        conn = self.engine._conn()

        if q.state_key:
            keys = [q.state_key]
        elif q.subject and q.relation:
            prefix = f"{scope_id}|{q.subject}|{q.relation}|"
            rows = conn.execute(
                "SELECT state_key FROM states WHERE state_key LIKE ? LIMIT ?",
                (prefix + "%", q.budget.max_candidates),
            ).fetchall()
            used["candidates"] += len(rows)
            keys = [row["state_key"] for row in rows]
        else:
            rows = conn.execute(
                "SELECT state_key FROM states WHERE scope_id=? LIMIT ?",
                (scope_id, q.budget.max_candidates),
            ).fetchall()
            used["candidates"] += len(rows)
            keys = [row["state_key"] for row in rows]

        if len(keys) > q.budget.max_items:
            keys = keys[: q.budget.max_items]
            r.truncated = True
            r.truncation_reason = "max_items"

        for key in keys:
            state = self.engine.get_state(key, q.as_of, q.knowledge_as_of)
            # unknown 有两种来源，必须分开（I5）：
            #   从来没有任何事件   → 这条关系没聊过 → 不计入结果，最终 not_found
            #   有事件但依据已撤销 → 曾经有过、现在没依据 → 保留，状态 unknown
            if state.status == "unknown" and not self.engine.events_of(key):
                continue
            r.states.append({
                "state_key": state.state_key, "status": state.status,
                "value": state.value, "version": state.version,
                "watermark": state.watermark,
                "conflict_reason": state.conflict_reason,
                "spec_version": state.spec_version,
            })
            used["items"] += 1
            used["chars"] += len(json.dumps(state.value, ensure_ascii=False))

        # 附带待确认项（11.4：不混入 states）
        rows = conn.execute(
            "SELECT * FROM clarifications WHERE scope_id=? AND status='open' LIMIT ?",
            (scope_id, q.budget.max_items),
        ).fetchall()
        for row in rows:
            detail = json.loads(row["detail_json"] or "{}")
            r.pending_items.append({
                "request_id": row["request_id"], "kind": row["kind"],
                "relation": detail.get("relation", ""),
                "excerpt": detail.get("hint", detail.get("kind", "")),
            })

    def _query_history(self, scope_id: str, q: RecallQuery, r: RecallResult, used: dict) -> None:
        if not q.state_key:
            raise ValueError("history query requires state_key")
        events = self.engine.events_of(q.state_key, include_void=True)
        used["candidates"] += len(events)
        if len(events) > q.budget.max_items:
            events = events[-q.budget.max_items:]
            r.truncated = True
            r.truncation_reason = "max_items"

        for ev in events:
            payload = json.loads(ev["payload_json"])
            r.states.append({
                "state_key": ev["state_key"], "status": "event",
                "value": payload, "version": ev["sequence_no"],
                "void": bool(ev["void"]),
                "watermark": ev["sequence_no"],
                "conflict_reason": None, "spec_version": ev["spec_version"],
            })
            used["items"] += 1

    def _query_episode(self, scope_id: str, q: RecallQuery, r: RecallResult, used: dict) -> None:
        if not q.text:
            raise ValueError("episode query requires text")
        conn = self.engine._conn()

        # trigram 要求查询 >= 3 字符；短查询降级为 LIKE（明确记录降级，不假装等价）
        if len(q.text) >= 3:
            rows = conn.execute(
                "SELECT f.trace_id FROM traces_fts f"
                " JOIN traces t ON t.trace_id = f.trace_id"
                " WHERE traces_fts MATCH ? AND t.scope_id = ? AND t.erased_at IS NULL"
                " LIMIT ?",
                # v0.52：转义双引号。旧版用户一句话里带 `"` 就是 FTS 语法错误，
                # 整轮 500，这一轮没有记忆。
                ('"' + q.text.replace('"', '""') + '"', scope_id,
                 q.budget.max_candidates),
            ).fetchall()
            r.coverage["search"] = "fts_trigram"
        else:
            rows = conn.execute(
                "SELECT trace_id FROM traces WHERE scope_id=? AND content LIKE ?"
                " AND erased_at IS NULL LIMIT ?",
                (scope_id, f"%{q.text}%", q.budget.max_candidates),
            ).fetchall()
            r.coverage["search"] = "like_fallback"

        used["candidates"] += len(rows)
        if len(rows) > q.budget.max_items:
            rows = rows[: q.budget.max_items]
            r.truncated = True
            r.truncation_reason = "max_items"

        for row in rows:
            t = self.engine.get_trace(row["trace_id"])
            if t is None:
                continue
            r.traces.append({
                "trace_id": t["trace_id"], "content": t["content"],
                "actor_id": t["actor_id"], "source_kind": t["source_kind"],
                "recorded_at": t["recorded_at"],
            })
            used["items"] += 1
            used["chars"] += len(t["content"])

    def _query_verbatim(self, scope_id: str, q: RecallQuery, r: RecallResult, used: dict) -> None:
        if not q.trace_id:
            raise ValueError("verbatim query requires trace_id")
        t = self.engine.get_trace(q.trace_id)
        if t is None:
            return
        if t["scope_id"] != scope_id:
            # 跨作用域一律当作不存在，不泄漏"存在但无权"（9.3）
            return
        if t["erased_at"]:
            r.errors.append("trace_erased")
            return
        r.traces.append({
            "trace_id": t["trace_id"], "content": t["content"],
            "actor_id": t["actor_id"], "source_kind": t["source_kind"],
            "recorded_at": t["recorded_at"],
        })
        used["items"] += 1
        used["chars"] += len(t["content"])

    # ============================================================ 便捷方法
    def state(self, scope_id: str, state_key: str, **kw) -> RecallResult:
        return self.query(scope_id, RecallQuery(kind="state", state_key=state_key, **kw))

    def history(self, scope_id: str, state_key: str, **kw) -> RecallResult:
        return self.query(scope_id, RecallQuery(kind="history", state_key=state_key, **kw))

    def episode(self, scope_id: str, text: str, **kw) -> RecallResult:
        return self.query(scope_id, RecallQuery(kind="episode", text=text, **kw))

    def verbatim(self, scope_id: str, trace_id: str, **kw) -> RecallResult:
        return self.query(scope_id, RecallQuery(kind="verbatim", trace_id=trace_id, **kw))

    # ============================================================ 拉回
    def pull(
        self,
        scope_id: str,
        text: str,
        *,
        limit: int = 3,
        budget: Budget | None = None,
        exclude: str = "",
    ) -> RecallResult:
        """**拉回**：把相关的旧记忆按权重捞回来。

        这是"无感"那一半的落点：用户没问，系统自己把相关的东西浮上来。

        和 `episode()` 的区别有两处，都是为了让拉回**真的能用**：

        1. **按权重排序，不按检索相关度排序**。
           episode 是"这段对话里出现过这个词的痕迹"，按时间倒；
           pull 是"我要拿东西出来用"，那就该按**该拿哪个先拿**排：
           锚点最前（用户明说"记住这个"），其次是当前权重高的（被用过的、没淡掉的）。
           检索相关度只用来**圈定候选**，不用来决定**给谁看**。
        2. **不重置衰减**。命中就命中，不当成"被用"。
           自动预取每轮都会捞一遍——如果每次都刷权重，
           所有记忆都会永不衰减，遗忘就整个失效了（10.3）。

        命中的痕迹会记一条 `recalled` 事件（事实：它浮上来过），
        但 `use_count` 与 `last_used_at` 都不动。
        """
        budget = budget or Budget(max_items=max(limit * 3, limit),
                                  max_candidates=max(limit * 6, limit))
        result = self.episode(scope_id, text, budget=budget)
        # v0.52：先排除当前这句再判断「落空」。旧版里当前这句总会命中自己，
        # 于是模糊兜底永远不触发，上层过滤掉它之后就只剩「最近说过的话」——
        # 「你还记得我欠谁钱吗」捞不回「我欠老李 500 块」。
        if exclude:
            result.traces = [t for t in result.traces if t["trace_id"] != exclude]

        # FTS trigram 对 1–2 个字的查询基本无能为力（LIKE 只认完整子串）。
        # 中文短词很常见：「奶茶」「房租」这种两字查询才是日常。
        # 精确检索落空时，用字符二元组模糊捞一遍候选。
        if not result.traces and text:
            from .memory import Memory
            hits = Memory(self.engine).search(scope_id, text, limit=budget.max_items + 1, exclude=exclude)
            hits = [h for h in hits if h["trace_id"] != exclude][: budget.max_items]
            result.traces = [
                {
                    "trace_id": h["trace_id"],
                    "content": h["content"],
                    "actor_id": "unknown",
                    "source_kind": "fuzzy",
                    "recorded_at": h["recorded_at"],
                    "priority": h.get("priority"),
                    "anchor": bool(h.get("anchor")),
                }
                for h in hits
            ]
            result.coverage["search"] = "fuzzy_bigram"

        if result.traces:
            trace_ids = [t["trace_id"] for t in result.traces]
            weights = Lifecycle(self.engine).batch_priority(trace_ids)

            def sort_key(t: dict) -> tuple:
                w = weights.get(t["trace_id"], {})
                # 锚点在前（用户明确说要留住的），
                # 然后权重高的在前（被用过、还没淡掉的），
                # 最后按时间倒（新的比旧的相关）。
                anchored = 1 if w.get("anchor") else 0
                return (-anchored, -float(w.get("priority") or 0.0),
                        t.get("recorded_at") or "")

            result.traces = sorted(result.traces, key=sort_key)[:limit]
            for t in result.traces:
                w = weights.get(t["trace_id"], {})
                t["anchor"] = bool(w.get("anchor"))
                t["priority"] = w.get("priority")
                t["importance"] = w.get("base")
                Lifecycle(self.engine).note_recall(t["trace_id"], scope_id)

        result.coverage["pull_limit"] = limit
        return result
