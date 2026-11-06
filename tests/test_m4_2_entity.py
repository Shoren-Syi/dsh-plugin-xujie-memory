"""M4.2 实体命中。

核心是两个取舍，都在 docstring 里写明了，测试就是把它们钉住：
  1. 不做模糊匹配（相似度会把"小李""老李"合并成一个人，不可逆）
  2. 首字分桶 + 长词优先（1000 实体下 <3ms，且"老王家的狗"不会先命中"老王"）
"""

from __future__ import annotations

import time

from hippo.core.prescreen import EntityIndex, prescreen


def test_alias_hits_canonical():
    """手册验收第 2 条：别名"王哥"能命中"老王"。

    注意这个"能命中"的前提是 aliases 里登记过"王哥"，
    不是靠字符相似度算出来的——见 test_no_fuzzy_collision。
    """
    idx = EntityIndex([("老王", 1), ("王哥", 1)])
    res = prescreen("王哥最近怎么样", entities=idx)
    assert res.passed and res.reason == "entity"
    assert res.matched == ("王哥",)


def test_no_fuzzy_collision():
    """不做编辑距离匹配：小李 ≠ 老李。

    这两个字在中文里是不同的人，合并之后所有记忆都会挂错人，
    而且没有任何办法拆回来。宁可漏，不可错。
    """
    idx = EntityIndex([("老李", 1)])
    assert not prescreen("小李最近怎么样", entities=idx).passed


def test_longest_match_wins():
    """同时存在"老王"和"老王家的狗"时，应该报告长的那个。"""
    idx = EntityIndex([("老王", 1), ("老王家的狗", 2)])
    res = prescreen("老王家的狗丢了", entities=idx)
    assert res.matched == ("老王家的狗",)


def test_single_char_names_ignored():
    """单字名（"李"）当实体会把"李子""行李"全命中，直接不收。"""
    idx = EntityIndex([("李", 1)])
    assert len(idx) == 0
    assert not prescreen("我买了两斤李子", entities=idx).passed


def test_punctuation_inside_text():
    idx = EntityIndex([("小张", 7)])
    assert prescreen("小张，你过来一下", entities=idx).matched == ("小张",)


def test_1000_entities_under_3ms():
    """手册验收第 1 条：1000 实体匹配 < 3ms。"""
    names = [(f"人物{i:04d}", i) for i in range(1000)]
    idx = EntityIndex(names)
    text = "今天开会讨论了很多事情，涉及到产品、技术和运营的方方面面，大家都说了不少。"
    prescreen(text, entities=idx)  # 预热
    t0 = time.perf_counter()
    for _ in range(100):
        prescreen(text, entities=idx)
    per = (time.perf_counter() - t0) / 100
    assert per < 0.003, f"{per * 1000:.3f}ms"


def test_unrelated_text_no_false_positive():
    """手册验收第 3 条：无关词不误命中。"""
    idx = EntityIndex([("杭州", 1), ("豆豆", 2), ("老王", 3)])
    for text in ["今天天气不错", "这个方案我觉得可以", "帮我写个总结"]:
        res = prescreen(text, entities=idx)
        assert res.reason != "entity", f"{text!r} 误命中 {res.matched}"


def test_index_from_store(store):
    """从库里加载实体时要展开 aliases。"""
    eid = store.upsert_entity(session="s1", canonical="老王", aliases=["王哥", "隔壁老王"])
    idx = EntityIndex.from_store(store, "s1")
    assert len(idx) == 3
    assert prescreen("隔壁老王来了", entities=idx).matched == ("隔壁老王",)
    assert idx.match("王哥")[0] == eid


def test_index_from_broken_store_is_empty():
    """库没迁移好时不该让预筛炸掉——聊天优先于记忆。"""
    class Broken:
        def list_entities(self, session):
            raise RuntimeError("no such table")

    idx = EntityIndex.from_store(Broken(), "s1")
    assert len(idx) == 0
    assert not prescreen("随便说点什么内容", entities=idx).passed


def test_empty_index_is_safe():
    idx = EntityIndex([])
    assert idx.match("任何文本") is None
    # 空索引不能影响别的分支："我对花生过敏" 靠 fact 规则照样该放
    assert prescreen("我对花生过敏", entities=idx).passed is True
