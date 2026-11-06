"""抽取编排（M5.4）：扫窗口 → 预筛 → 调模型 → 入库 → 打标记。

这个文件本身不含业务逻辑，它只负责**顺序**和**失败时往哪走**。
所有真正的活都在 encode / persist / prescreen 里，且各自可单测。

顺序为什么是这个顺序：

    pending_turns → slice_windows → 预筛 → encode → persist → mark_extracted

  1. 先切片再预筛：预筛是"这句话值不值得抽"，切片是"哪几句话属于一个上下文"。
     反过来做，单句预筛会丢掉上下文（"他住院了"没有"老王"就抽不出实体）。
  2. 预筛在 encode 之前：这是唯一的省钱关卡。全窗寒暄 → 0 成本跳过。
  3. persist 在 mark 之前：写库失败时不能标记已抽，否则这条窗口永远丢了。
  4. 标记最后做，且**降级也标记**——降级已经把原文写进 raw 事件，
     再抽一次只会再失败一次，白烧钱。要重抽走 M15 的离线巩固。

关于 every_n_turns：它是"攒够几轮才抽"，不是"窗口最大几轮"。
攒够就抽、抽完就标，这样一次调用覆盖 5 轮，成本是每轮抽的 1/5。
代价是"聊了 3 轮就关掉"的那 3 轮会留在 pending——由 M21 心跳在空闲时补抽。
M5 不做空闲检测，CLI 的 `hippo extract --force` 可以先手动补。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..log import get_logger
from .encode import Encoder, EncodeOutcome, build_known_entities
from .persist import persist_degraded, persist_result
from .prescreen import EntityIndex, load_rules, prescreen, prescreen_heuristic
from .store import Store
from .turns import Window, slice_windows

log = get_logger("extractor")


@dataclass
class RunResult:
    """一个窗口的处理结果。CLI 直接打印它。"""

    session: str
    start_idx: int
    end_idx: int
    status: str                     # ok / degraded / skipped
    reason: str = ""                # skipped 的原因：incomplete / empty / prescreen
    events: int = 0
    facts: int = 0
    traits: int = 0
    plans: int = 0
    doubts: int = 0
    verbatim_fail: int = 0
    attempts: int = 1
    # ★ 这三个数之前**从来没被记录过**：RunResult 里没有对应字段，
    #   _record() 也就没传给 record_run，extract_runs 的
    #   prompt_tokens / completion_tokens 两列一直是 0。
    #   症状是"成本有数、token 为 0"，而成本是从另一个字段算的，
    #   所以报表看起来正常——直到你想算缓存命中率，才发现分母是 0。
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    latency_ms: int = 0
    error: str | None = None

    def line(self) -> str:
        head = f"[{self.start_idx:>3}-{self.end_idx:<3}] {self.status:<8}"
        if self.status == "skipped":
            return f"{head} {self.reason}"
        bits = [f"ev={self.events}"]
        for name, v in (("fact", self.facts), ("trait", self.traits),
                        ("plan", self.plans), ("doubt", self.doubts)):
            if v:
                bits.append(f"{name}={v}")
        if self.verbatim_fail:
            bits.append(f"丢={self.verbatim_fail}")
        bits.append(f"{self.latency_ms}ms")
        if self.cost:
            bits.append(f"¥{self.cost:.4f}")
        return f"{head} {' '.join(bits)}"


@dataclass
class SessionReport:
    session: str
    results: list[RunResult] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return round(sum(r.cost for r in self.results), 6)

    @property
    def events(self) -> int:
        return sum(r.events for r in self.results)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out


class Extractor:
    def __init__(
        self,
        store: Store,
        cfg: Config,
        encoder: Encoder,
        *,
        rules: Any = None,
    ):
        self.store = store
        self.cfg = cfg
        self.encoder = encoder
        self.rules = rules or load_rules(cfg.extract.lexicon_path or None)

    # ---------------------------------------------------------------- 预筛
    def _worth_extracting(self, window: Window, index: EntityIndex | None) -> tuple[bool, str]:
        """窗口里有没有一句值得抽的话。

        逐轮判断，任一轮过就抽整窗——**不能只抽过筛的那一句**：
        "他住院了"单独看没有实体，"老王"单独看没有事件，合起来才是记忆。
        """
        if not self.cfg.extract.prescreen:
            return True, "prescreen_off"
        mode = self.cfg.extract.prescreen_mode
        if mode == "off":
            return True, "prescreen_off"
        judge = prescreen
        kwargs: dict[str, Any] = {}
        if mode == "heuristic":
            judge = prescreen_heuristic
            kwargs = {"min_chars": self.cfg.extract.heuristic_min_chars}
        prev_assistant = ""
        for t in window.turns:
            if t.role == "assistant":
                prev_assistant = t.content
                continue
            if t.role != "user":
                continue
            if judge(t.content, entities=index, prev_assistant=prev_assistant,
                     rules=self.rules, **kwargs).passed:
                return True, "hit"
        return False, "prescreen"

    # ---------------------------------------------------------------- 主流程
    async def run(self, session: str, *, force: bool = False) -> SessionReport:
        """处理一个 session 里所有可抽的窗口。

        force=True：不足 every_n_turns 也抽（CLI 手动补、M21 空闲补）。
        无论 force 与否，**不完整的窗口一律不抽**——半截对话抽出的人物关系是错的。
        """
        rep = SessionReport(session=session)
        pending = self.store.pending_turns(session)
        if not pending:
            return rep

        user_turns = sum(1 for t in pending if t.role == "user")
        if not force and user_turns < self.cfg.extract.every_n_turns:
            return rep

        windows = slice_windows(
            pending,
            session=session,
            per_window=self.cfg.extract.every_n_turns,
            max_tokens=self.cfg.extract.window_tokens,
        )
        if not windows:
            return rep

        # 一个窗口建一次实体索引（R-4.2 的调用约定）
        index: EntityIndex | None = None
        known = "（暂无）"

        for w in windows:
            # ★ 只判"完整性"，不判"长短"。
            #   slice_windows 的 has_pending 把"最后一条是 user"和"token 太少"
            #   合成了一个标志。前者要留着下次再抽，后者要立刻放弃——
            #   混在一起处理，结果是寒暄窗口永远占着 pending，
            #   或者完整的好窗口被当成不完整反复重扫。
            if not w.complete:
                rep.results.append(RunResult(
                    session=session, start_idx=w.start_idx, end_idx=w.end_idx,
                    status="skipped", reason="incomplete"))
                # 不 mark：这条尾巴下次还要抽
                continue

            if index is None:
                index = EntityIndex.from_store(self.store, session)

            # 预筛过了就抽，哪怕这个窗口只有 20 个 token——
            # "我养了只猫"5 个字，价值比 500 字寒暄高。
            ok, why = self._worth_extracting(w, index)
            if not ok:
                self._skip(rep, session, w, why)
                continue

            if known == "（暂无）":
                try:
                    known = build_known_entities(
                        self.store.list_entities(session),
                        self.cfg.extract.known_entities_limit)
                except Exception:  # noqa: BLE001
                    known = "（暂无）"

            await self._one(rep, session, w, known)

        return rep

    async def _one(self, rep: SessionReport, session: str, w: Window,
                   known: str) -> None:
        out: EncodeOutcome = await self.encoder.encode(w, known=known)
        row = RunResult(
            session=session, start_idx=w.start_idx, end_idx=w.end_idx,
            status=out.status, attempts=out.attempts, cost=out.cost,
            latency_ms=out.latency_ms, error=out.error,
            verbatim_fail=out.verbatim_fail,
            prompt_tokens=out.prompt_tokens,
            completion_tokens=out.completion_tokens,
            cached_tokens=out.cached_tokens,
            reasoning_tokens=out.reasoning_tokens,
        )

        if out.ok and out.result is not None:
            try:
                st = persist_result(
                    self.store, session, w, out.result,
                    affect_baseline=self.cfg.affect.baseline,
                    tz=self.cfg.timezone,
                )
                row.events = len(st.event_ids)
                row.facts, row.traits = st.facts, st.traits
                row.plans, row.doubts = st.plans, st.doubts
                row.verbatim_fail = st.verbatim_fail
            except Exception as exc:  # noqa: BLE001
                # 入库失败：已经回滚了，此时**绝不能**标记已抽。
                # 让它留在 pending 里，下次重来。
                row.status = "error"
                row.error = f"persist: {exc!r}"
                log.error("extract.persist.failed", extra={"fields": {
                    "session": session, "idxs": [w.start_idx, w.end_idx],
                    "err": row.error}})
                self._record(row)
                rep.results.append(row)
                return
        elif self.cfg.extract.degrade_to_raw:
            # 降级：至少把原文写进去。这条单独事务，即使上面全崩也能落。
            tokens = self._entity_tokens(w, session)
            eid = persist_degraded(self.store, session, w, tokens=tokens)
            row.events = 1 if eid else 0
            if eid is None:
                row.status = "error"
                row.error = (row.error or "") + " / degrade failed"
                self._record(row)
                rep.results.append(row)
                return

        self.mark_done(session, w)
        self._record(row)
        rep.results.append(row)
        log.info("extract.done", extra={"fields": {
            "session": session, "idxs": [w.start_idx, w.end_idx],
            "status": row.status, "events": row.events,
            "latency_ms": row.latency_ms, "cost": row.cost}})

    # ---------------------------------------------------------------- 辅助
    def mark_done(self, session: str, w: Window) -> int:
        return self.store.mark_turns_extracted(session, w.end_idx)

    def _skip(self, rep: SessionReport, session: str, w: Window, why: str) -> None:
        """预筛挡掉：**要标记已抽**。

        不标记的话，每次结账都会重新扫一遍这个窗口，永远挡、永远扫，
        白白多一次切片开销；而且 pending_turns 会一直增长到 500 条上限。
        """
        self.store.mark_turns_extracted(session, w.end_idx)
        row = RunResult(session=session, start_idx=w.start_idx, end_idx=w.end_idx,
                        status="skipped", reason=why)
        self._record(row)
        rep.results.append(row)

    def _entity_tokens(self, w: Window, session: str) -> list[str]:
        """降级事件的关键词：用实体命中结果，而不是随便切词。

        这样即使用户三个月后搜"老王"，这条 raw 事件也能被捞出来，
        而不是只能靠 trigram 全表扫。
        """
        try:
            idx = EntityIndex.from_store(self.store, session)
        except Exception:  # noqa: BLE001
            return []
        found: list[str] = []
        for t in w.turns:
            if t.role != "user":
                continue
            hit = idx.match(t.content)
            if hit and hit[1] not in found:
                found.append(hit[1])
        return found

    def _record(self, row: RunResult) -> None:
        try:
            self.store.record_run(
                session=row.session, start_idx=row.start_idx, end_idx=row.end_idx,
                status=row.status,
                n_events=row.events, n_facts=row.facts, n_traits=row.traits,
                n_plans=row.plans, n_doubts=row.doubts,
                verbatim_fail=row.verbatim_fail, attempts=row.attempts,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                cached_tokens=row.cached_tokens,
                reasoning_tokens=row.reasoning_tokens,
                cost=row.cost, latency_ms=row.latency_ms,
                error=(row.error or row.reason or None),
            )
        except Exception as exc:  # noqa: BLE001
            # 记账失败不能影响抽取本身
            log.warning("run.record.failed", extra={"fields": {"err": repr(exc)}})


__all__ = ["Extractor", "RunResult", "SessionReport"]
