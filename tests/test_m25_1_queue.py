"""M25.1 队列生成：四类缺口、优先级、去重。

这些测试只碰库，不碰模型——"零 token"在实现上就是"这段代码里没有
任何网络客户端"，测试要钉的是"跑完队列之后，metrics 里一行都没有"。
"""

from __future__ import annotations

import time

import pytest

from hippo.core.curiosity import Gap, build_queue, collect_gaps, format_queue


def _now() -> int:
    return int(time.time())


def _seed_all(store, now: int) -> None:
    # ① 过期未解
    store.add_doubt(session="s", question="你说的「那边」是公司还是老家？",
                    impact=0.9, expire_days=0, now=now - 9 * 86400)
    # ② 钉住的事里提到、角色卡空白的人
    eid = store.upsert_entity(session="s", canonical="老王", type="person")
    store.add_event(session="s", raw="老王还在那家创业公司",
                    event_time=now - 20 * 86400, pinned=1, entities=[eid])
    # ③ 低置信 + 久未更新
    eid2 = store.upsert_entity(session="s", canonical="小李", type="person")
    ev2 = store.add_event(session="s", raw="小李爱熬夜", event_time=now - 40 * 86400,
                          entities=[eid2])
    store.add_trait(entity_id=eid2, trait="作息很乱", evidence=[ev2], confidence=0.3)
    store.conn.execute("UPDATE card_traits SET updated_at = ?", (now - 40 * 86400,))
    # ④ 失效且没有新值
    store.set_fact(session="s", key="居住城市", value="杭州", when=now - 60 * 86400)
    store.conn.execute("UPDATE facts SET valid_to = ?", (now - 5 * 86400,))
    store.conn.commit()


def test_four_kinds_produce(store):
    """四类缺口各出一条。少一类的话，不是"没数据"，是 SQL 写错了。"""
    now = _now()
    _seed_all(store, now)

    gaps = collect_gaps(store, "s", now=now)
    kinds = {g.kind for g in gaps}
    assert kinds == {"doubt", "entity", "trait", "fact"}, kinds


def test_zero_token(store):
    """生成队列不花一分钱：跑完队列，metrics 一行都不该有。"""
    now = _now()
    _seed_all(store, now)
    build_queue(store, "s", now=now)

    n = store.conn.execute("SELECT count(*) FROM metrics").fetchone()[0]
    assert n == 0


def test_priority_sorted(store):
    """排序必须按 impact×urgency×novelty 降序。

    这里造一个"影响大但刚过期"和一个"影响小但放了很久"，验证乘积
    真的在起作用，而不是按 impact 或按时间单排。
    """
    now = _now()
    # A 刚过期一天、影响大；B 放了 30 天、影响小。
    # 0.9×0.29 = 0.26 < 0.45×1.0 = 0.45：**搁久了的小事该排在
    # 刚过期的大事前面**。这正是 urgency 存在的意义——不同就不设它了。
    store.add_doubt(session="s", question="A 那边是公司还是老家？", impact=0.9,
                    expire_days=0, now=now - 86400)
    store.add_doubt(session="s", question="B 那个人叫什么来着？", impact=0.45,
                    expire_days=0, now=now - 30 * 86400)
    gaps = build_queue(store, "s", now=now)
    assert gaps[0].question.startswith("B")
    assert gaps == sorted(gaps, key=lambda g: -g.priority)

    # 同样搁了 30 天，影响大的排前面。
    store.conn.execute("DELETE FROM doubts")
    store.add_doubt(session="s", question="A 那边是公司还是老家？", impact=0.9,
                    expire_days=0, now=now - 30 * 86400)
    store.add_doubt(session="s", question="B 那个人叫什么来着？", impact=0.45,
                    expire_days=0, now=now - 30 * 86400)
    gaps2 = build_queue(store, "s", now=now)
    assert gaps2[0].question.startswith("A")


def test_priority_formula(store):
    """优先级就是三个数的乘积，没有暗改系数。"""
    g = Gap(kind="doubt", ref="doubt:1", question="x？",
            impact=0.8, urgency=0.5, novelty=0.5)
    assert g.priority == pytest.approx(0.2)
    assert "impact 0.80" in g.why()


def test_empty_db(store):
    """空库不报错，返回空队列。"""
    assert build_queue(store, "nobody") == []
    assert "空" in format_queue([])


def test_same_gap_only_once(store):
    """同一个缺口问过就沉底，不会天天冒出来。

    判据是 gap_ref（doubt:1），不是问题文本——同一件事换个说法
    就能再问一遍的话，"只问一次"就是摆设。
    """
    now = _now()
    store.add_doubt(session="s", question="那边是公司还是老家？", impact=0.9,
                    expire_days=0, now=now - 8 * 86400)
    assert len(build_queue(store, "s", now=now)) == 1

    store.add_curiosity(session="s", question="那边是公司还是老家？",
                        gap_kind="doubt", gap_ref="doubt:1", impact=0.9,
                        urgency=1.0, novelty=1.0, priority=0.9, now=now)
    assert build_queue(store, "s", now=now) == []


def test_similar_questions_deduped(store):
    """两个疑问在问同一件事 → 只留一条。"""
    now = _now()
    store.add_doubt(session="s", question="用户说的那边是公司还是老家？",
                    impact=0.8, expire_days=0, now=now - 8 * 86400)
    store.add_doubt(session="s", question="用户说的那边是公司还是老家",
                    impact=0.8, expire_days=0, now=now - 8 * 86400)

    gaps = build_queue(store, "s", now=now)
    assert len(gaps) == 1


def test_queue_reaches_prompt(store, cfg):
    """队列里的话真的会进 prompt，而且只进一次。

    只测 build_queue 是不够的：一个永远不投递的队列，看起来完美，
    实际上是死的。
    """
    from hippo.core.curiosity import Curiosity
    from hippo.core.pack import pack

    now = _now()
    # 这条测试曾经**夜里必挂**：默认配置有 quiet_hours（23~7 点不追问），
    # 而 prepare() 在安静时段直接返回 None，断言就红了。CI 在夜里跑永远是红的。
    # 这条测的是"队列会不会真的进 prompt"，和作息无关，所以显式关掉安静时段。
    cfg.curiosity.quiet_hours = []
    store.add_doubt(session="s", question="那边是公司还是老家？", impact=0.9,
                    expire_days=0, now=now - 8 * 86400)
    assert Curiosity(store, cfg).prepare("s", now) is not None

    first = pack(store, "s", "今天干点啥", cfg=cfg)
    assert "[想问]" in first.text
    assert "那边是公司还是老家" in first.text

    # 说完还没等到回应，第二轮不许重复
    second = pack(store, "s", "今天干点啥", cfg=cfg)
    assert "[想问]" not in second.text


def test_questions_are_askable(store):
    """问出口的句子要像人话：都以问号收尾，不夹英文 key。"""
    now = _now()
    _seed_all(store, now)
    for g in build_queue(store, "s", now=now):
        assert g.question.endswith(("？", "?")), g.question
        assert "居住城市" in g.question or "city" not in g.question
