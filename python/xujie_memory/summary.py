"""摘要：合并、校验、版本化。

设计文档 5.8 / 12.2：
    合并创建新产物；旧摘要可归档，但依赖关系要保留。
    摘要不能成为精确数量计算、权限裁决和冲突解决的唯一依据。

这里的默认实现是**抽取式**（取每条痕迹的首句拼接），不调用模型。
接模型只需替换 `Summarizer`，其余逻辑不变。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from .engine import canonical_json, digest

# 省略校验用的词表。宁可误报，不可漏报——摘要丢一个"不"字，语义就反了。
# 但"别"歧义太大（"别忘""别人""特别"都不是否定），所以不收。
NEGATION = ("不", "没", "未", "甭")

# 这些搭配里虽然有否定字，但不构成否定命题，先剔除再检测。
SAFE_PHRASES = ("不过", "不管", "不仅", "不如", "不少", "无论", "没事", "没关系")
CONDITION = ("要是", "如果", "假如", "倘若", "万一", "除非")
PLAN = ("明天", "下次", "回头", "待会", "以后", "等我", "将来")


class Summarizer(Protocol):
    def __call__(self, texts: Sequence[str]) -> str:
        ...


def extractive(texts: Sequence[str], max_chars: int = 200) -> str:
    """默认实现：抽取式。不生成新内容，只挑原文片段。

    好处：永远不会引入原文没有的说法。
    代价：长对话里信息覆盖差。接模型时替换这个函数即可。
    """
    parts: list[str] = []
    used = 0
    for t in texts:
        head = t.strip().split("。")[0]
        if not head:
            continue
        if used + len(head) > max_chars:
            break
        parts.append(head)
        used += len(head)
    return "；".join(parts)


@dataclass
class SummaryResult:
    summary_id: str
    text: str
    input_refs: list[str]
    validation_status: str          # ok / failed
    warnings: list[str]


def validate_omissions(texts: Sequence[str], summary: str) -> list[str]:
    """检查摘要是否丢掉了会改变语义的成分（12.2）。

    这是启发式，不是证明。它只能发现「原文有、摘要没有」的显性丢失，
    发现不了「换了说法但意思变了」。
    """
    warnings: list[str] = []
    source = "".join(texts)
    for phrase in SAFE_PHRASES:
        source = source.replace(phrase, "　")

    for label, words in (("negation", NEGATION), ("condition", CONDITION), ("plan", PLAN)):
        src_hits = {w for w in words if w in source}
        if not src_hits:
            continue
        miss = [w for w in src_hits if w not in summary]
        if miss:
            warnings.append(f"{label}_lost:{','.join(sorted(miss))}")

    # 数字：摘要里出现的数字必须能在原文里找到
    import re
    src_nums = set(re.findall(r"\d+", source))
    sum_nums = set(re.findall(r"\d+", summary))
    extra = sum_nums - src_nums
    if extra:
        warnings.append(f"numbers_not_in_source:{','.join(sorted(extra))}")

    return warnings


class SummaryStore:
    def __init__(self, engine):
        self.engine = engine

    # ------------------------------------------------------------- 生成
    def create(
        self,
        scope_id: str,
        trace_ids: Sequence[str],
        summarizer: Summarizer = extractive,
        generator_version: str = "extractive-v1",
        coverage: dict | None = None,
    ) -> SummaryResult:
        texts: list[str] = []
        for tid in trace_ids:
            t = self.engine.get_trace(tid)
            if t is None or t["erased_at"]:
                continue
            texts.append(t["content"])

        text = summarizer(texts)
        warnings = validate_omissions(texts, text)
        status = "failed" if any(w.startswith("negation_lost") for w in warnings) else "ok"

        summary_id = "sm_" + digest(scope_id, canonical_json(list(trace_ids)), self.engine.now())[:24]
        self.engine._conn().execute(
            "INSERT INTO summaries(summary_id, scope_id, input_refs_json, text,"
            " generator_version, coverage_json, omissions_json, validation_status,"
            " lifecycle_state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                summary_id, scope_id, canonical_json(list(trace_ids)), text,
                generator_version, canonical_json(coverage or {}),
                canonical_json(warnings), status, "active", self.engine.now(),
            ),
        )
        return SummaryResult(summary_id, text, list(trace_ids), status, warnings)

    # ------------------------------------------------------------- 合并
    def merge(
        self,
        scope_id: str,
        summary_ids: Sequence[str],
        summarizer: Summarizer = extractive,
        generator_version: str = "extractive-v1",
    ) -> SummaryResult:
        """合并多个摘要：生成新产物，旧产物标 merged，不覆盖（12.2）。"""
        conn = self.engine._conn()
        # 顺序按时间正序，不按 IN 列表顺序，也不按数据库返回顺序。
        #
        # 摘要是给人读的：「还你三斤；再还两斤；免一斤吧」和
        # 「免一斤吧；再还两斤；还你三斤」是两段不同的话。
        # 而 IN 查询的返回顺序**不保证**（这里实测是倒的），
        # 所以必须显式 ORDER BY，并用 summary_id 兜底保证可复现（I2）。
        rows = conn.execute(
            f"SELECT * FROM summaries WHERE summary_id IN ({','.join('?' * len(summary_ids))})"
            f" ORDER BY created_at ASC, summary_id ASC",
            tuple(summary_ids),
        ).fetchall()
        if not rows:
            raise ValueError("no summaries to merge")

        texts = [r["text"] for r in rows]
        inputs: list[str] = []
        for r in rows:
            inputs.extend(json.loads(r["input_refs_json"]))

        text = summarizer(texts)
        warnings = validate_omissions(texts, text)
        if warnings:
            # 校验失败保留旧产物，不能因为「更短」就替换（12.2）
            return SummaryResult("", text, inputs, "failed", warnings)

        result = self.create(scope_id, inputs, summarizer, generator_version,
                             coverage={"merged_from": list(summary_ids)})
        conn.execute(
            f"UPDATE summaries SET lifecycle_state='merged'"
            f" WHERE summary_id IN ({','.join('?' * len(summary_ids))})",
            tuple(summary_ids),
        )
        return result

    # ------------------------------------------------------------- 读
    def active(self, scope_id: str) -> list[dict]:
        # summary_id 是兜底排序键：同一秒内产生的多条摘要，光按 created_at
        # 排出来的顺序由存储引擎决定——这次是 A、下次可能是 B。
        # 而整合周期要按这个顺序分组合并，顺序不稳就等于**不幂等**（I2）。
        rows = self.engine._conn().execute(
            "SELECT * FROM summaries WHERE scope_id=? AND lifecycle_state='active'"
            " ORDER BY created_at DESC, summary_id ASC",
            (scope_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, summary_id: str) -> dict | None:
        row = self.engine._conn().execute(
            "SELECT * FROM summaries WHERE summary_id=?", (summary_id,)
        ).fetchone()
        return dict(row) if row else None


def expire_states(engine, scope_id: str, now: str | None = None) -> list[str]:
    """按规格的 validity 把过期状态转 stale（12.3）。

    注意：这只改 status，不改 value。历史上仍可查，只是不再充当「现在」。
    """
    now = now or engine.now()
    stale: list[str] = []
    conn = engine._conn()

    for spec in engine.registry.all():
        if spec.validity != "ttl" or not spec.ttl_seconds:
            continue
        rows = conn.execute(
            "SELECT state_key, status FROM states WHERE scope_id=? AND status IN ('known','stale')",
            (scope_id,),
        ).fetchall()
        for row in rows:
            if relation_of_key(row["state_key"]) != spec.relation:
                continue
            last = conn.execute(
                "SELECT MAX(effective_at) FROM events WHERE state_key=? AND void=0",
                (row["state_key"],),
            ).fetchone()[0]
            if last is None:
                continue
            from datetime import datetime, timezone
            try:
                ts = datetime.fromisoformat(last)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            cur = datetime.fromisoformat(now)
            if cur.tzinfo is None:
                cur = cur.replace(tzinfo=timezone.utc)
            if (cur - ts).total_seconds() > spec.ttl_seconds:
                conn.execute(
                    "UPDATE states SET status='stale' WHERE state_key=?",
                    (row["state_key"],),
                )
                stale.append(row["state_key"])
    return stale


def relation_of_key(state_key: str) -> str:
    parts = state_key.split("|")
    return parts[2] if len(parts) >= 3 else ""
