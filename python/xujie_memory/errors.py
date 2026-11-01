"""内核错误类型。

原则：所有拒绝都必须是一个具体类型，而不是 None 或空值。
调用方必须能区分"没查到"与"查不了"（设计文档 I5）。
"""


class MemoryError(Exception):
    """所有内核错误的基类。"""


class ScopeNotFound(MemoryError):
    pass


class TraceNotFound(MemoryError):
    pass


class TraceErased(MemoryError):
    """痕迹已被显式删除，不得继续作为证据使用。"""


class SpecNotFound(MemoryError):
    pass


class SpecVersionMismatch(MemoryError):
    """事件使用的规格版本与状态流当前版本不一致。"""


class IdempotencyConflict(MemoryError):
    """同一幂等键被用于不同载荷。必须拒绝，不得静默覆盖。"""

    def __init__(self, key: str, existing_event_id: str):
        self.key = key
        self.existing_event_id = existing_event_id
        super().__init__(
            f"idempotency_key={key!r} already used with a different payload "
            f"(existing event {existing_event_id})"
        )


class VersionConflict(MemoryError):
    """乐观并发失败：状态版本已被其他写入推进。"""

    def __init__(self, state_key: str, expected: int, actual: int):
        self.state_key = state_key
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"state_key={state_key!r} expected_version={expected} but actual={actual}"
        )


class EvidenceMissing(MemoryError):
    """提交事件时未提供任何可解析的证据。违反 I1。"""


class DependencyCycle(MemoryError):
    """依赖图出现环。登记时必须拒绝（设计文档 9.5）。"""


class LockTimeout(MemoryError):
    """写锁等待超时。

    SQLite 只有一把写锁，`BEGIN IMMEDIATE` 拿不到时按 `busy_timeout` 等待，
    超时抛的是裸的 `sqlite3.OperationalError: database is locked`。

    这条之所以要单独成类型，是因为它**不是"数据错了"，是"现在不行"**：

    - `IdempotencyConflict` / `VersionConflict` → 改数据才有用
    - `LockTimeout` → 原样重试就可能成功

    把两者混成一个 `sqlite3.Error`，调用方只能全部当致命错误处理，
    于是一次瞬时的锁竞争被报成"记忆系统坏了"。
    """

    retryable = True

    def __init__(self, message: str = "database is locked"):
        super().__init__(message)


class EventAlreadyVoid(MemoryError):
    """要纠正 / 撤销的事件已经失效。

    这不是"冲突"，更不是"程序错了"——它通常意味着**同一个动作被执行了两次**
    （用户点了两下、消息重投、并发重试）。

    以前这里抛裸 `ValueError`，调用方只能 catch 一个泛型异常，
    和"参数写错了"混在一起。而这两件事的处理方式完全不同：
    前者忽略即可，后者是 bug。
    """

    def __init__(self, event_id: str):
        self.event_id = event_id
        super().__init__(f"event {event_id} already void")
