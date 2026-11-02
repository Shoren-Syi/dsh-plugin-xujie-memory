"""内核：痕迹、事件、状态投影、双时间、重算、依赖传播。

阶段 1：作用域、痕迹、幂等、投影、依赖链（T01/T03/T05）
阶段 2：双时间、纠正撤销、按状态键重算、依赖图与原子事件组（T04/T06/T07/T08）

本模块不调用任何模型。所有判定都是确定性函数。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .errors import (
    EventAlreadyVoid,
    LockTimeout,
    DependencyCycle,
    EvidenceMissing,
    IdempotencyConflict,
    ScopeNotFound,
    SpecNotFound,
    TraceErased,
    TraceNotFound,
    VersionConflict,
)
from .specs import RelationSpec, SpecRegistry

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


# ============================================================ 工具
def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def fixed_clock(start: str, step_seconds: float = 0.0):
    """可推进的假时钟，用于模拟"过了 30 天"。

    没有它就没法测试遗忘：真实的 30 天不能等，而 monkeypatch 全局函数
    会让测试之间互相污染。**遗忘是这个项目最核心的行为之一，
    它必须是可复现的。**
    """
    from datetime import timedelta

    current = datetime.fromisoformat(start)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    class Clock:
        def __call__(self) -> str:
            return current.isoformat(timespec="microseconds")

        def advance(self, **kwargs) -> str:
            nonlocal current
            current = current + timedelta(**kwargs)
            return self()

        def set(self, when: str) -> str:
            nonlocal current
            current = datetime.fromisoformat(when)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            return self()

    return Clock()


def canonical_json(value: Any) -> str:
    """规范化序列化。I2 的比较基础：键排序、无多余空白、不转义非 ASCII。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def make_state_key(
    scope_id: str,
    subject: str,
    relation: str,
    obj: str | None = None,
    **dims: str,
) -> str:
    """状态键必须稳定、可读、可比较。业务维度按名称排序，避免顺序影响。"""
    parts = [scope_id, subject, relation, obj or "-"]
    for k in sorted(dims):
        parts.append(f"{k}={dims[k]}")
    return "|".join(parts)


def relation_of(state_key: str) -> str:
    parts = state_key.split("|")
    return parts[2] if len(parts) >= 3 else ""


# ============================================================ 值对象
@dataclass(frozen=True)
class StateView:
    state_key: str
    status: str            # known / unknown / disputed / stale / retracted
    value: Any
    last_known: Any
    version: int           # 有效事件数，重算后稳定
    watermark: int         # 已投影到的最大 sequence_no
    conflict_reason: str | None
    spec_version: str

    @property
    def is_usable(self) -> bool:
        return self.status in ("known", "stale")


@dataclass
class CommitResult:
    event_id: str
    state_key: str
    sequence_no: int
    applied: bool          # False 表示命中幂等，未重复生效
    state: StateView
    # 这次提交**顺带作废**了几条事件。
    #
    # 目前只有一个来源：改初始值时，之后的余额快照会一起作废（XM-33）。
    # 放在这里而不是写日志，是因为调用方需要知道"我这一步不只改了一个数"——
    # 一次操作动了三条事件却不告诉调用方，就是另一种静默。
    cascaded: int = 0


@dataclass
class Assertion:
    """一条待提交的领域变更。阶段 1 由调用方直接构造，阶段 3 由闸门产出。"""
    scope_id: str
    state_key: str
    relation: str
    operation: str                  # append / update / retract / conflict
    effect_kind: str                # set / delta / invalidate
    payload: dict[str, Any]
    idempotency_key: str
    evidence_trace_ids: tuple[str, ...]
    actor_id: str
    effective_at: str | None = None   # 业务生效时间；缺省等于 recorded_at
    recorded_at: str | None = None    # 系统获知时间；一般由内核生成
    expected_version: int | None = None
    spec_version: str | None = None
    gate_decision_id: str | None = None
    operation_group_id: str | None = None
    target_ref: str | None = None
    # 这句话属于哪一类句式（准入熔断用）。空字符串 = 调用方不区分。
    # 它进事件是为了**事后能追责**：一条 auto 提交被纠正时，
    # 要知道该关哪个格子——没有它就只能关掉整个关系。
    pattern: str = ""


# ============================================================ 引擎
class Engine:
    def __init__(
        self,
        db_path: str | Path,
        registry: SpecRegistry,
        clock: Callable[[], str] | None = None,
    ):
        self.db_path = str(db_path)
        self.registry = registry
        self.registry.assert_acyclic()
        # 时钟可注入。默认是真实时间；测试和"模拟过了很久"的演示传自己的。
        self._clock = clock or utcnow
        self._local = threading.local()
        # 每个线程一条连接，但 close() 必须能关掉全部：
        # 并发示例里 8 个 worker 各建一条，主线程 close() 只关自己那条，
        # 剩下的在 Windows 上会让库文件删不掉（POSIX 不会，所以一直没暴露）。
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._derivations: dict[str, tuple[tuple[str, ...], str, Callable]] = {}
        self._init_schema()

    # -------------------------------------------------------- 连接
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path, timeout=30.0, isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            # WAL 下 NORMAL 不会损坏库，只可能在断电时丢最后一个事务；
            # FULL 每次提交都 fsync，是旧版慢的原因之一。
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    def now(self) -> str:
        """当前时间。全库只通过这里取时间，方便模拟"过了很久"。"""
        return self._clock()

    def close_thread_connection(self) -> None:
        """请求线程结束时释放它的连接，避免常驻服务累积句柄。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        with self._conns_lock:
            if conn not in self._conns:
                return
            self._conns.remove(conn)
        conn.close()

    def close(self) -> None:
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                # 另一个线程正在用这条连接时 close 会报错；关闭是收尾动作，
                # 报错没有调用方能处理，忽略即可。
                pass
        self._local.conn = None

    # 上下文管理不是为了好看：Windows 上还开着的库文件删不掉、改名改不掉，
    # 而"异常路径忘了 close"是最常见的写法。`with Engine(...) as e:`
    # 让关闭走 __exit__，异常也关。
    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            # Before the first 0.7 schema migration, keep an SQLite-consistent
            # rollback copy. This backup is private and is NOT erased by forget.
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "traces" in tables and "memory_maintenance" not in tables:
                import os
                backup_path = self.db_path + ".pre-0.7.0.bak"
                try:
                    fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
                    backup = sqlite3.connect(backup_path)
                    try:
                        conn.backup(backup)
                    finally:
                        backup.close()
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            self._migrate(conn)
        finally:
            conn.close()

    # 增量列：CREATE TABLE IF NOT EXISTS 不会给已有库加列，
    # 而"用旧库跑新代码"是最常见的升级路径。每加一列就在这里登记一次。
    _ADDED_COLUMNS = (
        # (表, 列, 定义)
        ("interpretations", "semantic_op", "TEXT"),
        ("commitments", "unit", "TEXT"),
        # 事件来自哪一类句式（准入/熔断用）。老库补列后为 NULL，
        # 熔断只对 pattern 非空的事件生效——不让历史数据参与判定。
        ("events", "pattern", "TEXT"),
        # 澄清只念一次（v0.50.0）。老库补列后为 NULL，
        # 那批历史请求会被当成"还没问过"——最多多说一次，不会丢东西。
        ("clarifications", "asked_at", "TEXT"),
        # v0.52：派生标签记住自己对应哪个槽位（偏好对象 / 称呼），
        # 用户改口时才能精确把旧值标成 corrected，不用整表扫描。
        ("person_tags", "slot", "TEXT"),
        # v0.52：澄清是在哪个会话里念过的。记忆跨会话共享之后，
        # 「一个会话最多问几次」必须按会话算，否则问满 6 次就终身不再问。
        ("clarifications", "asked_session", "TEXT"),
    )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        for table, column, decl in self._ADDED_COLUMNS:
            # 这里的连接没有 row_factory，PRAGMA 返回的是元组，name 在第 1 列
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    @contextmanager
    def _tx(self, retries: int = 1) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE：写事务立即取锁，避免升级锁时的死锁。

        **可重入**：已经在事务里时用 SAVEPOINT。此前嵌套会抛
        `cannot start a transaction within a transaction`——那是一颗地雷，
        因为"某个内部方法顺手开了个事务"是很自然的重构，一改就炸，
        而且错误信息指向 SQL 层，看不出是设计约束。

        **锁超时转领域异常**：`busy_timeout` 用完时 SQLite 抛裸的
        `OperationalError: database is locked`。调用方拿不到任何信息，
        只能当致命错误处理——而它其实重试就可能成功。

        重试是安全的：`commit` 本身幂等（`idempotency_key`），
        重跑一次事务不会产生第二次副作用。
        """
        conn = self._conn()
        depth = getattr(self._local, "tx_depth", 0)
        savepoint = f"sp_{depth}" if depth else None

        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if depth:
                    conn.execute(f"SAVEPOINT {savepoint}")
                else:
                    conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == retries:
                    raise LockTimeout(str(exc)) from exc
                last = exc
                time.sleep(0.05 * (2 ** attempt))
        else:                                        # pragma: no cover
            raise LockTimeout(str(last))

        self._local.tx_depth = depth + 1
        try:
            yield conn
        except BaseException:
            if depth:
                conn.execute(f"ROLLBACK TO {savepoint}")
                conn.execute(f"RELEASE {savepoint}")
            else:
                conn.execute("ROLLBACK")
            raise
        else:
            if depth:
                conn.execute(f"RELEASE {savepoint}")
            else:
                conn.execute("COMMIT")
        finally:
            self._local.tx_depth = depth

    # ======================================================== 作用域
    def ensure_scope(self, tenant_id: str, agent_id: str, world_id: str) -> str:
        scope_id = "sc_" + digest(tenant_id, agent_id, world_id)[:24]
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO scopes(scope_id, tenant_id, agent_id, world_id, created_at)"
                " VALUES (?,?,?,?,?)",
                (scope_id, tenant_id, agent_id, world_id, self.now()),
            )
        return scope_id

    def get_scope(self, scope_id: str) -> dict[str, str]:
        row = self._conn().execute(
            "SELECT * FROM scopes WHERE scope_id = ?", (scope_id,)
        ).fetchone()
        if row is None:
            raise ScopeNotFound(scope_id)
        return dict(row)

    # ======================================================== 痕迹
    def record_trace(
        self,
        scope_id: str,
        source_ref: str,
        actor_id: str,
        source_kind: str,
        content: str,
        source_time: str | None = None,
        source_time_precision: str | None = None,
        context_ref: str | None = None,
    ) -> str:
        """写入一条不可变痕迹。(scope_id, source_ref) 唯一：重试返回同一 trace_id。"""
        self.get_scope(scope_id)
        trace_id = "tr_" + digest(scope_id, source_ref)[:24]
        with self._tx() as c:
            existing = c.execute(
                "SELECT trace_id, content_hash FROM traces WHERE scope_id=? AND source_ref=?",
                (scope_id, source_ref),
            ).fetchone()
            if existing:
                if existing["content_hash"] != digest("content", content):
                    raise IdempotencyConflict(source_ref, existing["trace_id"])
                return existing["trace_id"]

            c.execute(
                "INSERT INTO traces(trace_id, scope_id, source_ref, actor_id, source_kind,"
                " content, content_hash, recorded_at, source_time, source_time_precision, context_ref)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trace_id, scope_id, source_ref, actor_id, source_kind,
                    content, digest("content", content), self.now(),
                    source_time, source_time_precision, context_ref,
                ),
            )
            c.execute(
                "INSERT INTO traces_fts(trace_id, content) VALUES (?,?)",
                (trace_id, content),
            )
        return trace_id

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM traces WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        return dict(row) if row else None

    def erase_trace(self, trace_id: str) -> dict[str, Any]:
        """显式删除：连同依赖它的事件一起失效，然后重算受影响状态。

        流程（12.4）：
            1. 找出引用该痕迹的事件（读，事务外）
            2. 事务内：事件置 void、状态重算、索引与摘录清除、原文清空
            3. 事务外：沿依赖图传播派生状态

        **不追加反向事件**：删除不是"撤销"，是"依据没了"。
        依据没了的状态必须转 unknown，不能靠一条补偿事件把余额算回去——
        那等于伪造替代证据（12.5）。

        XM-07：摘要、快照、导出副本的清理仍属部署层，见 ISSUES。
        """
        affected = self._state_keys_depending_on(trace_id)

        with self._tx() as c:
            rows = c.execute(
                "SELECT DISTINCT event_id FROM event_evidence WHERE trace_id=?",
                (trace_id,),
            ).fetchall()
            for row in rows:
                c.execute(
                    "UPDATE events SET void=1, superseded_by='ERASED' WHERE event_id=?",
                    (row["event_id"],),
                )
            for key in affected:
                spec = self._spec_or_none(key)
                if spec is not None:
                    self._recompute(c, key, spec)

            c.execute("DELETE FROM traces_fts WHERE trace_id=?", (trace_id,))
            c.execute(
                "UPDATE traces SET content='', context_ref=NULL, erased_at=? WHERE trace_id=?",
                (self.now(), trace_id),
            )
            c.execute(
                "UPDATE observations SET excerpt='', status='archived' WHERE trace_id=?",
                (trace_id,),
            )
            from .privacy import purge_derived
            purge_derived(self, c, trace_id, affected)

        # 依赖传播要另开事务（recompute_derived 自己管事务）
        for key in affected:
            self.propagate(key)

        return {
            "trace_id": trace_id,
            "state_keys": sorted(affected),
            "voided_events": len(rows),
        }

    def _state_keys_depending_on(self, trace_id: str) -> list[str]:
        rows = self._conn().execute(
            "SELECT DISTINCT e.state_key FROM event_evidence ev"
            " JOIN events e ON e.event_id = ev.event_id WHERE ev.trace_id=?",
            (trace_id,),
        ).fetchall()
        return [r["state_key"] for r in rows]

    def _spec_or_none(self, state_key: str) -> RelationSpec | None:
        try:
            return self.registry.get(relation_of(state_key))
        except SpecNotFound:
            return None

    # ======================================================== 提交
    def commit(self, a: Assertion) -> CommitResult:
        spec = self._resolve_spec(a)
        self._validate_assertion(a, spec)
        with self._tx() as c:
            return self._commit_in_tx(c, a, spec)

    def commit_batch(self, assertions: Sequence[Assertion]) -> list[CommitResult]:
        """原子事件组：要么全部提交，要么全部回滚（设计文档 9.5）。

        组内各事件共享 operation_group_id；调用方应保证它们属于同一业务操作。
        """
        if not assertions:
            return []
        results: list[CommitResult] = []
        with self._tx() as c:
            for a in assertions:
                spec = self._resolve_spec(a)
                # 组内前序事件会改变版本，因此组内不强制 expected_version
                a.expected_version = None
                self._validate_assertion(a, spec)
                results.append(self._commit_in_tx(c, a, spec))
        return results

    def _commit_in_tx(
        self, c: sqlite3.Connection, a: Assertion, spec: RelationSpec
    ) -> CommitResult:
        if c.execute("SELECT 1 FROM scopes WHERE scope_id=?", (a.scope_id,)).fetchone() is None:
            raise ScopeNotFound(a.scope_id)

        payload_hash = digest(canonical_json(a.payload))
        prior = c.execute(
            "SELECT * FROM events WHERE scope_id=? AND idempotency_key=?",
            (a.scope_id, a.idempotency_key),
        ).fetchone()

        # ---- 幂等：同键同载荷返回原结果；同键异载荷拒绝
        if prior is not None:
            if prior["payload_hash"] != payload_hash:
                raise IdempotencyConflict(a.idempotency_key, prior["event_id"])
            return CommitResult(
                event_id=prior["event_id"], state_key=prior["state_key"],
                sequence_no=prior["sequence_no"], applied=False,
                state=self._read_state(c, prior["state_key"]),
            )

        # ---- 证据必须可解析（I1）
        self._assert_evidence_usable(c, a.evidence_trace_ids)

        # ---- 乐观锁：version 为有效事件数，重算后稳定
        current = self._read_state(c, a.state_key)
        actual_version = current.version if current else 0
        if a.expected_version is not None and a.expected_version != actual_version:
            raise VersionConflict(a.state_key, a.expected_version, actual_version)

        # ---- 写入事件
        seq = c.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM events WHERE state_key=?",
            (a.state_key,),
        ).fetchone()[0]
        recorded_at = a.recorded_at or self.now()
        effective_at = a.effective_at or recorded_at
        event_id = "ev_" + digest(a.scope_id, a.state_key, a.idempotency_key)[:24]

        c.execute(
            "INSERT INTO events(event_id, scope_id, state_key, idempotency_key, payload_hash,"
            " operation_group_id, operation, effect_kind, payload_json, gate_decision_id,"
            " actor_id, effective_at, recorded_at, sequence_no, spec_version, target_ref,"
            " pattern, void)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                event_id, a.scope_id, a.state_key, a.idempotency_key, payload_hash,
                a.operation_group_id, a.operation, a.effect_kind,
                canonical_json(a.payload), a.gate_decision_id,
                a.actor_id, effective_at, recorded_at,
                seq, spec.spec_version, a.target_ref, a.pattern or None,
            ),
        )
        for trace_id in a.evidence_trace_ids:
            c.execute(
                "INSERT INTO event_evidence(event_id, trace_id) VALUES (?,?)",
                (event_id, trace_id),
            )

        # ---- 投影：全量重算该状态键（设计文档 9.6 的简单基点）
        state = self._recompute(c, a.state_key, spec)
        return CommitResult(
            event_id=event_id, state_key=a.state_key,
            sequence_no=seq, applied=True, state=state,
        )

    # -------------------------------------------------------- 提交辅助
    def _resolve_spec(self, a: Assertion) -> RelationSpec:
        spec = self.registry.get(a.relation)
        if a.spec_version and a.spec_version != spec.spec_version:
            raise SpecNotFound(
                f"assertion pins {a.spec_version!r} but registry has {spec.spec_version!r}"
            )
        return spec

    def _validate_assertion(self, a: Assertion, spec: RelationSpec) -> None:
        if not a.evidence_trace_ids:
            raise EvidenceMissing(f"{a.state_key}: no evidence_trace_ids")
        if a.effect_kind not in ("set", "delta", "invalidate", "none"):
            raise ValueError(f"bad effect_kind {a.effect_kind!r}")
        if a.effect_kind == "delta" and "delta" not in a.payload:
            raise ValueError("delta effect requires payload['delta']")
        if a.effect_kind == "none":
            # 无效果事件只有一种合法形状：承诺。写成别的说明上游搞错了，
            # 而"不知道是什么的无效果事件"会让状态机永远算不清。
            if a.payload.get("kind") != "promise":
                raise ValueError("effect_kind='none' requires payload['kind']='promise'")
            if not isinstance(a.payload.get("quantity"), (int, float)):
                raise ValueError("promise requires numeric payload['quantity']")
        if a.effect_kind == "set" and "value" not in a.payload:
            raise ValueError("set effect requires payload['value']")

    def _assert_evidence_usable(
        self, c: sqlite3.Connection, trace_ids: Sequence[str]
    ) -> None:
        for tid in trace_ids:
            row = c.execute(
                "SELECT erased_at FROM traces WHERE trace_id=?", (tid,)
            ).fetchone()
            if row is None:
                raise TraceNotFound(tid)
            if row["erased_at"]:
                raise TraceErased(tid)

    # ======================================================== 纠正 / 撤销
    def correct(
        self,
        target_event_id: str,
        new_payload: dict[str, Any],
        idempotency_key: str,
        evidence_trace_ids: tuple[str, ...],
        actor_id: str,
        effective_at: str | None = None,
    ) -> CommitResult:
        """纠正一条事件：新增替代事件，旧事件置为 void，然后重算。

        旧事件保留在流中（审计可查），但不参与求值。

        XM-01: 不检查「修正后是否与更早的偿付矛盾」。这条要引入「累计量」概念，
               当前只有余额，判不出来。调用方需自行校验后决定是否调用。
        """
        # 读目标和写替代必须在**同一个事务**里。
        # 早先是事务外读、事务内写：两个线程可以同时看到「目标未失效」，
        # 各自写一条替代事件，`superseded_by` 被后一个覆盖，
        # 前一条替代事件就成了无主孤儿——账是对的，链是断的。
        with self._tx() as c:
            target = self._get_event_in(c, target_event_id)
            if target is None:
                raise TraceNotFound(f"event {target_event_id} not found")
            if target["void"]:
                raise EventAlreadyVoid(target_event_id)

            a = Assertion(
                scope_id=target["scope_id"],
                state_key=target["state_key"],
                relation=relation_of(target["state_key"]),
                operation="append",
                effect_kind=target["effect_kind"],
                payload=new_payload,
                idempotency_key=idempotency_key,
                evidence_trace_ids=evidence_trace_ids,
                actor_id=actor_id,
                effective_at=effective_at or target["effective_at"],
                target_ref=target_event_id,
            )
            spec = self._resolve_spec(a)
            self._validate_assertion(a, spec)
            self._assert_evidence_usable(c, a.evidence_trace_ids)

            result = self._commit_in_tx(c, a, spec)
            if result.applied:
                # `AND void=0` 是第二道闸：即使前面所有检查都过了，
                # 条件更新也能保证「只有一个人能替代它」。
                cur = c.execute(
                    "UPDATE events SET void=1, superseded_by=? WHERE event_id=? AND void=0",
                    (result.event_id, target_event_id),
                )
                if cur.rowcount != 1:
                    raise EventAlreadyVoid(target_event_id)
                result.state = self._recompute(c, a.state_key, spec)
            return result

    def retract_event(
        self,
        target_event_id: str,
        idempotency_key: str,
        evidence_trace_ids: tuple[str, ...],
        actor_id: str,
    ) -> StateView:
        """撤销一条事件：写入 tombstone，旧事件置为 void，然后重算。

        不追加反向 delta——那会在存在后续依赖时算错（设计文档 9.3）。
        """
        # 同 correct：读目标和写 tombstone 必须在同一个事务里。
        with self._tx() as c:
            target = self._get_event_in(c, target_event_id)
            if target is None:
                raise TraceNotFound(f"event {target_event_id} not found")
            if target["void"]:
                raise EventAlreadyVoid(target_event_id)

            spec = self.registry.get(relation_of(target["state_key"]))
            self._assert_evidence_usable(c, evidence_trace_ids)
            seq = c.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM events WHERE state_key=?",
                (target["state_key"],),
            ).fetchone()[0]
            tomb = "ev_" + digest(target["scope_id"], target["state_key"], idempotency_key)[:24]
            now = self.now()
            c.execute(
                "INSERT INTO events(event_id, scope_id, state_key, idempotency_key, payload_hash,"
                " operation, effect_kind, payload_json, actor_id, effective_at, recorded_at,"
                " sequence_no, spec_version, target_ref, void)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (
                    tomb, target["scope_id"], target["state_key"], idempotency_key,
                    digest(canonical_json({"retracted_event": target_event_id})),
                    "retract", "invalidate",
                    canonical_json({"retracted_event": target_event_id}),
                    actor_id, target["effective_at"], now,
                    seq, spec.spec_version, target_event_id,
                ),
            )
            for tid in evidence_trace_ids:
                c.execute(
                    "INSERT INTO event_evidence(event_id, trace_id) VALUES (?,?)", (tomb, tid)
                )
            cur = c.execute(
                "UPDATE events SET void=1, superseded_by=? WHERE event_id=? AND void=0",
                (tomb, target_event_id),
            )
            if cur.rowcount != 1:
                raise EventAlreadyVoid(target_event_id)
            return self._recompute(c, target["state_key"], spec)

    def _get_event_in(self, c: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
        """在指定事务里读事件。读-判-写必须共用同一个连接，否则又是 TOCTOU。"""
        row = c.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def _get_event(self, event_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    # ======================================================== 求值
    def _fold(
        self,
        c: sqlite3.Connection,
        state_key: str,
        spec: RelationSpec,
        as_of: str | None = None,
        knowledge_as_of: str | None = None,
    ) -> StateView:
        """按 (effective_at, sequence_no) 重放一条状态流。

        两种时间：
            as_of            业务时间上限（effective_at <= as_of）
            knowledge_as_of  知识时间上限（recorded_at <= as_of）
            两者组合即"当时系统认为的状态"。
        """
        # 排序键：`(生效时间, 它取代的那条事件的序号, 自己的序号)`。
        #
        # 第三个键不是多余的。修正事件的排序如果只看自己的序号，
        # 它会永远排在**最后**——于是「改初始值」被排到两笔还款之后，
        # set 又把它们抹掉了，余额从 20 变成 50，而事件流看起来完全正常。
        #
        # 时间戳精确到微秒时这个问题碰不到（修正事件带着原事件的
        # effective_at，会比后面的还款更早）。但时钟一旦变粗
        # （固定时钟、批量导入、秒级时间戳），三条事件时间相同，
        # 就只能靠序号——这时"取代"语义必须显式写出来。
        sql = (
            "SELECT e.*, COALESCE(t.sequence_no, e.sequence_no) AS sort_seq"
            " FROM events e"
            " LEFT JOIN events t ON t.event_id = e.target_ref AND e.target_ref IS NOT NULL"
            " WHERE e.state_key=? AND e.void=0"
        )
        params: list[Any] = [state_key]
        if as_of:
            sql += " AND e.effective_at <= ?"
            params.append(as_of)
        if knowledge_as_of:
            sql += " AND e.recorded_at <= ?"
            params.append(knowledge_as_of)
        sql += " ORDER BY e.effective_at, sort_seq, e.sequence_no"
        rows = c.execute(sql, params).fetchall()

        # watermark 用全部事件（含 void）的最大序号，表示"投影到了这里"
        wm_row = c.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) FROM events WHERE state_key=?", (state_key,)
        ).fetchone()
        watermark = wm_row[0]

        state: StateView | None = None
        totals: dict = {}
        count = 0
        # 这本账用哪个单位记的。单位不是必填，也不是规格里定死的
        # （米按袋、按石、按斗都有人用），但**一本账只能有一个口径**。
        unit: str | None = None
        for row in rows:
            payload = json.loads(row["payload_json"])
            fake = Assertion(
                scope_id=row["scope_id"], state_key=state_key,
                relation=spec.relation, operation=row["operation"],
                effect_kind=row["effect_kind"],
                payload=payload,
                idempotency_key=row["idempotency_key"],
                evidence_trace_ids=(), actor_id=row["actor_id"],
            )
            change = apply_event(state, fake, spec, totals)
            count += 1

            # 单位变了：两笔账不是同一个计量口径，不能直接加减。
            #
            # 实测：「欠两石米」+「再欠三斗」−「还五斗」算出来是 0，状态 known——
            # 2+3−5。而真实答案是 2 石 − 2 斗 = 18 斗。**账面看不出任何异常。**
            #
            # 内核不做换算：石等于十斗是领域知识，写死在核心里就是把某地的度量
            # 习惯当成普适真理（各朝各代一石并不一样）。但内核也不能装作没看见。
            # 所以：**判 disputed、值停在上一次**，等有人来定口径。
            this_unit = payload.get("unit")

            # 一旦因为单位卡住，后面的事件都别再改写状态——
            # 否则「还你五斗」会把它盖成 non_negative_violated，
            # 真正的原因（单位不同）就丢了。**账卡住的原因要稳定。**
            if (state is not None and state.status == "disputed"
                    and (state.conflict_reason or "").startswith("unit_mismatch")):
                change = StateChange(
                    status="disputed", value=state.value, last_known=state.value,
                    conflict_reason=state.conflict_reason,
                )
            elif this_unit and unit and this_unit != unit and change.status in (
                "known", "stale"
            ):
                keep = state.value if state is not None else None
                change = StateChange(
                    status="disputed",
                    value=keep,
                    last_known=keep,
                    conflict_reason=f"unit_mismatch:{unit}!={this_unit}",
                )
            elif this_unit and not unit:
                unit = this_unit

            state = StateView(
                state_key=state_key, status=change.status, value=change.value,
                last_known=change.last_known, version=count, watermark=watermark,
                conflict_reason=change.conflict_reason,
                spec_version=row["spec_version"],
            )

        if state is None:
            return StateView(
                state_key=state_key, status="unknown", value=None, last_known=None,
                version=0, watermark=watermark, conflict_reason=None,
                spec_version=spec.spec_version,
            )
        return state

    def _recompute(self, c: sqlite3.Connection, state_key: str, spec: RelationSpec) -> StateView:
        # XM-04: 全量重放，不是文档 9.4 的「受影响后缀 + 依赖闭包」。
        #        改成后缀时 states.watermark 的语义会从「最大序号」变成「快照点」，
        #        依赖它做增量同步的调用方会静默出错。
        state = self._fold(c, state_key, spec)
        state = self._flag_delta_before_first_set(c, state_key, state)
        self._write_state(c, state, spec.spec_version)
        # 承诺投影挂在**唯一的重算入口**上，而不是各条写路径各调一次。
        # 之前只在 append 里调，结果 retract / correct 走的是另一条路——
        # 撤销一笔还款之后，承诺还写着 kept（测试当场抓到）。
        self._refresh_commitments(c, state_key)
        return state

    # -------------------------------------------------------- 承诺投影
    def _refresh_commitments(self, c: sqlite3.Connection, state_key: str) -> None:
        """从事件流重算某个状态键下的承诺。**全量，不做增量。**

        规则只有两条，但必须按时间顺序一起跑，否则会错：

            承诺事件   → 开一条 open，remaining = quantity
            还款事件   → 从最早的 open 开始扣，扣完标 kept

        为什么是"最早优先"（FIFO）：两笔承诺「下月还两袋」「月底还一袋」，
        今天还了一袋——没有任何信息能区分他先还的是哪一笔，
        而 FIFO 至少是**可解释**的：先答应的先兑现。

        撤销会自动复员：void=1 的事件直接不参与重算，
        所以撤回一次还款之后，被它结清的承诺会自己回到 open。
        这是选"全量重算"而不是"增量维护"换来的。
        """
        rows = c.execute(
            "SELECT event_id, scope_id, operation, effect_kind, payload_json, actor_id,"
            " effective_at, recorded_at, sequence_no, void, target_ref"
            " FROM events WHERE state_key=? AND void=0"
            " ORDER BY effective_at, sequence_no",
            (state_key,),
        ).fetchall()

        c.execute("DELETE FROM commitments WHERE state_key=?", (state_key,))
        open_items: list[dict[str, Any]] = []
        now = self.now()

        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            ek = row["effect_kind"]
            if ek == "none" and payload.get("kind") == "promise":
                qty = payload.get("quantity")
                if not isinstance(qty, (int, float)):
                    continue
                open_items.append({
                    "commitment_id": "cm_" + row["event_id"][3:],
                    "scope_id": row["scope_id"],
                    "state_key": state_key,
                    "event_id": row["event_id"],
                    "actor_id": row["actor_id"],
                    "quantity": float(qty),
                    "remaining": float(qty),
                    "unit": payload.get("unit"),
                    "due_hint": payload.get("due_hint"),
                    "due_at": payload.get("due_at"),
                    "status": "open",
                    "settled_by": None,
                    "created_at": row["recorded_at"],
                    "updated_at": now,
                })
                continue

            if ek != "delta":
                continue
            d = payload.get("delta")
            if not isinstance(d, (int, float)) or d >= 0:
                continue
            left = float(-d)
            for item in open_items:
                if left <= 0:
                    break
                if item["remaining"] <= 0:
                    continue
                take = min(item["remaining"], left)
                item["remaining"] -= take
                left -= take
                if item["remaining"] <= 0:
                    item["status"] = "kept"
                    item["settled_by"] = row["event_id"]

        for item in open_items:
            if item["status"] == "open" and item["remaining"] < item["quantity"]:
                item["status"] = "partial"
            c.execute(
                "INSERT INTO commitments(commitment_id, scope_id, state_key, event_id,"
                " actor_id, quantity, remaining, unit, due_hint, due_at, status,"
                " settled_by, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item["commitment_id"], item["scope_id"], item["state_key"],
                    item["event_id"], item["actor_id"], item["quantity"],
                    item["remaining"], item["unit"], item["due_hint"], item["due_at"],
                    item["status"], item["settled_by"], item["created_at"],
                    item["updated_at"],
                ),
            )

    def commitments(
        self, scope_id: str, state_key: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """列出承诺。默认全给，调用方自己筛。"""
        sql = "SELECT * FROM commitments WHERE scope_id=?"
        params: list[Any] = [scope_id]
        if state_key:
            sql += " AND state_key=?"
            params.append(state_key)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at, commitment_id"
        return [dict(r) for r in self._conn().execute(sql, params).fetchall()]

    def overdue(
        self, scope_id: str, state_key: str | None = None, now: str | None = None
    ) -> list[dict[str, Any]]:
        """到期没还的承诺，最早的在前。

        `due_at` 为 NULL 的（"回头""发工资"）永远不进这个列表——
        没有约定日子的承诺不该被催。这不是遗漏，是 XM-24 的边界。
        """
        at = now or self.now()
        sql = (
            "SELECT * FROM commitments WHERE scope_id=?"
            " AND status IN ('open','partial') AND due_at IS NOT NULL AND due_at <= ?"
        )
        params: list[Any] = [scope_id, at]
        if state_key:
            sql += " AND state_key=?"
            params.append(state_key)
        sql += " ORDER BY due_at, created_at"
        return [dict(r) for r in self._conn().execute(sql, params).fetchall()]

    def rebuild_commitments(self, scope_id: str) -> int:
        """从事件流重建承诺投影。返回处理的状态键数。

        存在的意义不是"修数据"，是**证明这张表可以随时扔掉**。
        只要它和事件流不一致，就是这个投影写坏了，而不是数据坏了。
        """
        keys = [
            r[0] for r in self._conn().execute(
                "SELECT DISTINCT state_key FROM events WHERE scope_id=?", (scope_id,)
            ).fetchall()
        ]
        with self._tx() as c:
            for key in keys:
                self._refresh_commitments(c, key)
        return len(keys)

    def _flag_delta_before_first_set(
        self, c: sqlite3.Connection, state_key: str, state: StateView
    ) -> StateView:
        """首个 set 之前的 delta：没有基数，永远不可能成立。

        它会被后来的 set 覆盖，最终状态看起来"正常"——但那条记录
        其实从未生效过。不标记的话，审计里完全看不出它是个错误。

        XM-09：这个检查放在重算的**最后**，因为要等整条流都算完才知道
        谁是"首个 set"。放在求值过程中只能看到局部，会把
        "未来会有 set"的合法情况误判成错误。
        """
        row = c.execute(
            "SELECT MIN(effective_at) AS t FROM events"
            " WHERE state_key=? AND effect_kind='set' AND void=0",
            (state_key,),
        ).fetchone()
        if row is None or row["t"] is None:
            # 整条流没有 set：delta 是「从 0 开始」，合法（+5 → 5）。
            # 减量仍然会被非负约束拦住，不需要这里再判。
            return state

        n = c.execute(
            "SELECT COUNT(*) AS n FROM events WHERE state_key=? AND effect_kind='delta'"
            " AND void=0 AND effective_at < ?",
            (state_key, row["t"]),
        ).fetchone()["n"]
        if not n:
            return state

        # 已经是 disputed 的保留原原因（那个更具体）。
        if state.status in ("known", "stale"):
            return replace(state, status="disputed",
                           conflict_reason="delta_before_first_set")
        return state

    def _read_state(self, c: sqlite3.Connection, state_key: str) -> StateView:
        row = c.execute("SELECT * FROM states WHERE state_key=?", (state_key,)).fetchone()
        if row is None:
            return StateView(
                state_key=state_key, status="unknown", value=None, last_known=None,
                version=0, watermark=0, conflict_reason=None, spec_version="",
            )
        return StateView(
            state_key=row["state_key"], status=row["status"],
            value=json.loads(row["value_json"]) if row["value_json"] else None,
            last_known=json.loads(row["last_known_json"]) if row["last_known_json"] else None,
            version=row["version"], watermark=row["watermark"],
            conflict_reason=row["conflict_reason"], spec_version=row["spec_version"],
        )

    def _write_state(self, c: sqlite3.Connection, s: StateView, spec_version: str) -> None:
        c.execute(
            "INSERT INTO states(state_key, scope_id, value_json, status, last_known_json,"
            " conflict_reason, version, watermark, evaluated_as_of, spec_version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(state_key) DO UPDATE SET"
            "   value_json=excluded.value_json,"
            "   status=excluded.status,"
            "   last_known_json=excluded.last_known_json,"
            "   conflict_reason=excluded.conflict_reason,"
            "   version=excluded.version,"
            "   watermark=excluded.watermark,"
            "   evaluated_as_of=excluded.evaluated_as_of,"
            "   spec_version=excluded.spec_version",
            (
                s.state_key, s.state_key.split("|")[0],
                canonical_json(s.value) if s.value is not None else None,
                s.status,
                canonical_json(s.last_known) if s.last_known is not None else None,
                s.conflict_reason, s.version, s.watermark, self.now(), spec_version,
            ),
        )

    # ======================================================== 读取
    # XM-06: 只有同步投影。加异步后，这里要能区分「读投影」与「等投影」
    #        （read-your-writes），属于接口变更。
    def get_state(
        self,
        state_key: str,
        as_of: str | None = None,
        knowledge_as_of: str | None = None,
    ) -> StateView:
        """无参数读当前投影；带时间参数则按事件流重放（不写回）。"""
        if as_of is None and knowledge_as_of is None:
            return self._read_state(self._conn(), state_key)

        spec = self.registry.get(relation_of(state_key))
        return self._fold(self._conn(), state_key, spec, as_of, knowledge_as_of)

    def events_of(self, state_key: str, include_void: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE state_key=?"
        if not include_void:
            sql += " AND void=0"
        sql += " ORDER BY effective_at, sequence_no"
        rows = self._conn().execute(sql, (state_key,)).fetchall()
        return [dict(r) for r in rows]

    def evidence_of(self, event_id: str) -> list[str]:
        rows = self._conn().execute(
            "SELECT trace_id FROM event_evidence WHERE event_id=? ORDER BY rowid", (event_id,)
        ).fetchall()
        return [r["trace_id"] for r in rows]

    def explain(self, state_key: str) -> dict[str, Any]:
        """状态值 + 事件链 + 证据片段。用于回答"为什么是这个值"。"""
        chain = []
        for ev in self.events_of(state_key):
            chain.append({
                "event_id": ev["event_id"],
                "sequence_no": ev["sequence_no"],
                "operation": ev["operation"],
                "effect_kind": ev["effect_kind"],
                "payload": json.loads(ev["payload_json"]),
                "effective_at": ev["effective_at"],
                "recorded_at": ev["recorded_at"],
                "void": bool(ev["void"]),
                "superseded_by": ev["superseded_by"],
                "evidence": [
                    {"trace_id": tid, "content": (self.get_trace(tid) or {}).get("content", "")}
                    for tid in self.evidence_of(ev["event_id"])
                ],
            })
        return {"state": self.get_state(state_key), "chain": chain}

    def replay(
        self,
        state_key: str,
        as_of: str | None = None,
        knowledge_as_of: str | None = None,
    ) -> StateView:
        """从事件流重建状态。用于 T04 与重算一致性校验。"""
        spec = self.registry.get(relation_of(state_key))
        return self._fold(self._conn(), state_key, spec, as_of, knowledge_as_of)

    # ======================================================== 依赖图
    def register_derivation(
        self,
        derived_key: str,
        input_keys: Sequence[str],
        rule_version: str,
        fn: Callable[[list[StateView]], tuple[str, Any, str | None]],
    ) -> None:
        """登记派生状态。

        fn 接收输入状态列表，返回 (status, value, conflict_reason)。
        派生状态不保存独立证据，其依据即输入状态的依赖链（设计文档 6.9）。
        """
        if derived_key in self._derivations:
            raise ValueError(f"derivation already registered: {derived_key}")
        # 环检测：derived_key 不能出现在自己的输入闭包里
        if derived_key in input_keys:
            raise DependencyCycle(f"{derived_key} depends on itself")
        # XM-05: fn 存在内存里，进程重启后必须重新注册。
        #        state_deps 只存了依赖关系，不存实现，别指望从库里恢复。
        self._derivations[derived_key] = (tuple(input_keys), rule_version, fn)
        with self._tx() as c:
            for k in input_keys:
                c.execute(
                    "INSERT OR REPLACE INTO state_deps(derived_key, input_key, rule_version)"
                    " VALUES (?,?,?)",
                    (derived_key, k, rule_version),
                )

    def recompute_derived(self, derived_key: str) -> StateView:
        if derived_key not in self._derivations:
            raise KeyError(f"no derivation registered for {derived_key}")
        inputs, rule_version, fn = self._derivations[derived_key]
        states = [self.get_state(k) for k in inputs]
        status, value, reason = fn(states)
        view = StateView(
            state_key=derived_key, status=status, value=value, last_known=None,
            version=0, watermark=0, conflict_reason=reason,
            spec_version=f"derived:{rule_version}",
        )
        with self._tx() as c:
            self._write_state(c, view, f"derived:{rule_version}")
        return view

    def propagate(self, changed_key: str) -> list[str]:
        """从被改动的键出发，按拓扑序重算其下游派生状态。"""
        order = self._dependent_order(changed_key)
        for key in order:
            self.recompute_derived(key)
        return order

    def _dependent_order(self, root: str) -> list[str]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT derived_key, input_key FROM state_deps"
        ).fetchall()
        graph: dict[str, list[str]] = {}
        for r in rows:
            graph.setdefault(r["input_key"], []).append(r["derived_key"])

        seen: list[str] = []
        seen_set: set[str] = set()

        def walk(node: str) -> None:
            for child in graph.get(node, ()):
                if child in seen_set:
                    continue
                walk(child)          # 先递归下游，保证拓扑序
                seen_set.add(child)
                seen.append(child)

        walk(root)
        seen.reverse()
        return seen

    # ======================================================== T01 检查
    def check_source_closure(self, state_key: str) -> list[str]:
        """来源闭合检查。返回问题列表，空表示通过。"""
        problems: list[str] = []
        for ev in self.events_of(state_key):
            ev_id = ev["event_id"]
            traces = self.evidence_of(ev_id)
            if not traces:
                problems.append(f"{ev_id}: no evidence")
                continue
            for tid in traces:
                t = self.get_trace(tid)
                if t is None:
                    problems.append(f"{ev_id}: evidence {tid} missing")
                elif t["erased_at"]:
                    problems.append(f"{ev_id}: evidence {tid} erased")
                elif t["scope_id"] != ev["scope_id"]:
                    problems.append(f"{ev_id}: evidence {tid} crosses scope")
        return problems


# ============================================================ 纯函数
@dataclass(frozen=True)
class StateChange:
    status: str
    value: Any = None
    last_known: Any = None
    conflict_reason: str | None = None


def apply_event(
    current: StateView | None,
    a: Assertion,
    spec: RelationSpec,
    totals: dict | None = None,
) -> StateChange:
    """事件 → 新状态。

    对 status/value 是纯函数：不依赖时钟、不访问数据库。
    唯一被就地改写的是 `totals`——它是折叠过程中的累加器，
    用来支持「累计偿付量」这类无法只看余额判断的约束（XM-01）。

    违反业务约束时不截断、不丢弃事件，而是进入 disputed
    （设计文档 7.4 conflict_policy / 8.5 第 7 条 / 14.2-H）。
    """
    prev_value = current.value if current else None

    # 承诺（effect_kind="none"）：事件进流，状态不动。
    #
    # 它不是"没有变化"，而是**变化发生在以后**。事件流里留着它，
    # 才能回答「他答应过什么」「答应了几次」；而 states 里不体现它，
    # 因为余额确实一分没动。把这两件事混在一起，就会出现
    # 「说了句下个月还，余额先少了」——XM-22 就是这么来的。
    if a.effect_kind == "none":
        if current is None:
            return StateChange(status="unknown", value=None, last_known=None)
        return StateChange(
            status=current.status, value=prev_value, last_known=prev_value,
        )

    if a.operation == "retract" or a.effect_kind == "invalidate":
        return StateChange(status="retracted", value=None, last_known=prev_value)

    if a.operation == "conflict":
        return StateChange(
            status="disputed", value=prev_value, last_known=prev_value,
            conflict_reason=a.payload.get("reason", "explicit_conflict"),
        )

    # 累计（只对 delta，且只在该关系声明了守恒量时）
    if totals is not None and a.effect_kind == "delta" and spec.conservation == "repayment":
        d = a.payload.get("delta", 0)
        # 内核必须自己挡类型：编码器给什么都不能让它抛异常。
        # 非数字的 delta 不是"没有变化"，是一个明确的错误，要落成 disputed。
        if not isinstance(d, (int, float)) or isinstance(d, bool):
            return StateChange(
                status="disputed", value=prev_value, last_known=prev_value,
                conflict_reason="non_numeric_delta",
            )
        if d < 0:
            totals["repaid"] = totals.get("repaid", 0.0) + (-d)

    if a.effect_kind == "set":
        raw = a.payload.get("value")
        # XM-01：修正后的初始值不能小于已偿付总额，否则「还得比欠的多」。
        # 这一类矛盾只看余额发现不了——余额可能仍是正的（因为期间又有新借款）。
        if (spec.conservation == "repayment" and totals is not None
                and isinstance(raw, (int, float))
                and totals.get("repaid", 0) > raw):
            return StateChange(
                status="disputed", value=prev_value, last_known=prev_value,
                conflict_reason="violates_accumulated_repayment",
            )
    else:  # delta
        # XM-09（已修）：delta 的 effective_at 早于本流首个 set 时，这里会
        # 在空状态上求值（如 0-5=-5），随后被 set 覆盖。
        # 现在由 `_recompute` 末尾的 `_flag_delta_before_first_set` 兜住：
        # 值照算，但状态降级为 disputed / delta_before_first_set。
        # 不在求值过程中判，因为那时还不知道"未来会不会有 set"。
        d = a.payload.get("delta", 0)
        if not isinstance(d, (int, float)) or isinstance(d, bool):
            return StateChange(
                status="disputed", value=prev_value, last_known=prev_value,
                conflict_reason="non_numeric_delta",
            )
        base = prev_value if isinstance(prev_value, (int, float)) else 0
        raw = base + d

    err = _validate_value(spec, raw)
    if err:
        return StateChange(
            status="disputed", value=prev_value, last_known=prev_value,
            conflict_reason=err,
        )

    return StateChange(status="known", value=raw, last_known=prev_value)


def _validate_value(spec: RelationSpec, value: Any) -> str | None:
    vt = spec.value_type
    if vt in ("integer", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"value_type:{vt}"
        if vt == "integer" and int(value) != value:
            return "value_type:integer"
        if not spec.allow_negative and value < 0:
            return "non_negative_violated"
        return None
    if vt == "string":
        return None if isinstance(value, str) else "value_type:string"
    if vt == "enum":
        return None if value in spec.enum_values else "enum_violated"
    return None
