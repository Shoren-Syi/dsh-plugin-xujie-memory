"""Xujie-Memory 内核（阶段 1–3）。

公开入口：
    Engine           提交与投影内核
    Assertion        一条待提交的领域变更
    SpecRegistry     关系规格注册表
    RelationSpec     单条关系规格
    make_state_key   状态键构造

    RuleEncoder      词表 + 正则的弱编码器（跑通流程用）
    ReplayEncoder    从标注样本回放（测试用）
    LLMEncoder       调 OpenAI 兼容接口（可选）

    Ingest           写路径编排：接收 → 编码 → 闸门 → 提交/挂起/只记录
    decide           单条解释的闸门判定

设计文档对应：v0.3 第 2、3、5、6、7、8 节。
"""

from .encoder import (
    Interpretation,
    LLMEncoder,
    ReplayEncoder,
    RuleEncoder,
    parse_number,
)
from .engine import (
    fixed_clock,
    Assertion,
    CommitResult,
    Engine,
    StateChange,
    StateView,
    apply_event,
    canonical_json,
    make_state_key,
    relation_of,
)
from .decay import Lifecycle, priority
from .errors import (
    DependencyCycle,
    EvidenceMissing,
    EventAlreadyVoid,
    IdempotencyConflict,
    LockTimeout,
    MemoryError,
    ScopeNotFound,
    SpecNotFound,
    SpecVersionMismatch,
    TraceErased,
    TraceNotFound,
    VersionConflict,
)
from .gate import (
    COMMIT,
    PENDING,
    RECORD_ONLY,
    REJECT,
    Decision,
    GateContext,
    decide,
)
from .ingest import (
    Ingest,
    IngestOutcome,
    IngestResult,
    ReplyResult,
    classify_reply,
    resolve,
)
from .audit import AuditReport, Finding, audit
from .consolidate import ConsolidationReport, consolidate
from .phrasing import Persona, acknowledge, answer, ask, declined, describe
from .recall import (
    FOUND,
    NOT_FOUND,
    PARTIAL,
    UNAVAILABLE,
    Budget,
    Recall,
    RecallQuery,
    RecallResult,
)
from .specs import RelationSpec, SpecRegistry
from .console import make_console_safe
from .memory import FuzzySearch, Memory, similarity

__all__ = [
    # 内核
    "Engine", "Assertion", "CommitResult", "StateView", "StateChange",
    "apply_event", "canonical_json", "make_state_key", "relation_of",
    "SpecRegistry", "RelationSpec",
    # 编码
    "Interpretation", "RuleEncoder", "ReplayEncoder", "LLMEncoder", "parse_number",
    # 闸门
    "GateContext", "Decision", "decide", "COMMIT", "PENDING", "RECORD_ONLY", "REJECT",
    # 编排
    "Ingest", "IngestResult", "IngestOutcome", "resolve",
    "ReplyResult", "classify_reply",
    # 话术（措辞与内核分离）
    "Persona", "ask", "describe", "acknowledge", "declined", "answer",
    # 生命周期
    "Lifecycle", "priority",
    "consolidate", "ConsolidationReport",
    # 不变量验收（方案第七节）
    "audit", "AuditReport", "Finding",
    # 时钟（模拟"过了很久"）
    "fixed_clock",
    # 读取
    "Recall", "RecallQuery", "RecallResult", "Budget",
    "FOUND", "PARTIAL", "NOT_FOUND", "UNAVAILABLE",
    # 三层记忆 / 重点 / 模糊搜索
    "Memory", "FuzzySearch", "similarity",
    # 控制台兜底（Windows 的 GBK 终端打不出 −/⚠/█）
    "make_console_safe",
    # 错误
    "MemoryError", "ScopeNotFound", "TraceNotFound", "TraceErased",
    "SpecNotFound", "SpecVersionMismatch", "IdempotencyConflict",
    "VersionConflict", "LockTimeout", "EventAlreadyVoid",
    "EvidenceMissing", "DependencyCycle",
]

__version__ = "0.52.0"
