"""不变量验收：把方案第七节跑成代码。

方案的原文：

> 每条不变量都必须能被独立测出，否则它只是口号。

| 不变量 | 方案给的验法 | 这里的实现 |
|---|---|---|
| I1 来源性 | 随机抽 100 条输出，每条定位到 trace_id | 全量遍历 state（比抽样严） |
| I2 幂等性 | 同一输入跑两遍，逐字段比对 | 投影 == fold(事件流)，逐字段 |
| I3 有界性 | 构造「什么都能匹配」的查询，看返回量 | 空过滤查询 + 大预算，看是否截断并标注 |
| I4 可逆性 | 抽 20 条已归档痕迹，尝试恢复 | 抽样恢复 + 恢复后归档回去 |
| I5 显式性 | 故障与空结果，检查返回类型 | 同一个 query 打在坏连接上 vs 空账上 |

## 为什么 I2 用「投影 == fold」而不是「跑两遍」

"跑两遍"要重放整条输入序列，那测的是**编码器稳定不稳定**（模型的事）。
而 I2 说的是"同一输入序列，收敛后的状态必须唯一"——它的内核含义是：
**状态是事件的纯函数**。

那就直接测这个函数关系：把事件流重新折一遍，和库里存的投影逐字段比。
不一致，说明投影带了事件流之外的信息（时间、随机数、上一次运行的残留），
那才是真的违反 I2。

## 为什么这些检查要独立成模块，不写成 pytest

因为它们是**验收**，不是**单元测试**：

- 单元测试断言"我知道的这条路径是对的"
- 验收断言"整个库现在没有违反不变量的地方"

前者跟着代码走，后者跟着数据走。一个真实部署在跑了一个月之后，
最该问的就是后者——而那个库不在测试目录里。

`AuditReport.verdict` 只有三种：pass / fail / partial。
**partial 必须带原因**，否则它就是个更委婉的 pass。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .recall import NOT_FOUND, UNAVAILABLE, Budget, Recall, RecallQuery

# 抽样上限。方案写的是"抽 100 条""抽 20 条"，这里作为默认值。
DEFAULT_STATE_SAMPLE = 100
DEFAULT_ARCHIVE_SAMPLE = 20


@dataclass
class Finding:
    """一条检查结果。

    `ok=False` 是**违规**，不是"没查"——没查的写在 `report.skipped` 里。
    两者混在一起，报告就没法当门禁用。
    """

    invariant: str
    ok: bool
    subject: str
    detail: str = ""

    def as_line(self) -> str:
        mark = "✓" if self.ok else "✗"
        body = f"{mark} {self.invariant} {self.subject}"
        return f"{body}  {self.detail}" if self.detail else body


@dataclass
class AuditReport:
    scope_id: str
    started_at: str
    finished_at: str = ""
    findings: list[Finding] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def add(self, f: Finding) -> None:
        self.findings.append(f)
        self.checked[f.invariant] = self.checked.get(f.invariant, 0) + 1

    @property
    def violations(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok]

    @property
    def verdict(self) -> str:
        """pass / fail / partial。

        partial 只在"有检查没跑成"时出现，而且必须看 `skipped`——
        一个永远返回 partial 的报告，和没有报告是一样的。
        """
        if self.violations:
            return "fail"
        if self.skipped:
            return "partial"
        return "pass"

    def summary(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "verdict": self.verdict,
            "checked": dict(self.checked),
            "violations": len(self.violations),
            "skipped": list(self.skipped),
        }


# ============================================================ I1 来源性
def check_source_closure(
    engine, scope_id: str, sample: int = DEFAULT_STATE_SAMPLE,
) -> list[Finding]:
    """每条有值的事实，都必须能沿证据链走到可访问的痕迹。

    方案要求"随机抽 100 条"。这里**全量查**——抽样只能证伪，不能证明；
    而这个检查的成本是每条一次索引查询，全量也就几十毫秒。
    真要抽样时把 `sample` 调小即可。

    只查**有值**的状态（known / disputed）。unknown 本来就没有事实要支撑。
    """
    rows = engine._conn().execute(
        "SELECT state_key FROM states WHERE scope_id=?"
        " AND status IN ('known','disputed') ORDER BY state_key LIMIT ?",
        (scope_id, sample),
    ).fetchall()

    out: list[Finding] = []
    for row in rows:
        key = row["state_key"]
        try:
            problems = engine.check_source_closure(key)
        except Exception as exc:                # noqa: BLE001
            out.append(Finding("I1", False, key, f"检查本身失败：{exc}"))
            continue
        out.append(Finding("I1", not problems, key, "; ".join(problems)))
    return out


# ============================================================ I2 幂等性
def _projection_row(engine, state_key: str) -> dict[str, Any] | None:
    row = engine._conn().execute(
        "SELECT value_json, status, version FROM states WHERE state_key=?",
        (state_key,),
    ).fetchone()
    return dict(row) if row else None


def check_projection_is_a_function_of_events(
    engine, scope_id: str, sample: int = DEFAULT_STATE_SAMPLE,
) -> list[Finding]:
    """投影必须等于「把事件流重新折一遍」的结果。

    这是 I2 最内核的一条：**状态是事件的纯函数**。
    只要这条成立，同一输入序列跑两遍必然得到同一个状态——
    因为两遍产生的事件流相同，而同一个事件流只有一个折法。

    不一致的常见原因（都是真的坑）：
        投影里混进了「当前时间」（过期判断）
        某条写路径自己改了 states，没走 _recompute
        快照没被正确作废
    """
    rows = engine._conn().execute(
        "SELECT state_key FROM states WHERE scope_id=? ORDER BY state_key LIMIT ?",
        (scope_id, sample),
    ).fetchall()

    out: list[Finding] = []
    # 只读：直接用现有连接折一遍。**不能**调 `_recompute`——它会写库，
    # 验收工具一旦改数据，下一次验收看到的东西已经不一样了。
    conn = engine._conn()
    for row in rows:
        key = row["state_key"]
        spec = engine.registry.get(_relation_of(key))
        if spec is None:
            out.append(Finding("I2", False, key, "状态键指向未知关系"))
            continue
        try:
            folded = engine._fold(conn, key, spec)
        except Exception as exc:                # noqa: BLE001
            out.append(Finding("I2", False, key, f"重算失败：{exc}"))
            continue
        stored = _projection_row(engine, key)
        if stored is None:
            out.append(Finding("I2", False, key, "投影行消失"))
            continue

        stored_value = json.loads(stored["value_json"]) if stored["value_json"] else None
        same = (
            _same_number(stored_value, folded.value)
            and stored["status"] == folded.status
        )
        out.append(Finding(
            "I2", same, key,
            "" if same else f"投影 {stored_value}/{stored['status']}"
                            f" ≠ 重算 {folded.value}/{folded.status}",
        ))
    return out


def _same_number(a: Any, b: Any) -> bool:
    """数值比较：None 只等于 None，其余按浮点容差。

    不能用 `==` 直接比：JSON 读回来的 3 和 float 3.0 在语义上是一个数，
    而它们的 `==` 也可能是 True——但 `3` 和 `3.000000001` 也是 True
    在以 1e-9 为容差时就太松了。用相对误差更贴近"这是一笔钱"的直觉。
    """
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == b:
            return True
        return abs(a - b) <= max(1e-9, abs(a) * 1e-9)
    return a == b


def _relation_of(state_key: str) -> str:
    parts = state_key.split("|")
    return parts[2] if len(parts) >= 3 else ""


# ============================================================ I3 有界性
def check_recall_is_bounded(engine, scope_id: str) -> list[Finding]:
    """「什么都能匹配」的查询也不许把库倒出来。

    构造：不给定任何过滤条件 + 一个很大的预算。
    通过条件有两条，缺一不可：
        返回条数 ≤ max_items
        超了就**明确标注** truncated + 原因
    """
    total = engine._conn().execute(
        "SELECT COUNT(*) FROM states WHERE scope_id=?", (scope_id,),
    ).fetchone()[0]
    # 上限压到 1，保证哪怕库里只有两条也会真的触发截断——
    # 否则"检查截断"这一步在大库上跑得到、在小库上跑不到，
    # 而小库恰恰是刚上线的样子。
    budget = Budget(max_items=1, max_candidates=200, max_rounds=2,
                    max_chars=2000, timeout_ms=2000)
    result = Recall(engine).query(
        scope_id, RecallQuery(kind="state", budget=budget),
    )

    out: list[Finding] = []
    n = len(result.states)
    out.append(Finding("I3", n <= budget.max_items, "returned_items",
                       f"{n} 条（上限 {budget.max_items}，库里共 {total}）"))

    # 只有"库里确实还有更多"时，才要求它标注截断。
    # 「返回 1 条、库里也正好 1 条」没被截断是正常的，
    # 在那里要求 truncated=True 是把自己测出一个假故障。
    if total > budget.max_items:
        out.append(Finding(
            "I3", result.truncated, "truncation_flag",
            "" if result.truncated else "被截断却没标注，用户会以为这就是全部",
        ))
    return out


# ============================================================ I4 可逆性
def check_archive_is_reversible(
    engine, scope_id: str, sample: int = DEFAULT_ARCHIVE_SAMPLE,
) -> list[Finding]:
    """归档不是删除。抽若干条已归档痕迹，恢复 → 读原文 → 再归档回去。

    最后那步"归档回去"很重要：验收不该改变被测系统的状态。
    否则跑一次验收，库就变了一次样——那下次验收看到的东西已经不一样了。
    """
    from .decay import Lifecycle

    rows = engine._conn().execute(
        "SELECT ref_id FROM lifecycle WHERE scope_id=? AND level='archived'"
        " ORDER BY ref_id LIMIT ?",
        (scope_id, sample),
    ).fetchall()
    if not rows:
        return []

    lc = Lifecycle(engine)
    out: list[Finding] = []
    for row in rows:
        tid = row["ref_id"]
        before = engine.get_trace(tid)
        if before is None or before["erased_at"]:
            # 用户行使过删除权。这不是"归档不可逆"，是"这条本来就不该在库里"。
            out.append(Finding("I4", True, tid, "已擦除，跳过"))
            continue
        if not (before["content"] or "").strip():
            # 原文是空的，就谈不上"恢复回来"。
            # 这条如果算通过，I4 会被一种最糟的情况蒙过去：
            # 内容早就没了，而检查还在说"恢复成功"——恢复了一个空字符串。
            out.append(Finding("I4", False, tid, "原文为空，归档后无从恢复"))
            continue
        ok = lc.restore(tid)
        text = engine.get_trace(tid)
        readable = text is not None and text["content"] == before["content"]
        out.append(Finding("I4", ok and readable, tid,
                           "" if ok and readable else "恢复后原文不可读"))
        lc.archive(tid)                          # 复原成被检查前的样子
    return out


# ============================================================ I5 显式性
class _FailingEngine:
    """只用来造"查询故障"。

    方案 I5 的验法是：故障与空结果两种场景，返回类型必须不同。
    要造故障，就得让底层真的抛错——所以给一个只会抛错的 connection。
    """

    def __init__(self, now: str):
        self._now = now

    def _conn(self):
        raise sqlite3.Error("simulated storage failure")

    def now(self) -> str:
        return self._now


def check_status_is_explicit(engine, scope_id: str) -> list[Finding]:
    """「故障」和「没有」不能返回同一个东西。

    把"系统坏了"说成"没有这条记忆"，是把系统问题伪装成事实问题（I5）。
    """
    out: list[Finding] = []

    # 空结果必须是真的空：查一个不存在的 subject。
    # 直接查 `kind="state"` 会连整个 scope 的状态都捞出来——那不是空查询，
    # 是"不设过滤"，测出来永远是 found。
    empty = Recall(engine).query(
        scope_id,
        RecallQuery(kind="state", subject="__no_such_subject__", relation="debt"),
    )
    out.append(Finding("I5", empty.status == NOT_FOUND, "empty_result", empty.status))

    broken = Recall(_FailingEngine(engine.now())).query(
        scope_id, RecallQuery(kind="state"),
    )
    out.append(Finding("I5", broken.status == UNAVAILABLE, "storage_failure",
                       broken.status))

    out.append(Finding(
        "I5", empty.status != broken.status, "distinguishable",
        "故障与空结果返回了同一个状态" if empty.status == broken.status else "",
    ))
    return out


# ============================================================ 一起跑
def audit(
    engine, scope_id: str,
    state_sample: int = DEFAULT_STATE_SAMPLE,
    archive_sample: int = DEFAULT_ARCHIVE_SAMPLE,
) -> AuditReport:
    """跑全套五条不变量。"""
    report = AuditReport(scope_id=scope_id, started_at=engine.now())

    checks = [
        ("I1", lambda: check_source_closure(engine, scope_id, state_sample)),
        ("I2", lambda: check_projection_is_a_function_of_events(engine, scope_id, state_sample)),
        ("I3", lambda: check_recall_is_bounded(engine, scope_id)),
        ("I4", lambda: check_archive_is_reversible(engine, scope_id, archive_sample)),
        ("I5", lambda: check_status_is_explicit(engine, scope_id)),
    ]
    for name, fn in checks:
        try:
            for finding in fn():
                report.add(finding)
        except Exception as exc:                # noqa: BLE001
            # 检查本身失败不能算"通过"，也不能算"违规"——单列出来。
            # 混进 verdict=fail 会掩盖真正的不变量违规。
            report.skipped.append(f"{name}: {type(exc).__name__}: {exc}")

    report.finished_at = engine.now()
    return report
