"""M18.3 过期与遗留：过期转记忆 / resolved 闭环 / 不重复转（验收 4 条）。

"不能凭空消失"是这一节的存在理由：问不出来的疑问要变成
"我忘了"的体感来源，答案来了的要体面关闭。
"""

from __future__ import annotations

from hippo.core.doubt import expire_to_memory, resolve_matching_doubts


def _add_expired(store, question="用户的老家在哪个城市", *,
                 days_ago=8, resolved=0, **kw):
    """造一条 N 天前创建的 doubt（默认已过期：7 天保质期）。"""
    import time as _t
    now = int(_t.time())
    did = store.add_doubt(session="s", question=question, **kw)
    store.conn.execute(
        "UPDATE doubts SET created=?, expires=?, resolved=? WHERE id=?",
        (now - days_ago * 86400, now - days_ago * 86400 + 7 * 86400,
         resolved, did))
    return did


# ---------------------------------------------------------- 过期转记忆
def test_expired_becomes_cold_event(store):
    """验收 #1：过期 → cold 有记录（kind=doubt, tier=3）。"""
    _add_expired(store)
    ids = expire_to_memory(store, session="s")
    assert len(ids) == 1
    e = store.get_event(ids[0])
    assert e.kind == "doubt" and e.tier == 3


def test_summary_format(store):
    """摘要 = "某个问题当时没搞清楚：{question}"（手册原文）。"""
    _add_expired(store, question="用户的老家在哪个城市")
    eid = expire_to_memory(store, session="s")[0]
    e = store.get_event(eid)
    assert e.summary == "某个问题当时没搞清楚：用户的老家在哪个城市"
    # raw 就是疑问句本身：原文是唯一真相源，疑问句也不例外
    assert e.raw == "用户的老家在哪个城市"


def test_no_double_conversion(store):
    """验收 #4：同一 doubt 只转一次（converted 标记）。"""
    _add_expired(store)
    first = expire_to_memory(store, session="s")
    second = expire_to_memory(store, session="s")
    assert len(first) == 1 and second == []


def test_resolved_not_converted(store):
    """resolved 的不转：答案已经有了，转成"没搞清楚"是撒谎。"""
    _add_expired(store, resolved=1)
    assert expire_to_memory(store, session="s") == []


def test_not_expired_stays(store):
    """没过期的留在池子里（3 天前创建，还剩 4 天）。"""
    _add_expired(store, days_ago=3)
    assert expire_to_memory(store, session="s") == []


def test_consolidate_run_converts(store):
    """巩固集成：没有任何重复记忆的会话，过期疑问也照样转。

    这是当初决定把扫描放在 clusters 判空之前的理由——
    提前返回会让"没有重复"的会话永远清不掉疑问。
    """
    from hippo.core.consolidate import run
    _add_expired(store)
    rep = run(store, "s", summarizer=None)
    assert rep.doubts_converted == 1
    assert store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='doubt'").fetchone()[0] == 1


# ---------------------------------------------------------- resolved 闭环
def test_resolve_matching_closes_doubt(store):
    """用户自己回答了 → 对应 doubt 标记 resolved（验收 #3 前半）。"""
    did = _add_expired(store, question="用户的老家是哪个城市",
                       days_ago=1)          # 没过期，活跃中
    n = resolve_matching_doubts(store, "s", ["用户老家是四川成都的"])
    assert n == 1
    d = store.conn.execute(
        "SELECT resolved FROM doubts WHERE id=?", (did,)).fetchone()
    assert d[0] == 1


def test_resolved_never_injected(store):
    """验收 #3 后半：resolved 后不再注入（active_doubts 不含它）。

    回答句"用户的猫是英短品种"与问句的覆盖率 0.67（实算），
    远超阈值 0.25。
    """
    _add_expired(store, question="用户的猫是什么品种呀", days_ago=1)
    resolve_matching_doubts(store, "s", ["用户的猫是英短品种"])
    assert store.active_doubts("s") == []


def test_converted_event_is_searchable(store):
    """验收 #2：转出的记忆能被检索到——"我忘了"的体感来源。"""
    from hippo.core.rank import l2_search
    _add_expired(store, question="用户的老家在哪个城市")
    eid = expire_to_memory(store, session="s")[0]
    res = l2_search(store, "s", "用户的老家")
    assert any(it.id == eid for it in res.items)
