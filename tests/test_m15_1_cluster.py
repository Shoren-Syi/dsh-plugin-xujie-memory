"""M15.1 近似聚类验收。

这里的每一条测试都对应 `tools/probe_dup.py` 量出来的一个具体失败模式，
不是"照着需求写个 happy path"。写明对应关系，是为了以后有人想改阈值时
能看见"改这个数会让哪条测试红"。

    test_identical_raw_always_merges        ← 同一句话写了两遍（最强证据）
    test_full_containment_merges            ← 关键词完全包含
    test_small_sample_shares_one_word_*     ← 2 词共享 1 个：强判据不过、弱判据过
    test_correction_is_never_merged         ← "不对，我说错了"
    test_shared_generic_word_not_merged     ← "吵""加班"这类泛词
    test_outside_time_window_not_paired     ← 30 天窗
    test_non_event_kind_excluded            ← fact/plan 不参与
"""

from __future__ import annotations

from datetime import datetime

import pytest

from hippo.core.consolidate import (
    CORRECTION, DupRow, candidate_pairs, cov_strict, find_clusters, kw_cov,
)

S = "s1"
BASE = int(datetime(2026, 11, 1, 12, 0).timestamp())
DAY = 86400


def _ev(store, raw, keywords, *, day=0, weight=0.5, kind="event", summary=""):
    return store.add_event(
        session=S, raw=raw,
        keywords=keywords,
        summary=summary or raw,
        kind=kind, w0=weight, weight=weight,
        event_time=BASE + day * DAY, ingest_time=BASE + day * DAY)


def _clusters(store, **kw):
    rows = [DupRow.of(d) for d in store.consolidate_rows(S)]
    return find_clusters(rows, **kw)


def _sizes(clusters):
    return sorted((c.size for c in clusters), reverse=True)


# ------------------------------------------------------------------ 强判据
def test_identical_raw_always_merges(store):
    """原文逐字相同 → 无条件合并。

    第一版 `cov_strict` 没有这一条，于是"总之就是不吃辣，别放辣椒"写了
    两遍、但关键词抽成 ['不吃辣','提醒'] 和 ['不吃辣','辣椒'] 时，
    m=2 共享 1 个 → 被小样本规则挡掉了。**同一句话都合不了，就没有什么
    能合的了**，所以逐字相同必须是最高优先级。
    """
    a = _ev(store, "总之就是不吃辣，别放辣椒", ["不吃辣", "提醒"], day=1)
    b = _ev(store, "总之就是不吃辣，别放辣椒", ["不吃辣", "辣椒"], day=2)
    cl = _clusters(store)
    assert len(cl) == 1 and cl[0].size == 2
    assert {m.id for m in cl[0].members} == {a, b}


def test_full_containment_merges(store):
    """关键词一边完全包含另一边，且都 ≥3 词 → 强判据。"""
    _ev(store, "我老婆是护士，上夜班比较多", ["老婆", "护士", "夜班"], day=1)
    _ev(store, "她这周又连着三个夜班", ["护士", "夜班", "这周"], day=2)
    cl = _clusters(store)
    assert _sizes(cl) == [2]
    assert cl[0].strong is True


def test_small_sample_shares_one_word_not_strong(store):
    """2 词共享 1 个 → 强判据给 0，弱判据给 1.0。

    这是实测出来的分水岭：抽取器只给 2 个关键词时，共享 1 个就是 0.5，
    落在任何合理阈值上都会过线，而**此时共享 1 个词根本不是证据**。
    """
    a = DupRow(id=1, event_time=0, keywords={"复查", "医院"}, summary="", raw="周五去医院复查", weight=0.5)
    b = DupRow(id=2, event_time=0, keywords={"复查", "甲状腺"}, summary="", raw="去查甲状腺", weight=0.5)
    assert kw_cov(a, b) == 0.5
    assert cov_strict(a, b) == 0.0


def test_small_sample_full_match_is_strong(store):
    """2 词且完全一致 → 强判据给 1.0。"""
    a = DupRow(id=1, event_time=0, keywords={"护士", "夜班"}, summary="", raw="x", weight=0.5)
    b = DupRow(id=2, event_time=0, keywords={"护士", "夜班"}, summary="", raw="y", weight=0.5)
    assert cov_strict(a, b) == 1.0


def test_three_words_share_one_is_weak_band(store):
    """3 词共享 1 个：强判据 0.33 不过，弱判据也不过（弱阈值 0.5）。"""
    a = DupRow(id=1, event_time=0, keywords={"a1", "a2", "a3"}, summary="", raw="x", weight=0.5)
    b = DupRow(id=2, event_time=0, keywords={"a1", "b2", "b3"}, summary="", raw="y", weight=0.5)
    assert kw_cov(a, b) == pytest.approx(1 / 3)
    assert cov_strict(a, b) == pytest.approx(1 / 3)


# ------------------------------------------------------------------ 刹车
def test_correction_is_never_merged(store):
    """含更正标记的事件不参与合并。

    它的价值就在"改过"这个动作本身。合并掉等于把变化抹平：
    库里只剩"住海淀"，再没人知道用户说过"住朝阳"然后改口。
    真机库实测这条挡住了 (31,32) 这对。
    """
    _ev(store, "我住在朝阳", ["朝阳", "居住", "住址"], day=1)
    _ev(store, "不对，我说错了，是海淀", ["海淀", "居住", "更正"], day=1)
    assert _clusters(store) == []


def test_correction_pattern_matches_real_phrasings(store):
    for raw in ("不对，我说错了", "其实是海淀", "更正一下", "我记错了",
                "不是这样", "我搞错了", "改主意了", "反悔了",
                # 「算了」是改变主意的标记，收进来挡的是
                # "我现在就想辞职…算了，先忍着吧" 这种状态变化
                "算了，先忍着吧"):
        assert CORRECTION.search(raw), raw
    # 不带"算了"的纯陈述不该被误伤
    for raw in ("我住在朝阳", "先忍着吧", "我吃不了辣"):
        assert not CORRECTION.search(raw), raw


def test_shared_generic_word_not_merged(store):
    """"微信群里吵起来" vs "搬工位吵了一下午"：2 词共享 1 个泛词「吵」。

    这两件事没关系，但词面形状和"同一件事的两种说法"完全一样。
    **没有任何词面信号能区分**，所以强判据一律不给分（宁可漏合）。
    """
    _ev(store, "微信群里又吵起来了", ["吵", "微信群"], day=1)
    _ev(store, "隔壁组搬工位，吵了一下午", ["吵", "搬工位"], day=1)
    cl = _clusters(store)
    # 强判据 0 簇；弱判据会合（这是设计上接受的代价，CLI 里标 weak）
    assert all(not c.strong for c in cl)
    assert _sizes(cl) in ([], [2])


def test_weak_cluster_is_marked_not_strong(store):
    """"吵"这一对如果被合，必须标成 weak，好让 `unmerge` 能撤销。"""
    _ev(store, "微信群里又吵起来了", ["吵", "微信群"], day=1)
    _ev(store, "隔壁组搬工位，吵了一下午", ["吵", "搬工位"], day=1)
    for c in _clusters(store):
        assert c.strong is False


# ------------------------------------------------------------------ 筛选
def test_outside_time_window_not_paired(store):
    """30 天窗。

    手册写 7 天，实测不够：模拟数据里 53% 的"同一件事"两次提到隔了 7 天以上
    （"我不吃辣"这种事会跨整月反复讲）。
    """
    _ev(store, "我老婆是护士，上夜班", ["老婆", "护士", "夜班"], day=1)
    _ev(store, "她这周连着三个夜班", ["护士", "夜班", "这周"], day=40)
    assert _clusters(store) == []


def test_within_window_is_paired(store):
    _ev(store, "我老婆是护士，上夜班", ["老婆", "护士", "夜班"], day=1)
    _ev(store, "她这周连着三个夜班", ["护士", "夜班", "这周"], day=29)
    assert _sizes(_clusters(store)) == [2]


def test_non_event_kind_excluded(store):
    """fact / plan 不参与。两条 plan 措辞一样也不是同一件事。"""
    _ev(store, "周三交季度报告", ["报告", "周三", "季度"], day=1, kind="plan")
    _ev(store, "周三交季度报告", ["报告", "周三", "季度"], day=2, kind="plan")
    assert _clusters(store) == []


def test_already_superseded_excluded(store):
    """已经并进别人的事件不能再当主记录，否则会出现 A→B→C 的链。"""
    a = _ev(store, "我吃不了辣", ["吃不了辣", "忌口", "饮食"], day=1)
    b = _ev(store, "我一点辣都吃不了", ["吃不了辣", "忌口", "饮食"], day=2)
    store.supersede_event(a, b)
    assert _clusters(store) == []


def test_single_event_no_cluster(store):
    _ev(store, "今天吃了碗牛肉面", ["牛肉面", "午饭"])
    assert _clusters(store) == []


# ------------------------------------------------------------------ 结构
def test_master_is_highest_weight(store):
    """主记录 = weight 最高，平手取 event_time 最早。"""
    _ev(store, "我吃不了辣，一点都受不了", ["吃不了辣", "忌口", "饮食"],
        day=1, weight=0.45)
    hi = _ev(store, "真的，我一点辣都吃不了", ["吃不了辣", "饮食", "忌口"],
             day=3, weight=0.7)
    cl = _clusters(store)
    assert cl[0].master.id == hi


def test_master_tie_breaks_on_earliest_time(store):
    _ev(store, "我吃不了辣，一点都受不了", ["吃不了辣", "忌口", "饮食"],
        day=5, weight=0.5)
    early = _ev(store, "真的，我一点辣都吃不了", ["吃不了辣", "饮食", "忌口"],
                day=2, weight=0.5)
    assert _clusters(store)[0].master.id == early


def test_cluster_size_capped(store):
    """单簇上限 6。超过说明在链式蔓延（single-linkage 的经典病症）。"""
    for i in range(10):
        _ev(store, f"搬家记录第{i}条，从朝阳搬到海淀", ["搬家", "朝阳", "海淀"],
            day=i)
    cl = _clusters(store)
    assert cl and all(c.size <= 6 for c in cl)


def test_deterministic(store):
    """同数据同结果。并查集的最终形状不该受 set 遍历顺序影响。"""
    for i in range(6):
        _ev(store, f"搬家记录{i}", ["搬家", "朝阳", "海淀"], day=i % 3)
    first = [sorted(m.id for m in c.members) for c in _clusters(store)]
    for _ in range(5):
        again = [sorted(m.id for m in c.members) for c in _clusters(store)]
        assert again == first


def test_candidate_pairs_uses_inverted_index(store):
    """候选对必须筛掉绝大部分——1 万条全量是 5000 万对。"""
    for i in range(60):
        _ev(store, f"琐事{i}", [f"词{i}", f"另{i}"], day=i % 20)
    rows = [DupRow.of(d) for d in store.consolidate_rows(S)]
    pairs = candidate_pairs(rows)
    total = len(rows) * (len(rows) - 1) // 2
    assert len(pairs) == 0
    assert total > 1000
