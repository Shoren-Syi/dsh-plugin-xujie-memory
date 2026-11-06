"""M6.1 关键词清洗（策略层）。

分界线：`schema.clean_keywords` 只做机械归一化（解析期），
`keywords.clean_keywords` 做策略判断（入库期，有实体名上下文）。
这里测的是后者。
"""

from __future__ import annotations

import pytest

from hippo.core.db import connect
from hippo.core.keywords import (
    MAX_KEYWORDS, clean_keywords, explain, is_empty_word, normalize_keywords,
)
from hippo.core.migrate import migrate
from hippo.core.store import Store


# ---------------------------------------------------------------- 机械层
def test_normalize_splits_string_on_spaces_and_punctuation():
    assert normalize_keywords("老王 跳槽、字节，房租") == ["老王", "跳槽", "字节", "房租"]


def test_normalize_strips_quotes_and_book_marks():
    assert normalize_keywords(['"搬家"', "《三体》", "「房租」"]) == ["搬家", "三体", "房租"]


def test_normalize_dedupes_case_insensitively():
    assert normalize_keywords(["AI", "ai", "K8s"]) == ["AI", "K8s"]


def test_normalize_keeps_order():
    assert normalize_keywords(["丙", "甲", "乙"]) == ["丙", "甲", "乙"]


# ---------------------------------------------------------------- 策略层
def test_drops_function_words():
    assert clean_keywords(["搬家", "那个", "然后", "房租"]) == ["搬家", "房租"]


def test_drops_generic_words():
    assert clean_keywords(["事情", "问题", "东西", "搬家"]) == ["搬家"]


def test_keeps_real_topics_that_look_generic():
    """泛词和"真话题"的界线：指向对话本身的砍，指向世界的留。"""
    assert clean_keywords(["工作", "心情", "搬家"]) == ["工作", "心情", "搬家"]


def test_drops_single_char_unless_entity():
    assert clean_keywords(["猫", "狗"]) == []
    assert clean_keywords(["猫", "狗"], entities=["猫"]) == ["猫"]


def test_entity_name_beats_length_rule():
    """实体名连停用词表都能豁免——"老王"如果哪天进了词表，也不能被砍。"""
    assert clean_keywords(["老王", "搬家"], entities=["老王"]) == ["老王", "搬家"]


def test_drops_too_long():
    """一句话不是检索键，是一条劣质摘要。"""
    assert clean_keywords(["用户昨天下午从朝阳搬到海淀房租涨两千"]) == []
    assert clean_keywords(["朝阳区搬家"]) == ["朝阳区搬家"]


def test_drops_pure_numbers_and_punctuation():
    assert clean_keywords(["2026", "12.5", "！！！", "搬家"]) == ["搬家"]


def test_empty_input():
    assert clean_keywords(None) == []
    assert clean_keywords([]) == []
    assert clean_keywords("") == []


# ---------------------------------------------------------------- 截断
def test_cap_is_eight():
    assert len(clean_keywords([f"词{i}词" for i in range(20)])) == MAX_KEYWORDS


def test_cap_prefers_entities():
    """实体名必须活过截断，否则"这段对话里有谁"就检索不到。"""
    many = [f"词{i}词" for i in range(12)] + ["老王"]
    out = clean_keywords(many, entities=["老王"])
    assert "老王" in out and len(out) == MAX_KEYWORDS


def test_cap_prefers_longer_tokens():
    many = [f"词{i}" for i in range(12)] + ["创业公司"]
    out = clean_keywords(many)
    assert "创业公司" in out, f"长词被短词挤掉了：{out}"


def test_truncation_keeps_original_order():
    raw = ["房租", "搬家", "朝阳", "老王", "年糕", "海淀", "字节", "后端", "加班", "地铁"]
    out = clean_keywords(raw, entities=["老王"])
    assert out == sorted(out, key=raw.index), f"顺序被打乱：{out}"


# ---------------------------------------------------------------- 解释
def test_explain_gives_reason_per_word():
    got = dict(explain(["搬家", "那个", "事情", "猫", "2026"], entities=["猫"]))
    assert got["搬家"] == "保留"
    assert got["那个"] == "停用词"
    assert got["事情"] == "空词"
    assert got["猫"] == "保留"
    assert got["2026"] == "纯数字"


def test_explain_marks_duplicates():
    got = dict(explain(["搬家", "搬家"]))
    assert got["搬家"] == "重复"


def test_is_empty_word_is_pre_cleaning_metric():
    """评测必须在清洗**前**统计空词，否则清洗完永远是 0。"""
    assert is_empty_word("事情") is True
    assert is_empty_word("那个") is True
    assert is_empty_word("搬家") is False
    assert is_empty_word("猫", entities=["猫"]) is False


# ---------------------------------------------------------------- 端到端：自召回
@pytest.fixture()
def store(tmp_path):
    conn = connect(tmp_path / "m.db")
    migrate(conn)
    s = Store(conn)
    yield s
    s.close()


def _seed(store: Store, cases: list[tuple[str, str]]) -> dict[str, int]:
    """写入若干事件，返回 raw -> id。"""
    ids = {}
    with store.tx():
        for raw, kws in cases:
            ids[raw] = store.add_event(
                session="s", raw=raw, summary=raw[:30],
                keywords=clean_keywords(kws.split()),
            )
    return ids


def test_keywords_can_recall_their_own_event(store):
    """self_recall@1：用某条事件的关键词去搜，第一条就该是它自己。

    这条是 M6.1 的核心理由。关键词写得再好，如果搜不出来就是装饰品。
    """
    cases = [
        ("我昨天下午搬家了，从朝阳搬到海淀", "搬家 朝阳 海淀"),
        ("房租涨了两千，有点肉疼", "房租 涨价"),
        ("老王说他下周要去深圳出差", "老王 深圳 出差"),
        ("年糕今天吐毛球了，吓我一跳", "年糕 吐毛球"),
        ("公司新来了个后端，天天问我要文档", "后端 文档"),
    ]
    ids = _seed(store, cases)
    misses = []
    for raw, _ in cases:
        ev = store.get_event(ids[raw])
        hits = store.search_by_tokens("s", ev.keyword_list, limit=3)
        if not hits or hits[0][0].id != ev.id:
            misses.append((raw, ev.keyword_list,
                           [h[0].id for h in hits]))
    assert not misses, f"自召回失败：{misses}"


def test_dirty_keywords_are_cleaned_on_write(store):
    """写入层兜底：调用方忘了清洗，库里也不该出现空词。"""
    eid = store.add_event(session="s", raw="我搬家了", keywords=["事情", "搬家", "那个"])
    ev = store.get_event(eid)
    assert ev.keyword_list == ["搬家"]


def test_keywords_land_in_kw_index(store):
    eid = store.add_event(session="s", raw="我搬家了", keywords=["搬家", "朝阳"])
    rows = store.conn.execute(
        "SELECT token FROM kw_index WHERE event_id = ? ORDER BY token", (eid,)).fetchall()
    # 顺序按 SQLite 的字节序，不按中文拼音——这里只关心"两个都进去了"
    assert {r[0] for r in rows} == {"朝阳", "搬家"}
