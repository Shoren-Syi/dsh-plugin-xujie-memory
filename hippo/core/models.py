"""行对象模型。DAO 只返回这些模型，不返回裸 dict / sqlite3.Row。"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from typing import Any, get_origin

from pydantic import BaseModel, ConfigDict


@lru_cache(maxsize=None)
def _row_meta(cls: type) -> tuple[frozenset[str], tuple[str, ...]]:
    """(字段名集合, 需要 json 反序列化的字段)。

    为什么要缓存：`cls.model_fields[name]` 每访问一次都走一遍 pydantic 的
    属性描述符。160 行 × 20 字段 = 3200 次，单次 ~5µs，就是 16ms——
    M10.1 的 kw_search 一开始 8ms 里有 6ms 花在这里，而它只是个 dict 查找。

    这是全局改动，不只服务检索：所有 DAO 读路径都受益。跑全量测试验证过。
    """
    fields = cls.model_fields
    lists = tuple(n for n, f in fields.items() if get_origin(f.annotation) is list)
    return frozenset(fields), lists


class _Row(BaseModel):
    model_config = ConfigDict(frozen=False)

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> "_Row":
        data = dict(row)
        names, lists = _row_meta(cls)
        for name in lists:
            if name in data:
                data[name] = _json_list(data[name])
        return cls(**{k: v for k, v in data.items() if k in names})


def _json_list(value: Any) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [int(x) for x in parsed] if isinstance(parsed, list) else []


class Event(_Row):
    id: int
    session: str
    event_time: int
    event_time_end: int | None = None
    ingest_time: int
    last_access: int | None = None
    time_precision: str = "day"
    time_guessed: int = 0
    tier: int = 2
    weight: float = 0.5
    w0: float = 0.5
    arousal: float = 0.0
    recall_count: int = 0
    keywords: str = ""
    summary: str = ""
    raw: str
    entities: list[int] = []
    source: str = "user"
    kind: str = "event"
    pinned: int = 0
    giveup_level: str = ""          # M22.3：kind=giveup 时记 level（聚合用）
    superseded_by: int | None = None
    archived: int = 0

    @property
    def keyword_list(self) -> list[str]:
        return [t for t in self.keywords.split() if t]


class Entity(_Row):
    id: int
    session: str
    canonical: str
    aliases: list[str] = []
    type: str = "person"
    confidence: float = 0.5
    first_seen: int | None = None
    last_seen: int | None = None
    # 非空 = 已并入别的实体。合并不可逆，所以只留痕不删行：
    # 删掉之后，"这条记忆为什么挂在他身上"就永远查不清了。
    merged_into: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> "Entity":
        data = dict(row)
        raw = data.get("aliases")
        if isinstance(raw, str):
            try:
                data["aliases"] = json.loads(raw) if raw else []
            except ValueError:
                data["aliases"] = []
        return cls(**{k: v for k, v in data.items() if k in cls.model_fields})


class CardTrait(_Row):
    id: int
    entity_id: int
    trait: str
    status: str = "observed"
    evidence: list[int] = []
    confidence: float = 0.5
    valid_from: int | None = None
    valid_to: int | None = None
    updated_at: int | None = None
    # "这条特征在回答哪个问题"。同槽位只留一条 active，
    # 空串 = 不占槽（不确定槽位的特征不许互相覆盖）。
    slot: str = ""

    @property
    def active(self) -> bool:
        return self.valid_to is None


class Fact(_Row):
    id: int
    session: str
    entity_id: int | None = None
    key: str
    value: str
    valid_from: int | None = None
    valid_to: int | None = None
    source_event: int | None = None
    confidence: float = 0.5
    # 009 迁移加的两列，模型一直没跟上。`from_row` 只填 model_fields，
    # 所以这两列在代码里"存在但读不到"——M14 要按 card 区分单值/多值槽才发现。
    # 这类漂移不会有任何报错，只会在有人第一次用到时 AttributeError。
    card: str = "single"      # single = 同实体同槽只能一个当前值；multi = 可并存
    key_raw: str = ""         # 模型写的原始说法（key 已归一）


class Doubt(_Row):
    id: int
    session: str
    question: str
    context: str = ""
    confidence: float = 0.5
    cost: float = 0.5
    impact: float = 0.5
    created: int
    expires: int
    asked_count: int = 0
    last_asked_turn: int | None = None
    resolved: int = 0
    converted: int = 0          # M18.3：已转成 cold 事件（只转一次）


class Plan(_Row):
    id: int
    session: str
    due_at: int
    content: str
    raw_hint: str | None = None
    recur: str | None = None
    fired: int = 0
    fired_at: int | None = None
    missed: int = 0
    attempts: int = 0
    created: int


class Affect(_Row):
    session: str
    joy: float = 60
    sadness: float = 10
    anger: float = 5
    fear: float = 5
    disgust: float = 5
    surprise: float = 20
    updated: int = 0


class LedgerEntry(_Row):
    action_id: str
    session: str
    action: str
    action_kind: str = ""           # M23.1：explore / edit_file / propose / message
    predicted: float = 0
    actual: float | None = None
    rpe: float | None = None
    ts: int
    settled: int = 0                # 0 待结算 / 1 已结算 / -1 过期作废
    settled_at: int | None = None


class Curiosity(_Row):
    id: int
    session: str
    question: str
    gap_kind: str = ""              # doubt / entity / trait / fact
    gap_ref: str = ""
    impact: float = 0
    urgency: float = 0
    novelty: float = 0
    priority: float = 0
    created: int
    spoken_at: int | None = None
    answered_at: int | None = None
    outcome: str = "pending"


class Turn(_Row):
    session: str
    idx: int
    role: str
    content: str
    ts: int
    partial: int = 0
    extracted_at: int | None = None


class Metric(_Row):
    id: int | None = None
    ts: int
    session: str | None = None
    req_id: str | None = None
    t0: int | None = None
    t1: int | None = None
    t2: int | None = None
    t3: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0
