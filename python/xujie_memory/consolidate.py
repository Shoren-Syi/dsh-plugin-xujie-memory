"""整合周期：把碎片收起来（方案 5.7，实现顺序第六步）。

方案原话：

> 以上六步不必实时执行。连续的编码会产生碎片。碎片需要周期性整合：
> 合并同类摘要、重算权重、归档、重建线索。
> 这个周期是机制的一部分，不是优化。
> 没有它，记忆只会越存越碎，最终无法回答「现在什么重要」。

方案列了四件事，**本实现只做两件半**。这不是没做完，是刻意：

| 方案写的 | 本实现 | 为什么 |
|---|---|---|
| 合并同类摘要 | 做，但只按**结构**合并 | 按语义合并要模型判断，破坏 I2 |
| 归档 | 做，归档前验证可恢复 | I4 |
| 重算权重 | **不做** | 权重不存库，是 `decay.priority()` 每次算出来的 |
| 重建线索 | **不做** | 线索就是 SQL 索引，没有独立的线索表 |

后两件如果不做会出事，那必须做。做了才是问题：把派生值存下来，
就要维护一致性；一旦有两条写路径，迟早不一致。而 `priority()` 是纯函数，
它就是唯一的真相——**这是比"存一份"更强的保证，不是偷懒**。

## 合并的边界：只按结构，不按语义

「这两段摘要意思相近，合了吧」——这句话本身需要模型判断，
而模型判断不满足 I2（同样输入同样输出）。所以合并只能靠**结构证据**：

    同一批输入痕迹来源相同（source_ref 前缀一致，例如 c06-1 / c06-2 / c06-3）
    且都是短碎片（≤ fragment_max_chars）
    且都没被合并过（coverage 里没有 merged_from）

代价是：跨会话的同类碎片不会合并。接受——**宁可碎，不可错**。
碎只是不好看，错是把两件不同的事揉成一件。

## 幂等

跑两遍必须得到同样的结果，否则这个周期不能定时跑（I2）。
靠三条保证：

    合并后旧摘要 lifecycle_state='merged'，第二遍的 active 里没有它们
    归档后 level='archived'，第二遍的 sweep 只看 level='hot'
    新产物的 coverage 里有 merged_from，第二遍不再参与合并

第二条尤其重要：`sweep()` 本来就只扫 hot，所以"归档过的不会再归档"
不是靠额外判断，是靠状态机本身。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .decay import Lifecycle
from .summary import SummaryStore, extractive

# 合并时会话归属的判定：source_ref 形如 "c06_flour-3"，取最后一段的前缀。
# 没有 "-" 的就整条当分组键——那说明这个 source_ref 本身已经是一件事了。
_REF_SEP = "-"


@dataclass
class ConsolidationReport:
    """一次整合做了什么、**没做什么**。

    `kept` 和 `skipped_steps` 不是调试信息，是 I5 的一部分：
    用户问「为什么这段记忆还在热区」，系统得答得出来。
    """

    scope_id: str
    started_at: str
    finished_at: str = ""
    dry_run: bool = False

    traces_examined: int = 0
    summaries_before: int = 0

    # 给"还没有摘要的痕迹"生成的摘要。它和 merged 是两步：
    # digested 产出碎片，merged 收拾碎片。
    digested: list[dict[str, Any]] = field(default_factory=list)
    summaries_merged: list[dict[str, Any]] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    kept: list[dict[str, Any]] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "traces_examined": self.traces_examined,
            "summaries_before": self.summaries_before,
            "digested": self.digested,
            "summaries_merged": self.summaries_merged,
            "archived": self.archived,
            "kept": self.kept,
            "skipped_steps": self.skipped_steps,
            "errors": self.errors,
        }

    @property
    def touched(self) -> int:
        return len(self.digested) + len(self.summaries_merged) + len(self.archived)


# ---------------------------------------------------------------- 入口
def consolidate(
    engine,
    scope_id: str,
    *,
    min_age_days: float = 30.0,
    threshold: float = 0.05,
    fragment_max_chars: int = 120,
    limit: int = 200,
    dry_run: bool = False,
) -> ConsolidationReport:
    """跑一次整合。可以重复跑，第二次通常是空的。"""
    report = ConsolidationReport(
        scope_id=scope_id,
        started_at=engine.now(),
        dry_run=dry_run,
        skipped_steps=[
            "recompute_weights: 权重是 decay.priority() 算出来的，库里没有可重算的值",
            "rebuild_cues: 线索由 SQL 索引承担，本实现没有独立线索表",
        ],
    )

    try:
        # 顺序不能反：先有摘要，才有"可合并的碎片"。
        _digest_uncovered(engine, scope_id, report, limit=limit, dry_run=dry_run)
        _merge_fragments(engine, scope_id, report,
                         fragment_max_chars=fragment_max_chars, limit=limit,
                         dry_run=dry_run)
        _archive_low_weight(engine, scope_id, report,
                            min_age_days=min_age_days, threshold=threshold,
                            limit=limit, dry_run=dry_run)
    except Exception as exc:                    # noqa: BLE001
        # 周期任务不能因为一条脏数据整轮不跑，但也不能假装没发生。
        report.errors.append(f"{type(exc).__name__}: {exc}")

    report.finished_at = engine.now()
    return report


# ---------------------------------------------------------------- 第一步
def _group_key(engine, summary: dict) -> str | None:
    """这条摘要属于哪一堆。读不出来就返回 None（不参与合并）。

    分组键来自**输入痕迹的来源**，不来自摘要正文——
    正文要靠"看得懂"才能分组，而"看得懂"是不确定的。
    """
    try:
        refs = json.loads(summary["input_refs_json"])
    except (TypeError, ValueError):
        return None
    if not refs:
        return None
    trace = engine.get_trace(refs[0])
    if trace is None:
        return None
    return _ref_group(trace["source_ref"])


def _digest_uncovered(
    engine, scope_id: str, report: ConsolidationReport,
    *, limit: int, dry_run: bool,
) -> None:
    """把**还没有摘要**的痕迹按来源分批摘要成一条。

    为什么这一步必须在合并之前：合并的对象是"已有的短摘要"，
    而写路径从来只写痕迹、不写摘要。所以一个真实库里的摘要数
    长期是 0——**合并永远没有对象**。

    这和 `lifecycle` 那条是同一类问题：机制写好了，但没人给它喂数据。
    v0.34 的端到端验收两次撞到它（第一次归档 0 条，第二次合并 0 组）。

    分批的依据是 `source_ref` 前缀：`c01_rice-1..10` 属于同一段对话，
    合起来读是连贯的；跨段合并会把"米"和"酒"揉成一句话。
    """
    store = SummaryStore(engine)
    covered: set[str] = set()
    actives = store.active(scope_id)[:limit]
    for s in actives:
        try:
            covered.update(json.loads(s["input_refs_json"]))
        except (TypeError, ValueError):
            continue

    rows = engine._conn().execute(
        "SELECT trace_id, source_ref FROM traces"
        " WHERE scope_id=? AND erased_at IS NULL ORDER BY recorded_at, trace_id LIMIT ?",
        (scope_id, limit * 10),
    ).fetchall()

    buckets: dict[str, list[str]] = {}
    for row in rows:
        if row["trace_id"] in covered:
            continue
        key = _ref_group(row["source_ref"])
        if key:
            buckets.setdefault(key, []).append(row["trace_id"])

    for key, tids in buckets.items():
        # 一条也摘要。单条不是"合并"，但它是碎片的最小单位；
        # 只处理 >=2 条的话，一段只聊了一句的对话永远进不了摘要。
        if dry_run:
            report.digested.append({"group": key, "traces": len(tids), "summary_id": ""})
            continue
        try:
            result = store.create(scope_id, tids, summarizer=extractive)
        except Exception as exc:                # noqa: BLE001
            report.errors.append(f"digest {key}: {type(exc).__name__}: {exc}")
            continue
        report.digested.append({
            "group": key, "traces": len(tids), "summary_id": result.summary_id,
        })


def _ref_group(source_ref: str) -> str | None:
    """`c01_rice-3` → `c01_rice`；没有分隔符就整条当一组。"""
    if not source_ref:
        return None
    if _REF_SEP in source_ref:
        return source_ref.rsplit(_REF_SEP, 1)[0] or None
    return source_ref


def _merge_fragments(
    engine, scope_id: str, report: ConsolidationReport,
    *, fragment_max_chars: int, limit: int, dry_run: bool,
) -> None:
    store = SummaryStore(engine)
    actives = store.active(scope_id)[:limit]
    report.summaries_before = len(actives)

    buckets: dict[str, list[dict]] = {}
    for s in actives:
        # 空摘要不参与合并。它不是"短"，它是"什么都没提出来"——
        # 两条空摘要合起来还是空，但会消耗一次 merge 的校验和一次写入。
        # （`extractive` 遇上超过 max_chars 的单句就会产出空串。）
        if not s["text"].strip():
            report.kept.append({"summary_id": s["summary_id"], "reason": "empty"})
            continue
        if len(s["text"]) > fragment_max_chars:
            continue
        # 已经合并过的产物不再参与合并。
        #
        # 不加这条会无限合并：A+B 产生 C，下一轮 C+D 产生 E，再下一轮 E+F……
        # 每轮都对（都满足"两条以上短碎片"），但摘要会越来越像一个
        # 覆盖全文的巨型句子，最后连"这是一件事"都读不出来。
        coverage = json.loads(s["coverage_json"] or "{}")
        if coverage.get("merged_from"):
            continue
        # 校验失败的摘要不参与合并：它自己就已经丢了东西，
        # 再把两条丢过东西的拼起来，等于把损失叠一层。
        if s["validation_status"] == "failed":
            report.kept.append({
                "summary_id": s["summary_id"],
                "reason": "validation_failed",
            })
            continue
        key = _group_key(engine, s)
        if key:
            buckets.setdefault(key, []).append(s)

    for key, group in buckets.items():
        if len(group) < 2:
            continue
        ids = [s["summary_id"] for s in group]
        if dry_run:
            report.summaries_merged.append({"group": key, "from": ids, "to": ""})
            continue
        try:
            result = store.merge(scope_id, ids, summarizer=extractive)
        except Exception as exc:                # noqa: BLE001
            report.errors.append(f"merge {key}: {type(exc).__name__}: {exc}")
            continue
        if result.summary_id:
            report.summaries_merged.append({
                "group": key, "from": ids, "to": result.summary_id,
            })
        else:
            # merge 内部校验失败会保留旧产物（返回空 id）。
            # 这不是错误，是拒绝——但要记下来，否则"为什么没合"无从查。
            report.kept.append({
                "group": key, "summary_ids": ids,
                "reason": "omission_check_failed",
                "warnings": result.warnings,
            })


# ---------------------------------------------------------------- 第二步
def _referenced_by_known_state(engine, trace_ids: list[str]) -> set[str]:
    """哪些痕迹还在给「当前有效的状态」作证。

    这些不能归档——归档本身是可逆的（I4），但一条仍在支撑余额的痕迹
    被挪到冷区，下次追查来源就要多跳一次。代价不大，但没必要。
    这条边界来自 XM-07。
    """
    if not trace_ids:
        return set()
    conn = engine._conn()
    rows = conn.execute(
        f"""
        SELECT DISTINCT e.trace_id
          FROM event_evidence e
          JOIN events v ON v.event_id = e.event_id
          JOIN states s ON s.state_key = v.state_key
         WHERE s.status IN ('known', 'disputed')
           AND v.void = 0
           AND e.trace_id IN ({','.join('?' * len(trace_ids))})
        """,
        tuple(trace_ids),
    ).fetchall()
    return {r["trace_id"] for r in rows}


def _archive_low_weight(
    engine, scope_id: str, report: ConsolidationReport,
    *, min_age_days: float, threshold: float, limit: int, dry_run: bool,
) -> None:
    lc = Lifecycle(engine)
    candidates = lc.sweep(
        scope_id, threshold=threshold, min_age_days=min_age_days,
        limit=limit, dry_run=True,
    )
    report.traces_examined = len(candidates)
    if not candidates:
        return

    # 先剔掉"还在给有效状态作证"的那批，再动手。
    # 顺序不能反：先归档再补救，中间那段时间里来源链路是断的。
    protected = _referenced_by_known_state(engine, candidates)
    to_archive = [t for t in candidates if t not in protected]
    for tid in candidates:
        if tid in protected:
            report.kept.append({"trace_id": tid, "reason": "supports_live_state"})

    if dry_run:
        report.archived.extend(to_archive)
        return

    # 逐条走 archive()，**不再回头调一次 sweep**。
    #
    # 先算出 to_archive 再调 sweep 是个陷阱：sweep 只看"低权重 + 够老"，
    # 它不知道哪些痕迹还在给有效状态作证，于是会把刚剔掉的那批一起扫走。
    # dry_run 报告和真实结果就此分叉，而这种"报的和做的不一样"最难查。
    for tid in to_archive:
        if lc.archive(tid):
            report.archived.append(tid)
        else:
            # archive() 返回 False 只有两种：痕迹不存在，或已被擦除。
            # 后者是用户行使了删除权，不是失败，记下来即可。
            report.kept.append({"trace_id": tid, "reason": "not_recoverable"})
