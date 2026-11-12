# -*- coding: utf-8 -*-
"""M68 验收：写入幂等 + 本地合并。

两种重复要分开处理：
- **完全同一段原话**（重试/重放）：跳过，什么都不改 → 幂等；
- **同一件事换个说法**：不新增事件，把新关键词/重要性合并进旧事件（raw 不动）。
"""
from __future__ import annotations

from hippo.core.persist import persist_result
from hippo.core.schema import EventOut, ExtractResult
from hippo.core.store import Store
from hippo.core.turns import Window


def _window(store: Store, session: str = "s") -> Window:
    turns = store.get_turns(session, limit=6)
    return Window(session=session, idxs=[t.idx for t in turns], token_est=0,
                  has_pending=False, turns=turns)


def _seed(store: Store) -> None:
    store.add_turns("s", [
        ("user", "我在 lib/index.js 里加了 withRetry，重试一次传输失败", 0),
        ("assistant", "好", 0),
    ], ts=1_760_000_000)


def test_identical_raw_is_idempotent(store):
    _seed(store)
    ev = EventOut(raw="我在 lib/index.js 里加了 withRetry，重试一次传输失败",
                  summary="用户给 lib/index.js 的 fetch 加了 withRetry 重试",
                  keywords=["withRetry", "index.js"], importance=4)
    persist_result(store, "s", _window(store), ExtractResult(events=[ev]), now=1_760_000_100)
    before = store.conn.execute("select count(*) from events").fetchone()[0]
    # 原话一模一样再来一次（模拟重试/重放）
    persist_result(store, "s", _window(store), ExtractResult(events=[ev]), now=1_760_000_200)
    after = store.conn.execute("select count(*) from events").fetchone()[0]
    assert before == after == 1


def test_near_duplicate_merges_new_keywords(store):
    _seed(store)
    first = EventOut(raw="我在 lib/index.js 里加了 withRetry，重试一次传输失败",
                     summary="用户给 lib/index.js 的 fetch 加了 withRetry 重试",
                     keywords=["withRetry"], importance=3)
    persist_result(store, "s", _window(store), ExtractResult(events=[first]), now=1_760_000_100)

    # 同一件事的另一种说法，但带新关键词和新数字
    second = EventOut(raw="重试一次传输失败，改成 120ms 后再试，最多两次",
                      summary="用户给 lib/index.js 的 fetch 增加 withRetry 重试机制，间隔 120ms",
                      keywords=["120ms", "重试间隔"], importance=5)
    # 让 raw 锚定通过：把第二句加进库里的 turns
    store.add_turns("s", [("user", "重试一次传输失败，改成 120ms 后再试，最多两次", 0)], ts=1_760_000_150)
    rep = persist_result(store, "s", _window(store), ExtractResult(events=[second]), now=1_760_000_300)

    rows = store.conn.execute("select count(*) from events").fetchone()[0]
    assert rows == 1, "重复事件不该新增"
    assert rep.events_merged == 1
    ev = store.conn.execute("select keywords, importance from events").fetchone()
    kws = str(ev["keywords"]).split()
    assert "120ms" in kws and "withRetry" in kws, kws
    assert ev["importance"] == 5, "重要性取较大值"
    # 关键词进了检索索引
    hit = store.conn.execute(
        "select count(*) from kw_index where token='120ms'").fetchone()[0]
    assert hit == 1


def test_fresh_event_still_written(store):
    _seed(store)
    ev = EventOut(raw="我在 lib/index.js 里加了 withRetry，重试一次传输失败",
                  summary="用户加了重试", keywords=["withRetry"], importance=3)
    persist_result(store, "s", _window(store), ExtractResult(events=[ev]), now=1_760_000_100)
    store.add_turns("s", [("user", "另外我妈下个月十号过生日", 0)], ts=1_760_000_200)
    other = EventOut(raw="另外我妈下个月十号过生日",
                     summary="用户妈妈下个月十号过生日", keywords=["生日"], importance=4)
    persist_result(store, "s", _window(store), ExtractResult(events=[other]), now=1_760_000_300)
    assert store.conn.execute("select count(*) from events").fetchone()[0] == 2
