"""关系规格（RelationSpec）。

设计文档 7.1 的理由：哪些关系可自动接受、负值是否允许、谁能写，
不能由模型临时决定，也不应散落在代码里。每种关系用一份版本化规格声明，
内核只执行规格，不自行推断。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import SpecNotFound

RISK_TIERS = ("R0", "R1", "R2", "R3")
ADMISSION_MODES = ("shadow", "assisted", "auto")
EFFECT_KINDS = ("set", "delta", "invalidate")
MODALITIES = (
    "asserted",
    # 请求 / 祈使。「给我免三碗」不是在陈述事实，也不是假设——
    # 说话人是真的在要求这件事。缺了这一格，它会被判成 hypothetical
    # 然后 record_only，用户明确要求的事反而不记（实测踩到）。
    "request",
    "hypothetical",
    "conditional",
    "quoted",
    "denied",
    "planned",
    "joking",
    "ironic",
    "in_world",
    "unclear",
)


@dataclass(frozen=True)
class RelationSpec:
    relation: str
    version: str
    risk_tier: str
    required_slots: tuple[str, ...]
    allowed_modalities: tuple[str, ...]
    writers: tuple[str, ...]
    effect_kind: str
    admission_mode: str = "shadow"
    value_type: str = "integer"          # integer / number / string / enum
    # XM-12: unit 只用于展示，不参与 state_key，也不参与校验。
    #        编码器给出与规格不符的单位时不会报错——这是有意的取舍，见 ISSUES。
    #        真要校验，应判 disputed 而不是拒绝（拒绝会丢掉证据）。
    unit: str | None = None
    allow_negative: bool = False
    enum_values: tuple[str, ...] = ()
    confirmation: str = "none"           # none / implicit / explicit / authority
    validity: str = "permanent"          # permanent / ttl / reconfirm
    ttl_seconds: int | None = None
    conflict_policy: str = "dispute"     # dispute / reject / last_write_wins
    conservation: str | None = None      # None / "repayment"：追踪累计偿付量
    feeds: tuple[str, ...] = ()
    derived_from: tuple[str, ...] = ()
    note: str = ""

    # ------------------------------------------------------------- 自检
    def validate(self) -> None:
        if self.risk_tier not in RISK_TIERS:
            raise ValueError(f"{self.relation}: bad risk_tier {self.risk_tier!r}")
        if self.admission_mode not in ADMISSION_MODES:
            raise ValueError(f"{self.relation}: bad admission_mode {self.admission_mode!r}")
        if self.effect_kind not in EFFECT_KINDS:
            raise ValueError(f"{self.relation}: bad effect_kind {self.effect_kind!r}")
        for m in self.allowed_modalities:
            if m not in MODALITIES:
                raise ValueError(f"{self.relation}: unknown modality {m!r}")
        if not self.required_slots:
            raise ValueError(f"{self.relation}: required_slots must not be empty")
        # R3 永远不允许自动提交（设计文档 7.3 / 15.5）
        if self.risk_tier == "R3" and self.admission_mode == "auto":
            raise ValueError(
                f"{self.relation}: R3 relation must not be 'auto' "
                "(permissions/deletion go through the control channel only)"
            )
        if self.value_type == "enum" and not self.enum_values:
            raise ValueError(f"{self.relation}: enum value_type requires enum_values")
        if self.conservation not in (None, "repayment"):
            raise ValueError(f"{self.relation}: unknown conservation {self.conservation!r}")

    @property
    def spec_version(self) -> str:
        return f"{self.relation}@{self.version}"


@dataclass
class SpecRegistry:
    _by_relation: dict[str, RelationSpec] = field(default_factory=dict)

    # ------------------------------------------------------------- 加载
    @classmethod
    def from_dir(cls, path: str | Path) -> "SpecRegistry":
        reg = cls()
        for f in sorted(Path(path).glob("*.json")):
            reg.add(RelationSpec(**json.loads(f.read_text(encoding="utf-8"))))
        return reg

    @classmethod
    def from_specs(cls, specs: list[RelationSpec]) -> "SpecRegistry":
        reg = cls()
        for s in specs:
            reg.add(s)
        return reg

    def add(self, spec: RelationSpec) -> None:
        spec.validate()
        if spec.relation in self._by_relation:
            raise ValueError(f"duplicate relation spec: {spec.relation}")
        self._by_relation[spec.relation] = spec

    # ------------------------------------------------------------- 查询
    def get(self, relation: str) -> RelationSpec:
        try:
            return self._by_relation[relation]
        except KeyError:
            raise SpecNotFound(f"no spec registered for relation {relation!r}") from None

    def all(self) -> list[RelationSpec]:
        return list(self._by_relation.values())

    # ------------------------------------------------------------- 依赖图
    def dependency_edges(self) -> list[tuple[str, str]]:
        """返回 (input_relation, derived_relation) 边列表，用于环检测。"""
        edges: list[tuple[str, str]] = []
        for spec in self._by_relation.values():
            for src in spec.derived_from:
                edges.append((src, spec.relation))
        return edges

    def assert_acyclic(self) -> None:
        """依赖图必须为 DAG（设计文档 9.5）。"""
        from .errors import DependencyCycle

        graph: dict[str, set[str]] = {}
        for src, dst in self.dependency_edges():
            graph.setdefault(src, set()).add(dst)

        WHITE, GREY, BLACK = 0, 1, 2
        color: dict[str, int] = {}

        def visit(node: str, stack: list[str]) -> None:
            color[node] = GREY
            stack.append(node)
            for nxt in graph.get(node, ()):
                c = color.get(nxt, WHITE)
                if c == GREY:
                    cycle = " -> ".join(stack[stack.index(nxt):] + [nxt])
                    raise DependencyCycle(f"dependency cycle: {cycle}")
                if c == WHITE:
                    visit(nxt, stack)
            stack.pop()
            color[node] = BLACK

        for node in list(graph):
            if color.get(node, WHITE) == WHITE:
                visit(node, [])


def slot_value(spec: RelationSpec, slots: dict[str, Any], name: str) -> Any:
    """取槽位；缺失时抛错，不做默认值填充（I5）。"""
    if name not in slots:
        raise KeyError(f"missing required slot {name!r} for relation {spec.relation!r}")
    return slots[name]
