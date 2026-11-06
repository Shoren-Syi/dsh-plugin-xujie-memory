"""M14.2 每日重算验收。

重算是**批量改写全库**的操作，出错代价最高：一个符号写反，10 万条记忆
的层级当天全部错位，而且不会有任何报错——只会在三天后有人问"你怎么
忘了我昨天说的事"。

所以这里的测试偏向"守边界"而不是"验数值"：
幂等、试算不写、pinned 不许掉、archived 不许动、w0 不许被顺手改掉。
"""

from __future__ import annotations

import time

import pytest

from hippo.config import Config
from hippo.core.dynamics import recompute

DAY = 86400
S = "main"
CFG = Config()


def mk(store, *, w0=0.5, ingest=0, pinned=0, arousal=0.0, raw="一件事"):
    return store.add_event(
        session=S, raw=raw, summary=raw, keywords=["事"],
        event_time=ingest, ingest_time=ingest, w0=w0, weight=w0,
        pinned=pinned, arousal=arousal,
    )


def w_of(store, eid: int) -> float:
    return store.get_event(eid).weight


def t_of(store, eid: int) -> int:
    return store.get_event(eid).tier


# ------------------------------------------------------------------ 基本
def test_recompute_writes_weight_and_tier(store):
    eid = mk(store, w0=0.75, ingest=0)
    rep = recompute(store, 400 * DAY, cfg=CFG.weight)
    assert rep.total == 1
    assert w_of(store, eid) < 0.75
    assert 0.0 <= w_of(store, eid) <= 1.0


def test_recompute_changes_nothing_when_fresh(store):
    """刚写进来的事件不该被改。改了就说明 age 算错了（比如把 last_access=0 当真）。"""
    eid = mk(store, w0=0.55, ingest=10_000)
    before = w_of(store, eid)
    recompute(store, 10_000, cfg=CFG.weight)
    assert w_of(store, eid) == pytest.approx(before, abs=1e-6)


def test_dry_run_does_not_write(store):
    eid = mk(store, w0=0.75, ingest=0)
    before = w_of(store, eid)
    rep = recompute(store, 400 * DAY, cfg=CFG.weight, dry_run=True)
    assert rep.dry_run is True
    assert rep.total == 1
    assert w_of(store, eid) == before, "dry-run 写了库"


def test_idempotent(store):
    """★ 连跑两次结果一致。

    公式必须只读 w0/pinned/recall/last_access/ingest。一旦有人把 `weight`
    也放进公式（"权重高的更容易被再召回"这类想法），第二次结果就会不一样，
    而且不会有任何报错——库会每次重算都在缓慢漂移。
    """
    for i in range(5):
        mk(store, w0=0.4 + i * 0.1, ingest=0, raw=f"事{i}")
    recompute(store, 200 * DAY, cfg=CFG.weight)
    first = [(i, w_of(store, i), t_of(store, i))
             for i in store.event_ids_of_session(S)]
    rep2 = recompute(store, 200 * DAY, cfg=CFG.weight)
    second = [(i, w_of(store, i), t_of(store, i))
              for i in store.event_ids_of_session(S)]
    assert first == second
    assert rep2.changed == 0


def test_w0_is_never_touched(store):
    """w0 是写入时定的常量。重算改了它，衰减就变成"越算越快"。"""
    eid = mk(store, w0=0.63, ingest=0)
    for d in (10, 100, 1000):
        recompute(store, d * DAY, cfg=CFG.weight)
        assert store.get_event(eid).w0 == pytest.approx(0.63)


# ------------------------------------------------------------------ 分层
def test_pinned_stays_hot(store):
    """★ 钉住恒 hot，100% 。"""
    eid = mk(store, w0=0.5, pinned=1, ingest=0)
    for d in (0, 100, 3650):
        rep = recompute(store, d * DAY, cfg=CFG.weight)
        assert t_of(store, eid) == 1
        assert rep.pinned_bad == 0


def test_heavy_event_stays_above_cold(store):
    """DoD：30 天前的重要事件还能被检索到。"""
    eid = mk(store, w0=0.75, ingest=0)
    recompute(store, 30 * DAY, cfg=CFG.weight)
    assert t_of(store, eid) <= 2


def test_trivial_event_goes_cold(store):
    eid = mk(store, w0=0.20, ingest=0)
    recompute(store, 30 * DAY, cfg=CFG.weight)
    assert t_of(store, eid) == 3


def test_migration_matrix_counts_both_directions(store):
    """迁移矩阵要能看见"往下走"和"被召回后回升"两种流向。"""
    old_heavy = mk(store, w0=0.75, ingest=0)
    old_light = mk(store, w0=0.25, ingest=0)
    recompute(store, 500 * DAY, cfg=CFG.weight)
    assert t_of(store, old_heavy) == 3
    assert t_of(store, old_light) == 3

    # 老事件被重新想起 → 回到 warm/hot
    store.touch_events([old_heavy], when=500 * DAY)
    rep = recompute(store, 500 * DAY, cfg=CFG.weight)
    assert t_of(store, old_heavy) == 1
    assert rep.moves.get((3, 1), 0) >= 1, rep.moves


def test_report_dist_and_ratio(store):
    for w in (0.75, 0.55, 0.25):
        mk(store, w0=w, ingest=0, raw=f"w{w}")
    rep = recompute(store, 0, cfg=CFG.weight)
    assert rep.total == 3
    assert sum(rep.dist.values()) == 3
    assert 0.0 <= rep.hot_ratio <= 1.0


def test_hot_ratio_denominator_is_all_events(store):
    """hot 占比的分母是"参与重算的条数"，不是"库里的条数"。

    archived 的也被算进分母的话，归档越多 hot 占比越低，看着像"记忆全冷了"。
    """
    mk(store, w0=0.75, ingest=0)
    rep = recompute(store, 0, cfg=CFG.weight)
    assert rep.hot_ratio == pytest.approx(1.0)


# ------------------------------------------------------------------ 范围
def test_archived_events_are_skipped(store, conn):
    """归档 = "不再参与召回"。给它算权重没有意义，还会污染 hot 占比的分母。

    归档接口要到 M15 才建，这里直接改列——测的是重算的**筛选条件**，
    不是归档流程。
    """
    eid = mk(store, w0=0.75, ingest=0)
    conn.execute("UPDATE events SET archived = 1 WHERE id = ?", (eid,))
    rep = recompute(store, 500 * DAY, cfg=CFG.weight)
    assert rep.total == 0
    assert w_of(store, eid) == pytest.approx(0.75), "归档的权重被改了"


def test_session_filter(store):
    a = mk(store, w0=0.5, ingest=0, raw="甲")
    b = store.add_event(session="other", raw="乙", event_time=0, ingest_time=0,
                         w0=0.5, weight=0.5)
    rep = recompute(store, 100 * DAY, cfg=CFG.weight, session=S)
    assert rep.total == 1
    assert store.get_event(b).weight == pytest.approx(0.5), "别的会话被改了"


def test_empty_db_does_not_crash(store):
    rep = recompute(store, 0, cfg=CFG.weight)
    assert rep.total == 0
    assert rep.hot_ratio == 0.0


# ------------------------------------------------------------------ 性能
def test_20k_events_under_1s(store, conn):
    """手册要求 10 万条 <3s。这里量 2 万条 <1s，线性外推约 10 万条 5s 内。

    为什么不在测试里塞 10 万条：那要 20 秒建库，会让整套测试从 26 秒
    变成 46 秒，而结论一样。真正的 10 万条验证在 tools/bench_recompute。
    """
    base = 1_700_000_000
    rows = [(S, base + i, base, 2, 0.4 + (i % 50) / 100, 0.5, 0.1 + (i % 3) / 10,
             f"事{i}", f"事{i}", "[]", 0)
            for i in range(20_000)]
    with store.tx():
        conn.executemany(
            "INSERT INTO events(session, event_time, ingest_time, tier, w0,"
            " weight, arousal, summary, raw, entities, pinned)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    t0 = time.perf_counter()
    rep = recompute(store, base + 30 * DAY, cfg=CFG.weight)
    ms = (time.perf_counter() - t0) * 1000
    assert rep.total == 20_000
    assert ms < 1000, f"{ms:.0f}ms 太慢（手册要求 10 万条 <3s）"
