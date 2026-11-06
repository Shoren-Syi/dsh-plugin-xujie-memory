"""M10.1 验收：关键词倒排。

这一节的核心不是"能查到"，是**2 字词能查到**。

SQLite FTS5 的 trigram 分词器只索引 ≥3 字符的片段，"老王""搬家""房租"
这些 2 字词它一个都建不了索引。中文词以 2 字为主，所以 FTS 在中文里
不是"弱一点"，是"基本不可用"。kw_index 才是主力。
"""

from __future__ import annotations

import json
import os
import tempfile
import time

import pytest

from hippo.core.db import connect
from hippo.core.lexicon import MAX_QUERY_TOKENS, query_tokens
from hippo.core.migrate import migrate
from hippo.core.store import Store


# ------------------------------------------------------------------ 分词
def test_two_char_word_survives_tokenization():
    """'老王' 必须出现在 token 集里。这是全部设计的前提。"""
    toks = query_tokens("老王最近怎么样")
    assert "老王" in toks


def test_tokenizer_finds_word_inside_sentence():
    """句子里找词：'我上次搬家那个事' → '搬家'。

    倒排里存的是词，用户给的是句子。不做这层映射，句子一个 token 都命中不了。
    """
    toks = query_tokens("我上次搬家那个事")
    assert "搬家" in toks


def test_tokenizer_demotes_weak_grams():
    """'我在''在那' 这种"两个停用字拼起来"的片段**保留但排到最后**。

    早先的写法是直接砍掉。实测砍错了"最后"——"最"和"后"都在单字停用词表里，
    而"我拖到最后一刻"整句只有这一个有效线索。单字表判断不了两字组合是不是词。

    所以现在不砍，只降级：预算不够时先丢它们，够就让它们照常参与。
    """
    toks = query_tokens("我在那几天")
    assert "几天" in toks, "实词片段必须留着"
    assert "我在" in toks, "弱片段保留"
    assert toks.index("几天") < toks.index("我在"), "弱片段排在实词后面"


def test_tokenizer_keeps_word_made_of_two_stopword_chars():
    """'最后' 是词，虽然两个字都在单字停用词表里。

    这条是事故 60 的回归：砍掉它之后，'我拖到最后一刻' 的 18 个片段
    一个都命中不了库里的 '拖延 习惯 最后两天'。
    """
    assert "最后" in query_tokens("我总是拖到最后一刻")


def test_tokenizer_keeps_ascii_words():
    toks = query_tokens("我用 GPT-4 写代码")
    assert "gpt-4" in toks


def test_tokenizer_is_bounded():
    """占位符预算：48 个查询片段 + 800 个候选 < SQLite 的 999。

    超了不是"查得更全"，是 `sqlite3.OperationalError: too many SQL variables`，
    而那行错误发生在用户发消息的路径上。
    """
    long_q = "我想问一下上次说的那个关于搬家和房租还有孩子上学的事情到底怎么样了"
    toks = query_tokens(long_q)
    assert len(toks) <= MAX_QUERY_TOKENS
    assert len(toks) + 800 < 999


def test_tokenizer_empty_input():
    assert query_tokens("") == []
    assert query_tokens("   ") == []
    assert query_tokens("！！？？") == []


# ------------------------------------------------------------------ 倒排
def test_two_char_keyword_is_found_by_kw_index(store):
    eid = store.add_event(
        session="s1", raw="老王今天辞职了", event_time=1700000000,
        keywords="老王 辞职", summary="老王辞职")
    hits = store.search_by_tokens("s1", ["老王"])
    assert [h[0].id for h in hits] == [eid]


def test_more_hits_rank_higher(store):
    a = store.add_event(session="s1", raw="老王搬家了", event_time=1700000000,
                        keywords="老王 搬家", summary="老王搬家")
    b = store.add_event(session="s1", raw="房东通知涨房租，可能要搬家",
                        event_time=1700000100, keywords="搬家 房租", summary="考虑搬家")
    rows = store.search_by_tokens("s1", ["老王", "搬家"])
    assert rows[0][0].id == a and rows[0][1] == 2
    assert rows[1][0].id == b and rows[1][1] == 1


def test_empty_tokens_return_empty(store):
    store.add_event(session="s1", raw="随便一条", event_time=1700000000,
                    keywords="随便")
    assert store.kw_search("s1", []) == []
    assert store.kw_search("s1", [""]) == []


def test_hit_tokens_are_reported(store):
    """返回命中的 token 明细——`recall --explain` 靠它解释"为什么是这条"。"""
    store.add_event(session="s1", raw="老王搬家", event_time=1700000000,
                    keywords="老王 搬家 朝阳")
    rows = store.kw_search("s1", ["老王", "搬家"])
    assert rows[0][1] == 2
    assert set(rows[0][2]) == {"老王", "搬家"}


# ------------------------------------------------------------------ 前缀兜底
def test_prefix_fallback_finds_longer_token(store):
    """用户说"老王"，库里存的是"老王头"。

    精确 IN 查不到。前缀范围查询补上。
    """
    eid = store.add_event(session="s1", raw="老王头在门口下棋", event_time=1700000000,
                          keywords="老王头 下棋", summary="老王头下棋")
    rows = store.kw_search("s1", ["老王"])
    assert [r[0].id for r in rows] == [eid]


def test_prefix_fallback_does_not_double_count(store):
    """同时被精确和前缀命中的事件，token 不能算两次。

    "老王"精确命中 + "老王头"前缀命中，是**两个不同的词**，算 2；
    但同一个词被两条路都命中（理论上不会，实际 SQL 会），只能算 1。
    """
    store.add_event(session="s1", raw="老王头和老王", event_time=1700000000,
                    keywords="老王 老王头", summary="两个人")
    rows = store.kw_search("s1", ["老王"], prefix_min_hits=5)
    assert len(rows) == 1
    assert rows[0][1] == 2
    assert set(rows[0][2]) == {"老王", "老王头"}


def test_prefix_fallback_skipped_when_exact_enough(store):
    """精确命中已有 3 条以上时不跑前缀。

    前缀是兜底（D64）：它扫的行多一个量级，常态路径不该付这个代价。
    这里用"库里有个只在长词里出现的片段"来验证——不跑前缀就查不到它。
    """
    for i in range(3):
        store.add_event(session="s1", raw=f"老王的事{i}", event_time=1700000000 + i,
                        keywords="老王", summary=f"老王{i}")
    store.add_event(session="s1", raw="老王头下棋", event_time=1700000000,
                    keywords="老王头", summary="下棋")

    got = {r[0].summary for r in store.kw_search("s1", ["老王"], prefix_min_hits=3)}
    assert got == {"老王0", "老王1", "老王2"}, "够 3 条就不该跑前缀"


def test_prefix_fallback_ignores_single_char(store):
    """单字片段不参与前缀。'妈' 会捞回半库（同 D60 的理由）。"""
    store.add_event(session="s1", raw="妈妈住院", event_time=1700000000,
                    keywords="妈妈 住院", summary="住院")
    assert store.kw_search("s1", ["妈"]) == []


# ------------------------------------------------------------------ 与 L1 联动
def test_kw_search_respects_candidates(store):
    a = store.add_event(session="s1", raw="老王搬家", event_time=1700000000,
                        keywords="老王 搬家", summary="a")
    b = store.add_event(session="s1", raw="老王出差", event_time=1700000100,
                        keywords="老王 出差", summary="b")
    rows = store.kw_search("s1", ["老王"], candidates=[b])
    assert [r[0].id for r in rows] == [b]
    assert a not in [r[0].id for r in rows]


def test_kw_search_respects_superseded(store):
    eid = store.add_event(session="s1", raw="老王在腾讯", event_time=1700000000,
                          keywords="老王 腾讯", summary="旧")
    new = store.add_event(session="s1", raw="老王跳到字节", event_time=1700100000,
                          keywords="老王 字节", summary="新")
    store.supersede_event(eid, new)
    default = [r[0].id for r in store.kw_search("s1", ["老王"])]
    assert default == [new], "被取代的那条默认看不见，新的那条还在"
    hist = [r[0].id for r in store.kw_search("s1", ["老王"], include_historical=True)]
    assert set(hist) == {eid, new}


# ------------------------------------------------------------------ 性能
def _big_store(n: int = 8000) -> tuple[Store, str]:
    d = tempfile.mkdtemp()
    conn = connect(os.path.join(d, "bench.db"))
    migrate(conn)
    st = Store(conn)
    words = ["搬家", "房租", "老王", "字节", "跳槽", "孩子", "上学", "复查",
             "出差", "深圳", "猫粮", "年糕", "加班", "体检", "房贷"]
    with st.tx():
        for i in range(n):
            st.conn.execute(
                "INSERT INTO events (session,event_time,ingest_time,w0,weight,"
                "keywords,summary,raw,entities) VALUES ('s1',?,?,?,?,?,?,?,'[]')",
                (1700000000 + i * 60, 1700000000 + i * 60,
                 0.3 + (i % 7) / 10, 0.3 + (i % 7) / 10,
                 f"{words[i % 15]} {words[(i * 3) % 15]} {words[(i * 7) % 15]}",
                 f"第{i}条摘要", f"第{i}条原文"))
        # 走 add_event 会顺带写 kw_index？不会：这里直插 events，kw_index 得自己补。
        # 这个坑在第一次写这个 bench 时就踩了：指标好看得像假的，因为库里 0 行倒排。
        rows = st.conn.execute("SELECT id, keywords FROM events").fetchall()
        for r in rows:
            for t in r["keywords"].split():
                st.conn.execute(
                    "INSERT OR IGNORE INTO kw_index(token, event_id) VALUES (?,?)",
                    (t, r["id"]))
    return st, d


@pytest.mark.slow
def test_kw_search_8000_under_8ms():
    """8000 条 → 全部命中 → 40 条，< 8ms（手册验收 #3）。

    断言的是 8ms 这个绝对数，而不是比值。理由：这一层的耗时决定了
    **每轮对话都能不能调它**，而 M13 之后每轮要调 1~2 次。比值答不了这个问题。
    （事故 46 的教训是"别用绝对秒数比两台机器的速度"，不是"永远别用绝对数"。）
    """
    st, d = _big_store()
    try:
        ids = [r[0] for r in st.conn.execute("SELECT id FROM events LIMIT 800").fetchall()]
        toks = query_tokens("老王 搬家 房租 孩子上学")
        # 预热
        st.kw_search("s1", toks, limit=40, candidates=ids)

        best = 1e9
        for _ in range(3):
            t0 = time.perf_counter()
            rows = st.kw_search("s1", toks, limit=40, candidates=ids)
            best = min(best, (time.perf_counter() - t0) * 1000)
        assert len(rows) == 40
        assert best < 8.0, f"8000 条下 {best:.2f}ms，超过 8ms"
    finally:
        st.close()


@pytest.mark.slow
def test_kw_search_no_candidates_still_fast():
    """不带候选集（L1 没认出来）时也要快——那时它要扫全库。

    这条是 M9 留下的现场：'我几点睡' 认不出实体，候选 = 全库 800 条，
    kw_search 就是唯一在干活的层。它慢了，整轮就慢了。
    """
    st, d = _big_store()
    try:
        toks = query_tokens("老王 搬家 房租")
        st.kw_search("s1", toks, limit=40)
        best = 1e9
        for _ in range(3):
            t0 = time.perf_counter()
            st.kw_search("s1", toks, limit=40)
            best = min(best, (time.perf_counter() - t0) * 1000)
        assert best < 15.0, f"全库扫描 {best:.2f}ms，超过 15ms"
    finally:
        st.close()
