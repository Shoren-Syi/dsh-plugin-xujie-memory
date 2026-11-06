"""M17.2 验收：三戳互不污染。

三个时间戳各管一件事，混用不会报错，只会让整条链路悄悄错向：

    event_time   这件事**发生在**什么时候   → 只由 parse_time 决定
    ingest_time  我**什么时候知道的**       → 只由写入时刻决定
    last_access  我**上一次想起**它          → 只由检索命中决定

最容易混的一对是 event_time 和 last_access：都叫"时间"，都在 events 表里，
都能拿来算 age。但"讲了一件 2019 年的事"和"这条记忆是 2019 年存的"是
完全相反的两件事——按前者算衰减，用户今天刚说的话会立刻沉底。
"""

from __future__ import annotations

import time

import pytest

from hippo.core.dynamics import recompute
from hippo.core.store import Store
from hippo.core.weight import compute_weight


def row(store: Store, eid: int) -> dict:
    r = store.conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    return dict(r)


# ---------------------------------------------------------------- 写入
def test_write_sets_event_and_ingest_independently(store):
    """event_time 来自那句话，ingest_time 来自现在。两者可以差十年。"""
    old = int(time.time()) - 6 * 365 * 86400
    now = int(time.time())
    eid = store.add_event(session="s", raw="2019 年我在成都工作", summary="成都工作",
                          keywords="成都 工作", event_time=old, ingest_time=now)
    r = row(store, eid)
    assert r["event_time"] == old
    assert r["ingest_time"] == now
    assert abs(r["event_time"] - r["ingest_time"]) > 5 * 365 * 86400


def test_last_access_is_null_until_first_recall(store):
    """刚写进来的记忆，谁都没想起过。NULL 是"从没被检索到"的准确表达，
    拿 0 或 ingest_time 顶替都会让"这条从没被用过"这件事查不出来。"""
    eid = store.add_event(session="s", raw="我养了只猫叫年糕", summary="养猫",
                          keywords="猫 年糕")
    assert row(store, eid)["last_access"] is None


def test_event_time_defaults_to_ingest_when_absent(store):
    """没有时间线索时，事件时间 = 对话发生的时间。

    这是唯一的"允许相同"的情况：用户没说什么时候，我们只有"现在"。
    """
    eid = store.add_event(session="s", raw="我不吃香菜", summary="不吃香菜",
                          keywords="香菜")
    r = row(store, eid)
    assert r["event_time"] == r["ingest_time"]
    assert r["last_access"] is None


# ---------------------------------------------------------------- 检索
def test_touch_updates_only_last_access(store):
    """touch 只碰 last_access 和 recall_count，一个字节都不许动另外两个。"""
    old = int(time.time()) - 3 * 365 * 86400
    eid = store.add_event(session="s", raw="三年前搬过家", summary="搬家",
                          keywords="搬家", event_time=old)
    before = row(store, eid)
    later = int(time.time())
    store.touch_events([eid], when=later)
    after = row(store, eid)

    assert after["last_access"] == later
    assert after["recall_count"] == before["recall_count"] + 1
    assert after["event_time"] == before["event_time"]
    assert after["ingest_time"] == before["ingest_time"]


def test_repeated_touch_never_moves_event_time(store):
    """反复想起同一件事，event_time 必须纹丝不动。

    如果实现图省事写成 `UPDATE ... SET last_access=?, event_time=?`，
    一条 2019 年的事被想起 10 次之后就会变成"今天发生的事"——
    时间线上彻底失真，而且没法恢复（原文没写日期）。
    """
    old = int(time.time()) - 7 * 365 * 86400
    eid = store.add_event(session="s", raw="七年前的事", summary="旧事",
                          keywords="旧事", event_time=old)
    base = int(time.time())
    for i in range(10):
        store.touch_events([eid], when=base + i * 3600)
    assert row(store, eid)["event_time"] == old


# ---------------------------------------------------------------- 衰减
def test_old_event_written_today_does_not_sink(store):
    """手册 M17.2 验收 #2：event_time=2019 但刚写入，权重 ≥0.6。

    这是三戳分离的**全部理由**。用户今天第一次说起"2019 年我在成都"，
    这条记忆对当下的价值是最高的（刚建立的事实），不是最低的（七年前的旧闻）。
    按 event_time 算 age，它一进来就该老死——这就是混淆两个戳的代价。
    """
    now = int(time.time())
    old = now - 7 * 365 * 86400
    eid = store.add_event(session="s", raw="2019 年我在成都做后端", summary="成都工作",
                          keywords="成都 后端", event_time=old, ingest_time=now,
                          w0=0.8, weight=0.8)
    e = store.get_event(eid)
    w = compute_weight(e, now)
    assert w >= 0.6, f"刚写入的老事件只剩 {w:.2f}"


def test_decay_ignores_event_time_entirely(store):
    """把 event_time 改成 1990 年，权重一点不变。

    这是比"≥0.6"更强的断言：它不是"影响很小"，是**完全没有影响**。
    """
    now = int(time.time())
    a = store.add_event(session="s", raw="我住在北京", summary="住北京",
                        keywords="北京", event_time=now - 365 * 86400,
                        ingest_time=now, w0=0.8)
    b = store.add_event(session="s", raw="我住在上海", summary="住上海",
                        keywords="上海", event_time=now - 30 * 365 * 86400,
                        ingest_time=now, w0=0.8)
    wa = compute_weight(store.get_event(a), now)
    wb = compute_weight(store.get_event(b), now)
    assert wa == pytest.approx(wb)


def test_recompute_does_not_write_time_columns(store):
    """recompute 只改 weight/tier，不许动任何时间列。

    夜里跑一次全量重算，如果顺手把 last_access 也"刷新"一遍，
    所有记忆都会变成"刚被想起"——衰减机制直接失效，而且是静默的。
    """
    old = int(time.time()) - 2 * 365 * 86400
    eid = store.add_event(session="s", raw="两年前换过工作", summary="换工作",
                          keywords="换工作", event_time=old)
    before = row(store, eid)
    recompute(store, now=int(time.time()))
    after = row(store, eid)
    for col in ("event_time", "event_time_end", "ingest_time", "last_access"):
        assert after[col] == before[col], f"recompute 动了 {col}"


# ---------------------------------------------------------------- 端到端
def test_persist_keeps_hint_time_and_now_time_apart(store):
    """走完整写入链路：hint 说"两年前"，两个戳必须分开。

    这条防的是 persist 层把 span.start 顺手也赋给 ingest_time——
    单测 add_event 是测不出来的，因为那时两个值由调用方分别传入。
    """
    from hippo.core.persist import persist_result
    from hippo.core.schema import EventOut, ExtractResult
    from hippo.core.turns import Turn, Window

    now = int(time.time())
    w = Window(session="s", idxs=[0], token_est=20, has_pending=False, turns=[
        Turn(session="s", idx=0, role="user", content="两年前我从前端转后端了", ts=now),
    ])
    ev = EventOut(raw="两年前我从前端转后端了", summary="两年前从前端转后端",
                  keywords=["前端", "后端"], event_time_hint="两年前",
                  arousal=0.5, source="user")
    res = ExtractResult(events=[ev], facts=[], traits=[], plans=[], doubts=[])

    st = persist_result(store, "s", w, res, now=now)
    assert st.event_ids
    r = row(store, st.event_ids[0])
    assert r["ingest_time"] == now
    # 两年前 ≈ 2024 年，与 now 至少差 600 天
    assert now - r["event_time"] > 600 * 86400
    assert r["last_access"] is None
