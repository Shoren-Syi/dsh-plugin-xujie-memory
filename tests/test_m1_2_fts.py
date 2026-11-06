"""M1.2 验收：FTS5 同步、trigram 边界、kw_index 互补。"""

from __future__ import annotations

import sqlite3

from hippo.core.migrate import rebuild_fts
from hippo.core.store import Store

SUMMARY = "用户对花生严重过敏，曾过敏性休克送医，随身带肾上腺素笔。"
KEYWORDS = "花生 过敏 急救"


def _seed(store: Store) -> int:
    return store.add_event(
        session="s1",
        raw="我对花生过敏，上次差点没命。",
        summary=SUMMARY,
        keywords=KEYWORDS,
        event_time=1700000000,
    )


def test_insert_is_indexed(store):
    eid = _seed(store)
    hits = store.fts_search("花生严重过敏")
    assert eid in [h[0] for h in hits]


def test_summary_update_resyncs(store):
    eid = _seed(store)
    assert store.fts_search("花生严重过敏")
    store.conn.execute(
        "UPDATE events SET summary = ? WHERE id = ?",
        ("用户喜欢猫，养了一只叫年糕的橘猫。", eid),
    )
    assert store.fts_search("花生严重过敏") == []
    assert eid in [h[0] for h in store.fts_search("年糕的橘猫")]


def test_delete_resyncs(store):
    eid = _seed(store)
    assert store.fts_search("花生严重过敏")
    assert store.delete_event(eid)
    assert store.fts_search("花生严重过敏") == []


def test_weight_update_does_not_touch_fts(store):
    """权重/访问时间的高频更新不应触发 FTS 重建（性能）。"""
    eid = _seed(store)
    before = store.conn.execute(
        "SELECT count(*) FROM events_fts_data"
    ).fetchone()[0]
    for _ in range(50):
        store.touch_events([eid])
    after = store.conn.execute(
        "SELECT count(*) FROM events_fts_data"
    ).fetchone()[0]
    assert after == before
    assert eid in [h[0] for h in store.fts_search("花生严重过敏")]


def test_two_char_query_needs_kw_index(store):
    """trigram 只索引 ≥3 字符片段 —— 2 字中文词必须走 kw_index。

    这条用例是回归护栏：如果哪天有人把主力检索改回 FTS5，它会立刻报警。
    """
    eid = store.add_event(
        session="s1", raw="老王从字节跳槽了。",
        summary="用户的朋友老王从字节跳动离职，去了创业公司。",
        keywords="老王 跳槽 字节",
    )
    try:
        fts_hits = store.fts_search("老王")
    except sqlite3.OperationalError:
        fts_hits = []
    assert fts_hits == [], "FTS5 trigram 不该命中 2 字词"

    kw_hits = store.search_by_tokens("s1", ["老王"])
    assert [h[0].id for h in kw_hits] == [eid], "kw_index 必须兜住 2 字词"


def test_rebuild_fts(store):
    eid = _seed(store)
    store.conn.execute("DROP TRIGGER events_au")
    store.conn.execute(
        "UPDATE events SET summary = '改过的摘要，包含独一无二的词汇甲乙丙' WHERE id = ?",
        (eid,),
    )
    assert store.fts_search("独一无二的词汇甲乙丙") == []
    rebuild_fts(store.conn)
    assert eid in [h[0] for h in store.fts_search("独一无二的词汇甲乙丙")]


def test_fts_index_isolated_by_token_length(store):
    """≥3 字可命中，证明索引本身正常，只是不覆盖 2 字。"""
    store.add_event(
        session="s1", raw="r", summary="杭州西湖边的房子租金很贵", keywords="杭州 租房",
    )
    assert store.fts_search("西湖边的房子")
