"""M18.1 存疑产生：过滤 / 合并 / 上限（手册验收表 4 条）。

夹具设计说明（10 条候选 → 丢 5 条 = 50%，落在手册 40%~60% 区间）：
    2 条 impact<0.4、2 条 <8 字、1 条与库内已有相似、5 条正常。
每条丢因都有独立断言，不是只数总数——只数总数的话，
"长度过滤"和"impact 过滤"写反了测试照样绿。
"""

from __future__ import annotations

from hippo.core.doubt import (MERGE_SIMILARITY, MIN_IMPACT, MIN_QUESTION_LEN,
                              MergedInto, filter_doubts, trigram_similarity)
from hippo.core.schema import DoubtOut


# ---------------------------------------------------------- 相似度
def test_trigram_identical_and_unrelated():
    assert trigram_similarity("用户住在上海", "用户住在上海") == 1.0
    # 完全无共享字符 → 0
    assert trigram_similarity("abcdef", "uvwxyz") == 0.0


def test_trigram_symmetric_and_bounded():
    a, b = "用户的老家在哪里", "用户的老家是成都"
    s1 = trigram_similarity(a, b)
    assert 0.0 < s1 < 1.0
    assert s1 == trigram_similarity(b, a)          # 对称
    assert trigram_similarity("", "用户") == 0.0   # 空串安全


# ---------------------------------------------------------- 基础过滤
def test_low_impact_dropped():
    """impact < 0.4：不影响理解的不记。"""
    kept, st = filter_doubts([DoubtOut(question="用户到底养的是哪种猫品种？",
                                       impact=0.39)])
    assert kept == []
    assert st.dropped_low_impact == 1


def test_vague_question_dropped():
    """少于 8 字：太含糊，问了也没用。"""
    kept, st = filter_doubts([DoubtOut(question="住哪里？",
                                       impact=0.9)])
    assert kept == []
    assert st.dropped_vague == 1


def test_boundary_values_kept():
    """边界值恰好在线上的要保留：0.4 和 8 字都是"含"下界。"""
    kept, st = filter_doubts([
        DoubtOut(question="用户的猫是什么品种", impact=MIN_IMPACT),   # 恰 8 字 + 0.4
        DoubtOut(question="用户的驾照是哪种类型", impact=0.41),       # 不相似，不触发合并
    ])
    assert st.dropped == 0 and len(kept) == 2


# ---------------------------------------------------------- 合并
def test_merged_into_existing(store):
    """与库内已有 doubt 相似 >0.8 → 合并不新增，impact 只升不降。"""
    store.add_doubt(session="s", question="用户的猫叫什么名字",
                    impact=0.5)
    existing = store.active_doubts("s")
    kept, st = filter_doubts(
        [DoubtOut(question="用户的猫叫什么名字呀", impact=0.7)], existing)
    assert kept == []
    assert len(st.merged_into_existing) == 1
    m = st.merged_into_existing[0]
    assert isinstance(m, MergedInto) and m.impact == 0.7
    store.bump_doubt_impact(m.doubt_id, m.impact)
    assert store.active_doubts("s")[0].impact == 0.7


def test_bump_never_lowers(store):
    """新候选 impact 更低时不覆盖——已有条目是被时间检验过的。"""
    did = store.add_doubt(session="s", question="用户的老家在哪个城市",
                          impact=0.8)
    store.bump_doubt_impact(did, 0.3)
    assert store.active_doubts("s")[0].impact == 0.8


def test_duplicates_within_batch(store):
    """一次抽取里同一件事两种问法 → 只留 impact 高的。

    句对是实算过的：「…呀」与「…呀呢」相似度 0.889 > 0.8（曾手算
    另一对为"必然相似"，实算 0.15——夹具里的相似度一律实算为准）。
    """
    kept, st = filter_doubts([
        DoubtOut(question="用户的猫是什么品种呀", impact=0.6),
        DoubtOut(question="用户的猫是什么品种呀呢", impact=0.8),
    ])
    assert st.dropped_duplicate == 1 and len(kept) == 1
    assert kept[0].impact == 0.8


# ---------------------------------------------------------- 上限与过期
def test_cap_twenty_prunes_lowest_impact(store):
    """第 21 条起，impact 最低的被淘汰（手册：上限生效）。"""
    for i in range(20):
        store.add_doubt(session="s", question=f"用户的习惯{i}是哪一种呢",
                        impact=0.5 + i * 0.01)
    store.add_doubt(session="s", question="用户的秘密爱好是啥呢呀",
                    impact=0.0)             # 最低，该被淘汰
    assert store.counts()["doubts"] == 21
    store.prune_doubts("s", keep=20)
    rows = store.active_doubts("s", limit=50)
    assert len(rows) == 20
    assert all(d.question != "用户的秘密爱好是啥呢呀" for d in rows)


def test_expire_days_default_seven(store):
    """expires = now + 7d（手册：过期时间正确）。"""
    import time as _t
    before = int(_t.time())
    store.add_doubt(session="s", question="用户的驾照是哪种类型的呢",
                    expire_days=7)
    d = store.active_doubts("s")[0]
    assert d.created >= before - 1
    assert d.expires - d.created == 7 * 86400


# ---------------------------------------------------------- 端到端 + 丢弃率
def _ten_candidates() -> list[dict]:
    """10 条候选：丢 5（2 低 impact + 2 含糊 + 1 重复）→ 丢弃率 50%。

    相似度均已实算：第 1/10 条 0.889 > 0.8；其余五条正常候选两两
    最高 0.067，不会误合并。
    """
    return [
        {"question": "用户的猫是什么品种呀", "impact": 0.7},      # ✓
        {"question": "用户在哪一家公司上班呢", "impact": 0.8},    # ✓
        {"question": "用户说的老王是谁", "impact": 0.6},          # ✓
        {"question": "用户的生日是几月几号", "impact": 0.75},     # ✓
        {"question": "用户养猫养了几年了", "impact": 0.5},        # ✓
        {"question": "用户昨天午饭吃了啥", "impact": 0.3},        # ✗ impact
        {"question": "用户手机是什么牌子", "impact": 0.2},        # ✗ impact
        {"question": "哪呢？", "impact": 0.9},                    # ✗ 含糊
        {"question": "啥呀？", "impact": 0.9},                    # ✗ 含糊
        {"question": "用户的猫是什么品种呀呢", "impact": 0.9},    # ✗ 与第 1 条重复
    ]


def test_drop_rate_between_40_and_60(store):
    """验收 #1：丢弃率 40%~60%。夹具 10 条丢 5 = 50%。"""
    store.add_doubt(session="s", question="用户的猫叫什么名字", impact=0.5)
    cands = [DoubtOut(**c) for c in _ten_candidates()]
    kept, st = filter_doubts(cands, store.active_doubts("s"))
    assert st.total == 10
    assert 0.40 <= st.drop_rate <= 0.60, f"drop_rate={st.drop_rate:.2f}"
    assert len(kept) == 5
    assert st.dropped_low_impact == 2
    assert st.dropped_vague == 2
    assert st.dropped_duplicate == 1


def test_persist_end_to_end_accounts(store):
    """persist 端到端：doubts_seen / doubts_filtered / doubts 三本账对得上。"""
    from hippo.core.models import Turn
    from hippo.core.persist import persist_result
    from hippo.core.schema import ExtractResult
    from hippo.core.turns import Window

    turns = [Turn(session="s", idx=0, role="user",
                  content="我跟你说说我家猫的事", ts=1_700_000_000)]
    w = Window(session="s", idxs=[0], token_est=100,
               has_pending=False, turns=turns)
    r = ExtractResult(
        events=[{"raw": "我跟你说说我家猫的事", "keywords": ["猫"]}],
        doubts=_ten_candidates(),
    )
    st = persist_result(store, "s", w, r)
    assert st.doubts_seen == 10
    assert st.doubts_filtered == 5
    assert st.doubts == 5
    assert len(store.active_doubts("s", limit=50)) == 5


def test_thresholds_match_manual():
    """三个判据常量与手册一致（改常量 = 改语义，测试守住）。"""
    assert MIN_IMPACT == 0.4
    assert MIN_QUESTION_LEN == 8
    assert MERGE_SIMILARITY == 0.8
