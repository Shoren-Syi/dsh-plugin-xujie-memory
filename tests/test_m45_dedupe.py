# -*- coding: utf-8 -*-
"""M45 验收：事件去重。

真实成因：一次抽取的窗口里同时有用户原话和助手复述，模型会为两句话各产出一条
事件，摘要几乎一样。不挡的话，同一件事会越记越多，检索时互相挤占预算。
"""
from __future__ import annotations

from hippo.core.dedupe import is_duplicate, similarity
from hippo.core.persist import persist_result
from hippo.core.schema import EventOut, ExtractResult
from hippo.core.store import Store
from hippo.core.turns import Window


def _window(store: Store, session: str = "s") -> Window:
    turns = store.get_turns(session, limit=6)
    return Window(session=session, idxs=[t.idx for t in turns], token_est=0,
                  has_pending=False, turns=turns)


def test_similarity_separates_real_duplicates():
    dup = ("用户下个月辞职，去杭州一家医疗公司，工资涨四千",
           "用户下个月将辞职，去杭州一家医疗公司工作，工资涨四千")
    diff = ("用户住在杭州", "用户去北京出差三天")
    assert similarity(*dup) > 0.62, similarity(*dup)
    assert similarity(*diff) < 0.3, similarity(*diff)


def test_is_duplicate_threshold():
    others = ["用户下个月辞职，去杭州一家医疗公司，工资涨四千"]
    assert is_duplicate("用户下个月将辞职，去杭州一家医疗公司工作，工资涨四千", others)
    assert not is_duplicate("用户养了一只猫叫年糕", others)


def test_persist_skips_echo_of_same_event(store):
    """同一轮里"用户说"和"助手复述"两条，只留一条。"""
    store.add_turns("s", [
        ("user", "我下个月要辞职，去杭州一家医疗公司，工资涨了四千", 0),
        ("assistant", "记下了：下个月辞职，去杭州医疗公司，涨薪四千", 0),
    ], ts=1_760_000_000)
    rep = persist_result(store, "s", _window(store), ExtractResult(events=[
        EventOut(raw="我下个月要辞职，去杭州一家医疗公司，工资涨了四千",
                 summary="用户下个月辞职，去杭州一家医疗公司，工资涨四千",
                 keywords=["辞职", "杭州"], importance=5),
        # 同一件事换个说法（真实场景里这来自助手复述或模型重复输出）
        EventOut(raw="我下个月要辞职",
                 summary="用户下个月将辞职，去杭州一家医疗公司工作，工资涨四千",
                 keywords=["辞职", "杭州"], importance=5),
    ]), now=1_760_000_100)
    rows = store.conn.execute("select summary from events").fetchall()
    assert len(rows) == 1, [r[0] for r in rows]
    assert len(rep.event_ids) == 1
    assert rep.events_dup == 1


def test_persist_keeps_genuinely_different_events(store):
    store.add_turns("s", [
        ("user", "我下个月要辞职，去杭州一家医疗公司", 0),
        ("user", "另外我养的猫年糕三岁了", 0),
    ], ts=1_760_000_000)
    persist_result(store, "s", _window(store), ExtractResult(events=[
        EventOut(raw="我下个月要辞职，去杭州一家医疗公司",
                 summary="用户下个月辞职，去杭州医疗公司", keywords=["辞职"], importance=5),
        EventOut(raw="另外我养的猫年糕三岁了",
                 summary="用户养的猫年糕三岁", keywords=["年糕", "猫"], importance=3),
    ]), now=1_760_000_100)
    assert store.conn.execute("select count(*) from events").fetchone()[0] == 2
