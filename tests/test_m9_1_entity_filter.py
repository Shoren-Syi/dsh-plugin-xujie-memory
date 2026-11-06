"""M9.1 验收：实体过滤。

L1 唯一允许"猜"的地方是"这句话提到了谁"。猜错的代价是双面的：
  - 漏认（该认的没认）→ 候选集里没有那条记忆 → 它答"不记得"
  - 多认（认错人了）   → 候选集被别人的记忆灌满 → 它答得头头是道，但答的是别人

第二种更难发现，也更难挽回。所以这一节的测试里，"不该命中"比"该命中"多。
"""

from __future__ import annotations

import json
import time

from hippo.core.retrieve import (
    MIN_ENTITY_LEN, Query, extract_entities_in_query, l1_candidates, parse_query,
)


def _ent(store, name, type_="person", aliases=()):
    return store.upsert_entity(
        session="s1", canonical=name, type=type_, aliases=list(aliases))


def _ev(store, raw, entities=(), weight=0.5, **kw):
    return store.add_event(
        session="s1", raw=raw, summary=raw, keywords=[], entities=entities,
        w0=weight, weight=weight, **kw)


# ---------------------------------------------------------------- 命中正确
def test_entity_in_query_finds_its_events(store):
    """提到"老王"必返回老王相关事件。这是 M9.1 唯一不能错的验收。"""
    wang = _ent(store, "老王")
    li = _ent(store, "小李")
    e_wang = _ev(store, "老王昨天辞职了", [wang], weight=0.6)
    e_li = _ev(store, "小李在准备考试", [li], weight=0.6)

    q = parse_query("老王最近怎么样", "s1", store)
    assert q.entity_ids == [wang]
    res = l1_candidates(store, "s1", q)
    assert e_wang in res.event_ids
    assert e_li not in res.event_ids


def test_alias_hits_same_entity(store):
    """用户嘴里说的大概率是别名不是正名。只认 canonical 等于白建 aliases。"""
    eid = _ent(store, "王建国", aliases=["老王", "建国"])
    e = _ev(store, "老王把车卖了", [eid])
    ids, names = extract_entities_in_query("老王的车呢", "s1", store)
    assert ids == [eid], names
    assert e in l1_candidates(store, "s1", text="老王的车呢").event_ids


def test_entity_matched_by_normalized_alias(store):
    """别名带装饰（"隔壁的老王"）也要认。normalize_name 在 M7 已经做过这件事。"""
    eid = _ent(store, "王建国", aliases=["隔壁的老王"])
    ids, _ = extract_entities_in_query("隔壁老王今天没来", "s1", store)
    assert ids == [eid]


# ---------------------------------------------------------------- 不该命中
def test_longest_name_wins_and_short_name_does_not_double_hit(store):
    """"老王"和"王"同时存在时，只能返回一个实体。

    返回两个的后果不是"多一条记忆"，是候选集翻倍 + 排序被打乱：
    "王"关联的事件可能是几十条无关的（"王"这个字出现在"王牌""王经理"里），
    它们按 weight 挤进来，把真正的"老王"挤到后面去。
    """
    long_id = _ent(store, "老王")
    short_id = _ent(store, "王")
    ids, names = extract_entities_in_query("老王来了", "s1", store)
    assert ids == [long_id]
    assert short_id not in ids


def test_single_char_entity_never_matches(store):
    """单字实体不参与查询匹配（MIN_ENTITY_LEN=2）。

    代价是"我妈"捞不到 canonical='妈' 的实体。收益是"妈"不会命中
    "妈妈""姨妈""姑妈""大妈"——后者的每一条都会把候选集灌满。
    这条测试把"代价"写成断言，是为了以后有人想放宽时能看见它换来了什么。
    """
    eid = _ent(store, "妈")
    assert MIN_ENTITY_LEN == 2
    ids, _ = extract_entities_in_query("我妈今天住院了", "s1", store)
    assert ids == []


def test_no_entity_returns_empty_not_error(store):
    """无实体时返回空集，不报错。

    这里返回 [] 而 l1_candidates 返回全库，两者不矛盾：
    前者是"没认出人"，后者是"没认出人也得给点东西"。
    """
    _ev(store, "今天天气不错")
    ids, names = extract_entities_in_query("今天天气不错", "s1", store)
    assert ids == [] and names == []


def test_entity_of_other_session_does_not_leak(store):
    """别人的会话里有个"老王"，不能被他命中。"""
    other = store.upsert_entity(session="s2", canonical="老王", type="person")
    _ev(store, "老王在 s2 里存在", [other])
    ids, _ = extract_entities_in_query("老王呢", "s1", store)
    assert ids == []


# ---------------------------------------------------------------- 关系表不变量
def test_json_column_never_drifts_from_relation(store):
    """`events.entities`（JSON 缓存）必须与 `event_entities`（真相源）一致。

    M9 只查关系表（D59）。如果有人改坏了 002 的同步触发器，
    检索会静默少结果，而 JSON 列看起来完全正常——这类 bug 只能靠不变量测试抓。
    """
    a = _ent(store, "老王")
    b = _ent(store, "小李")
    e = _ev(store, "老王和小李一起吃饭", [a, b])

    row = store.conn.execute(
        "SELECT entities FROM events WHERE id = ?", (e,)).fetchone()
    from_json = sorted(json.loads(row["entities"]))
    from_rel = sorted(store.entities_of_event(e))
    assert from_json == from_rel == sorted([a, b])


# ---------------------------------------------------------------- 压缩率与耗时
def _seed(store, n_events=8000, n_entities=40):
    """造一个 8000 条的库：40 个实体，每个 200 条事件。"""
    ids = [_ent(store, f"人物{i:02d}") for i in range(n_entities)]
    with store.tx():
        for i in range(n_events):
            eid = ids[i % n_entities]
            store.conn.execute(
                """
                INSERT INTO events (session, event_time, ingest_time, w0, weight,
                                    keywords, summary, raw, entities)
                VALUES ('s1', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (1_700_000_000 + i * 60, 1_700_000_000 + i * 60,
                 0.3 + (i % 7) / 10, 0.3 + (i % 7) / 10,
                 "关键词", f"事件{i}", f"这是第{i}条事件", json.dumps([eid])),
            )
    store.conn.commit()
    return ids


def test_compression_rate_under_5_percent(store):
    """8000 条 → 候选 / 全库 ≤5%。"""
    ids = _seed(store)
    res = l1_candidates(store, "s1", text="人物07最近怎么样")
    total = store.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    ratio = len(res.event_ids) / total
    assert res.query.entity_ids == [ids[7]]
    assert ratio <= 0.05, f"{len(res.event_ids)}/{total} = {ratio:.1%}"


def test_entity_filter_is_faster_than_scan(store):
    """实体过滤必须比全表扫快，且快得明显。

    刻意**不写绝对秒数**（事故 46）：CI 机器可能比开发机慢 4 倍，
    `assert elapsed < 5ms` 会在别人的机器上红，而在自己机器上永远是绿的——
    那种测试保护不了任何人。这里比的是"带实体条件 vs 不带实体条件"，
    两者跑在同一台机器、同一个库上，比值才是可移植的。
    """
    _seed(store)
    q = parse_query("人物07最近怎么样", "s1", store)

    def timed(fn):
        t0 = time.perf_counter()
        fn()
        return time.perf_counter() - t0

    t_filtered = min(timed(lambda: l1_candidates(store, "s1", q)) for _ in range(3))
    t_full = min(timed(lambda: l1_candidates(
        store, "s1", Query(text=""), cap=100_000)) for _ in range(3))

    assert t_filtered < t_full, (
        f"实体过滤反而更慢：{t_filtered*1000:.2f}ms vs {t_full*1000:.2f}ms")
    assert t_filtered < 50 / 1000, f"8000 条下 {t_filtered*1000:.2f}ms，超过 50ms"
