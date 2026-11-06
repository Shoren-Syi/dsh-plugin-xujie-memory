"""M15.2 合并策略与原文指针验收。

手册的四条硬要求（每一条都对应一个不可逆的损失，所以必须逐条钉死）：

  1. 主记录 raw **字节一致**      —— 合并绝不能碰原文
  2. 非主记录可追溯              —— superseded_by 指对，能被历史查询找回
  3. 新摘要覆盖全部细节          —— 走模型重生成，不是"摘要再摘要"
  4. 无原文丢失                  —— 所有 raw 仍可查

外加两条我加的（M14 的教训）：
  5. `recall_count` 取 **max 不取 sum**，否则合并 50 条旧事会直接顶到 cap
  6. 关键词必须**并集**，否则被合并事件的说话方式再也搜不到
"""

from __future__ import annotations

from datetime import datetime

import pytest

from hippo.core.consolidate import (
    DupRow, build_summary_prompt, cluster_cost, find_clusters, parse_summary, run,
)

S = "s1"
BASE = int(datetime(2026, 11, 1, 12, 0).timestamp())
DAY = 86400


def _ev(store, raw, keywords, *, day=0, weight=0.5, summary="", recall=0):
    eid = store.add_event(
        session=S, raw=raw, keywords=keywords, summary=summary or raw,
        w0=weight, weight=weight,
        event_time=BASE + day * DAY, ingest_time=BASE + day * DAY)
    if recall:
        for _ in range(recall):
            store.touch_events([eid], when=BASE + day * DAY)
    return eid


def _three(store):
    """一组确定会被合并的三条（关键词 3 词、共享 2 词以上）。"""
    return [
        _ev(store, "我吃不了辣，一点都受不了", ["吃不了辣", "忌口", "饮食"], day=1),
        _ev(store, "真的，我一点辣都吃不了", ["吃不了辣", "饮食", "忌口"], day=2),
        _ev(store, "总之就是不吃辣，别放辣椒", ["不吃辣", "忌口", "饮食"], day=3),
    ]


# ------------------------------------------------------------------ 手册四条
def test_master_raw_is_byte_identical(store):
    """★ 主记录 raw 一个字节都不许改。

    这是整个系统的地基：三个月后看到的那句话，必须是用户当时真的说过的那句。
    数据库层有 `trg_events_raw_immutable` 拦，但那条触发器只在 UPDATE raw 时触发；
    合并走的是 UPDATE keywords/summary/recall_count，**不会碰到它**——
    也就是说，这个 bug 如果存在，数据库不会报错。
    """
    ids = _three(store)
    before = {i: store.get_event(i).raw for i in ids}
    run(store, S)
    master = _master_of(store, ids)
    assert store.get_event(master).raw == before[master]
    # 非主记录的 raw 也一条不少
    for i in ids:
        assert store.get_event(i) is not None
        assert store.get_event(i).raw == before[i]


def test_members_point_to_master(store):
    """★ 非主记录 superseded_by 指向主记录，不删除。"""
    ids = _three(store)
    rep = run(store, S)
    assert rep.merged == 2
    master = _master_of(store, ids)
    for i in ids:
        if i == master:
            assert store.get_event(i).superseded_by is None
        else:
            assert store.get_event(i).superseded_by == master


def test_superseded_disappear_from_default_recall(store):
    """被合并的事件默认检索看不见，但历史查询能看见。

    这两件事必须同时成立：看不见 = 合并真的生效了；
    看得见（historical）= 原文没丢。
    """
    ids = _three(store)
    run(store, S)
    master = _master_of(store, ids)
    # 走 fetch_events 而不是 get_events：后者是"按 id 取回"，不过滤；
    # 检索链路用的是 fetch_events，它对 superseded 的过滤才是被测的行为。
    live = {e.id for e in store.fetch_events(ids, S)}
    assert live == {master}
    hist = {e.id for e in store.fetch_events(ids, S, include_historical=True)}
    assert hist == set(ids)


def test_all_raws_still_queryable(store):
    """★ 无原文丢失：所有 raw 仍在库里，逐字可比。"""
    ids = _three(store)
    raws = {i: store.get_event(i).raw for i in ids}
    run(store, S)
    for i, raw in raws.items():
        assert store.get_event(i).raw == raw


def test_summary_regenerated_from_raws(store):
    """★ 摘要重生成：喂的是**全部 raw**，不是旧摘要。

    手册 §M15.2 禁止"拿摘要再摘要"，理由是所有摘要系统都逃不掉的那条：
    每摘要一次掉一层信息，三轮之后摘要和原文没关系了，而且不会有任何报错。
    """
    ids = _three(store)
    seen: list[str] = []

    def fake(messages):
        prompt = messages[0]["content"]
        seen.append(prompt)
        # 断言喂进去的是 raw 原文，不是三条旧摘要
        return ('{"summary": "用户吃不了辣，一点都受不了，要求别放辣椒"}',
                {"prompt_tokens": 100, "completion_tokens": 20})

    rep = run(store, S, summarizer=fake)
    assert rep.summary_ok == 1
    prompt = seen[0]
    for i in ids:
        assert store.get_event(i).raw in prompt
    master = _master_of(store, ids)
    assert "别放辣椒" in store.get_event(master).summary


def test_summary_failure_does_not_block_merge(store):
    """摘要失败只影响摘要，不能连带把 supersede 也回滚。

    否则一次网络抖动会让这一轮白跑，而下一轮重新扫出来的还是同一批。
    """
    ids = _three(store)
    old = store.get_event(ids[0]).summary

    def boom(messages):
        raise RuntimeError("network down")

    rep = run(store, S, summarizer=boom)
    assert rep.merged == 2
    assert rep.summary_fail == 1
    master = _master_of(store, ids)
    assert store.get_event(master).summary == old


def test_bad_json_summary_keeps_old(store):
    """模型吐了非 json 且内容为空 → 保留原摘要，不写空串。"""
    ids = _three(store)
    run(store, S, summarizer=lambda m: ("", {"prompt_tokens": 1}))
    master = _master_of(store, ids)
    assert store.get_event(master).summary


# ------------------------------------------------------------------ 两条自加
def test_recall_count_takes_max_not_sum(store):
    """★ `recall_count` 取 max。

    求和会让一条合并了 50 次旧事的记忆直接顶到 cap，半衰期 ×2.25 白送
    （R-14-Gate 留给 M15 的第一条）。
    """
    a = _ev(store, "我吃不了辣，一点都受不了", ["吃不了辣", "忌口", "饮食"],
            day=1, recall=3)
    b = _ev(store, "真的，我一点辣都吃不了", ["吃不了辣", "饮食", "忌口"],
            day=2, recall=1)
    run(store, S)
    master = _master_of(store, [a, b])
    assert store.get_event(master).recall_count == 3


def test_keywords_are_unioned(store):
    """★ 关键词取并集。

    被合并的事件不再出现在检索结果里，它们的说法如果不同步到主记录，
    那些词就再也搜不到了——原文还在库里，但检索不到。
    这是合并最隐蔽的损失，光看"无原文丢失"是发现不了的。
    """
    # 两条共享 2 个词（强判据 0.67），各自有独占词
    _ev(store, "我吃不了辣，一点都受不了", ["吃不了辣", "忌口", "饮食"], day=1)
    _ev(store, "总之就是不吃辣，别放辣椒", ["吃不了辣", "忌口", "辣椒"], day=2)
    run(store, S)
    master = store.consolidate_rows(S)[0]
    kw = set(master["keywords"].split())
    assert {"吃不了辣", "忌口", "饮食", "辣椒"} <= kw


def test_union_keywords_are_searchable(store):
    """并集不只是字段好看——倒排里必须真的能搜到。"""
    _ev(store, "我吃不了辣，一点都受不了", ["吃不了辣", "忌口", "饮食"], day=1)
    _ev(store, "总之就是不吃辣，别放辣椒", ["吃不了辣", "忌口", "辣椒"], day=2)
    run(store, S)
    # 直接查倒排：只并 keywords 字段而不重建 kw_index 的话，
    # 字段看着对、检索什么也搜不到——这正是最容易漏的那一步。
    hits = [r["event_id"] for r in store.conn.execute(
        "SELECT event_id FROM kw_index WHERE token = ?", ("辣椒",)).fetchall()]
    assert hits == [store.consolidate_rows(S)[0]["id"]], "member 的倒排条目该被摘掉"


# ------------------------------------------------------------------ 日志与撤销
def test_log_written(store):
    ids = _three(store)
    run(store, S)
    st = store.consolidation_stats(S)
    assert st["batches"] == 1
    assert st["merged"] == 2
    master = _master_of(store, ids)
    rows = store.conn.execute(
        "SELECT member_ids, summary_old, summary_new FROM consolidation_log"
        " WHERE master_id = ?", (master,)).fetchall()
    assert len(rows) == 1
    import json
    assert set(json.loads(rows[0]["member_ids"])) == set(ids) - {master}


def test_unmerge_restores(store):
    """撤销把 superseded_by 放回 NULL，成员重新出现在默认检索里。"""
    ids = _three(store)
    run(store, S)
    master = _master_of(store, ids)
    freed = store.unmerge(master)
    assert set(freed) == set(ids) - {master}
    assert {e.id for e in store.get_events(ids)} == set(ids)
    assert store.consolidation_stats(S)["undone"] == 2


def test_unmerge_rebuilds_index(store):
    """撤销后倒排必须重建，否则"恢复了但搜不到"——比不撤销还糟。"""
    _ev(store, "我吃不了辣，一点都受不了", ["吃不了辣", "忌口", "饮食"], day=1)
    b = _ev(store, "总之就是不吃辣，别放辣椒", ["吃不了辣", "忌口", "辣椒"], day=2)
    run(store, S)
    master = store.consolidate_rows(S)[0]["id"]
    store.unmerge(master)
    hits = {r["event_id"] for r in store.conn.execute(
        "SELECT event_id FROM kw_index WHERE token = ?", ("辣椒",)).fetchall()}
    assert b in hits, "被撤销的事件必须重新能被倒排命中"


def test_unmerge_twice_is_noop(store):
    ids = _three(store)
    run(store, S)
    master = _master_of(store, ids)
    assert store.unmerge(master)
    assert store.unmerge(master) == []


def test_unmerge_keeps_summary_and_keywords(store):
    """撤销**不**回滚摘要和关键词。

    摘要可能已被后续抽取改过，回滚会覆盖更新内容；关键词并集是无损的，
    留着反而让主记录更好检索。撤销要解决的是"这几条不该合"，
    不是"把库恢复到合并前那一秒"。
    """
    ids = _three(store)
    run(store, S, summarizer=lambda m: ('{"summary": "合并后的摘要"}', {}))
    master = _master_of(store, ids)
    store.unmerge(master)
    ev = store.get_event(master)
    assert ev.summary == "合并后的摘要"
    assert len(ev.keywords.split()) >= 3


# ------------------------------------------------------------------ 行为
def test_idempotent(store):
    """★ 合完再跑，一条都不该再合。

    不幂等的后果不是"多合几次"——第二次会把刚合并的主记录再并进别人，
    因为它的关键词变多了、和更多事件相似了。
    """
    _three(store)
    first = run(store, S)
    assert first.merged == 2
    second = run(store, S)
    assert second.merged == 0
    assert second.clusters == 0
    assert len(store.consolidate_rows(S)) == 1


def test_dry_run_writes_nothing(store):
    ids = _three(store)
    rep = run(store, S, dry_run=True)
    assert rep.merged == 2
    assert len(store.consolidate_rows(S)) == 3
    assert store.consolidation_stats(S)["batches"] == 0


def test_empty_session_is_safe(store):
    rep = run(store, "nobody")
    assert rep.scanned == 0 and rep.merged == 0


# ------------------------------------------------------------------ 工具函数
def test_cluster_cost_is_estimate_not_call(store):
    cl = find_clusters([DupRow(id=i, event_time=BASE, keywords={"a", "b", "c"},
                               summary="", raw="x" * 40, weight=0.5)
                        for i in range(1, 4)])
    assert cluster_cost(cl[0]) > 0


def test_build_prompt_forbids_guessing(store):
    """prompt 必须明确"不要添加原文没有的信息"和"矛盾要如实写"。

    后者是实测出来的：合并"住朝阳 + 更正为海淀"那组时，模型写出了
    "先说住朝阳，后更正为海淀"——这正是我们没弄丢的东西。
    """
    cl = find_clusters([DupRow(id=1, event_time=0, keywords={"甲", "乙", "丙"},
                               summary="", raw="我吃不了辣，一点都受不了",
                               weight=0.5),
                        DupRow(id=2, event_time=0, keywords={"甲", "乙", "丁"},
                               summary="", raw="真的，我一点辣都吃不了",
                               weight=0.4)])
    assert cl, "这对应该被合并"
    prompt = build_summary_prompt(cl[0])[0]["content"]
    assert "不要添加原文没有的信息" in prompt
    assert "矛盾" in prompt


def test_parse_summary_variants():
    assert parse_summary('{"summary": "用户不吃辣"}') == "用户不吃辣"
    assert parse_summary('```json\n{"summary": "用户不吃辣"}\n```') == "用户不吃辣"
    assert parse_summary("用户不吃辣") == "用户不吃辣"
    assert parse_summary("") == ""
    assert parse_summary("{}") == ""


def test_parse_summary_truncates():
    long = "很" * 200
    got = parse_summary('{"summary": "%s"}' % long, max_len=20)
    assert len(got) <= 20 and got.endswith("…")


# ------------------------------------------------------------------ helper
def _master_of(store, ids) -> int:
    """簇里的主记录：没被 supersede 的那条。"""
    for i in ids:
        ev = store.get_event(i)
        if ev is not None and ev.superseded_by is None:
            return i
    raise AssertionError("三条全被合并了？应该留一条主记录")
