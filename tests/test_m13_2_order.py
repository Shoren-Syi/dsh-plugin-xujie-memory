"""M13.2 验收：注入格式与顺序。

顺序在这里不是排版问题，是**成本问题**。DeepSeek 按前缀字节匹配缓存，
时间锚往前挪一行，缓存命中率就从 70% 掉到 0，成本翻十倍。

所以这一节的测试基本都是"守位置"的：谁在第一行、谁在最后一行、
分隔符是不是固定的那三个字符。
"""

from __future__ import annotations

import re
from datetime import datetime

from hippo.core.pack import TOTAL_BUDGET, pack

NOW = datetime(2026, 11, 5, 14, 0)


def _ev(store, raw, keywords="", summary="", tier=2):
    return store.add_event(session="s1", raw=raw, keywords=keywords,
                           summary=summary or raw, tier=tier,
                           event_time=int(datetime(2026, 11, 4).timestamp()))


def _pack(store, text="我搬家了"):
    return pack(store, "s1", text, now=NOW)


def _sections(text: str) -> list[str]:
    return [s for s in text.split("\n---\n") if s.strip()]


def test_six_sections_in_order(store):
    _ev(store, "我搬家了", keywords="搬家")
    r = _pack(store)
    secs = _sections(r.text)
    assert secs[0].startswith("[ctx-v")
    assert "我是谁" in secs[0] or "记得住事" in secs[0]
    assert secs[-1].startswith("[现在 ")
    assert len(secs) <= 6


def test_anchor_is_very_last(store):
    """时间锚必须是最后一行。往前挪一行，缓存就全废——
    这条测试是专门防后来人"顺手"重排的。"""
    _ev(store, "我搬家了", keywords="搬家")
    r = _pack(store)
    last = r.text.rstrip().split("\n")[-1]
    assert re.fullmatch(r"\[现在 \d{4}-\d{2}-\d{2} \d{2}:\d{2}\]", last), last


def test_anchor_text_shape(store):
    r = _pack(store)
    assert re.fullmatch(r"\[现在 \d{4}-\d{2}-\d{2} \d{2}:\d{2}\]",
                        r.by_name["anchor"].text)


def test_separator_is_fixed(store):
    _ev(store, "我搬家了", keywords="搬家")
    r = _pack(store)
    assert "\n---\n" in r.text
    assert "\n\n---\n" not in r.text, "分隔符只能是固定三个字符"
    assert "---\n\n" not in r.text


def test_no_trailing_blank_lines(store):
    _ev(store, "我搬家了", keywords="搬家")
    text = _pack(store).text
    assert text == text.strip()
    assert "\n\n\n" not in text


def test_no_random_blank_lines(store):
    """字节级一致的前提：不能有"看起来一样但字节不同"的空行。"""
    _ev(store, "我搬家了", keywords="搬家")
    a, b = _pack(store).text, _pack(store).text
    assert a == b
    assert a.count("\n\n") == b.count("\n\n")


def test_version_stable_without_change(store):
    """内容不变，版本号 10 轮不变。"""
    _ev(store, "我搬家了", keywords="搬家")
    vs = set()
    for _ in range(10):
        vs.add(re.match(r"\[ctx-v(\w{4})\]", _pack(store).text).group(1))
    assert len(vs) == 1


def test_version_changes_with_card(store):
    eid = store.upsert_entity(session="s1", canonical="老王", type="person")
    ev = _ev(store, "素材", keywords="素材")
    v1 = re.match(r"\[ctx-v(\w{4})\]", _pack(store).text).group(1)
    store.add_trait(entity_id=eid, trait="怕坐飞机", evidence=[ev],
                    status="observed", confidence=0.8, slot="fear")
    v2 = re.match(r"\[ctx-v(\w{4})\]", _pack(store).text).group(1)
    assert v1 != v2, "角色卡变了版本号必须变，否则观测不到缓存失效点"


def test_version_ignores_memory_block(store):
    """D 段每轮都变，但它不算进版本号——算进去的话版本号每轮都变，
    版本号就没法用来观测"稳定段什么时候被改动了"。"""
    _ev(store, "我搬家了", keywords="搬家 海淀")
    _ev(store, "猫不吃东西", keywords="猫 年糕")
    v1 = re.match(r"\[ctx-v(\w{4})\]", _pack(store, "我搬家了").text).group(1)
    v2 = re.match(r"\[ctx-v(\w{4})\]", _pack(store, "猫最近怎么样").text).group(1)
    assert v1 == v2


def test_total_within_budget(store):
    _ev(store, "我搬家了", keywords="搬家")
    assert _pack(store).total_tokens <= TOTAL_BUDGET
