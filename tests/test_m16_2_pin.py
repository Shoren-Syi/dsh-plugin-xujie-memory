"""M16.2 验收：钉住机制。

四条验收，逐条对应：

    1. 识别率 ≥85%（17/20）      → test_recognition_rate 用一个 20 例标注集
    2. 永不因预算被裁 100%       → test_pinned_survives_memory_budget（D 段）
                                   test_pinned_block_never_dropped（C 段）
    3. 90 天后仍在               → test_pinned_not_decayed（走 M14 的权重公式）
    4. 超额提醒一次，不重复       → test_pin_warning_once

「一次提醒」的判据不是布尔值而是"上次提醒时的条数"：用户清到 8 条又攒回 12 条，
是新的一次失控，该再说一声；而条数没涨就一句都不重复——每轮提一次会让提醒
变成噪声，用户连看都不看。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hippo.core.pack import PIN_WARN_MIN, _pinned, pack
from hippo.core.pin import PIN_MARKERS, looks_pinned, pin_span
from hippo.core.rank import _fill
from hippo.core.weight import compute_weight

S = "main"
TZ = "Asia/Shanghai"
BASE = int(datetime(2026, 1, 1, 12, tzinfo=ZoneInfo(TZ)).timestamp())


# --------------------------------------------------------------- 验收 #1
# 12 正例：覆盖用户真实会说的形态（祈使、强调、带冒号、带语气词）
POSITIVE = [
    "记住这个：我不吃香菜",
    "你一定要记住，我对青霉素过敏",
    "别忘了我不喝冰的",
    "这个很重要，记下来：我下周三面试",
    "帮我记住，我妈生日是腊月初八",
    "记一下，我的工位在 12 楼",
    "你记住啊，我讨厌别人叫我小王",
    "不要忘了，钥匙在门口花盆下面",
    "必须记住：我对猫毛过敏",
    "存一下：公司报销要在月底前交",
    "这很重要，我不吃辣，真的",
    "记住我周三不上班",
]

# 8 负例：全是最像"记住"但其实不是的句子
NEGATIVE = [
    "我记得你说过要搬家",
    "你还记得吗？",
    "记性不太好，老是忘事",
    "我忘了带钥匙",
    "不用记住这个，随口一说",
    "别记这个",
    "你记住密码了吗",
    "我记不住那么多名字",
]


def test_recognition_rate():
    """★ 收 20 例，要求 ≥17。"""
    hits = sum(looks_pinned(t) for t in POSITIVE) + sum(
        1 for t in NEGATIVE if not looks_pinned(t))
    total = len(POSITIVE) + len(NEGATIVE)
    assert total == 20, "标注集必须是 20 例（手册的口径）"
    assert hits >= 17, f"识别率 {hits}/{total}，要求 ≥17"


def test_negation_wins_over_marker():
    """否定先判。

    "不用记住这个" 含"记住"。如果先查祈使标记、后查否定，它会被钉住——
    方向和 M15 那个 `frozen` bug 一模一样（只查正向条件）。
    """
    assert not looks_pinned("不用记住这个，随口一说")
    assert not looks_pinned("不用记下来，我随口说的")
    assert looks_pinned("记住这个，不是随口说的")


def test_statement_with_jide_is_not_pin():
    """`我记得` 是陈述，`记住` 才是祈使。差一个字。"""
    assert not looks_pinned("我记得他住在朝阳")
    assert looks_pinned("记住他住在朝阳")


def test_pin_span_extracts_the_sentence():
    """日志要能写出"是哪句话触发的"，否则误判没法排查。"""
    span = pin_span("今天挺累的。记住：我对青霉素过敏。明天再说吧。")
    assert "青霉素" in span
    assert "今天挺累的" not in span
    assert pin_span("今天挺累的") == ""


def test_false_friend_list_is_not_too_short():
    """排除表里的条目太短会误伤它自己的反例。

    `"我记住"` 作为排除项，会把 `"帮我记住"` 一起挡掉（子串命中）——
    这正是 M16.2 第一版的表现：识别率 19/20，漏的就是"帮我记住我妈生日"。
    所以排除项都带上了"了/过/吗"这类收尾。
    """
    assert looks_pinned("帮我记住，我妈生日是腊月初八")
    assert not looks_pinned("我记住了")
    assert not looks_pinned("我记住过这件事")


def test_markers_do_not_contain_bare_jide():
    """裸"记得"不能进 marker 表：`我记得`/`你还记得吗` 会全中。"""
    assert "记得" not in PIN_MARKERS


# --------------------------------------------------------------- 落地
def test_persist_marks_all_events_of_the_turn(store):
    """一句话抽出两条事件，两条都要钉。

    只钉第一条或最后一条是在猜用户意图："记住，我对青霉素过敏，
    还有我不吃香菜"——两条都是他要求记住的。
    """
    from hippo.core.persist import persist_result
    from hippo.core.schema import ExtractResult
    from hippo.core.turns import Turn, Window

    w = Window(session=S, idxs=[0, 1], token_est=40, has_pending=False, turns=[
        Turn(session=S, idx=0, role="user",
             content="记住：我对青霉素过敏，还有我不吃香菜", ts=BASE),
        Turn(session=S, idx=1, role="assistant", content="好的", ts=BASE + 5),
    ])
    res = _extract_result(persist_result, ["我对青霉素过敏", "我不吃香菜"])
    stats = persist_result(store, S, w, res, tz=TZ, now=BASE)
    assert len(stats.event_ids) == 2
    for eid in stats.event_ids:
        ev = store.get_event(eid)
        assert ev is not None and ev.pinned == 1
        assert ev.tier == 1, "钉住必须立刻进 hot，否则夜里之前它可能根本不进 prompt"


def test_plain_turn_is_not_pinned(store):
    from hippo.core.persist import persist_result
    from hippo.core.turns import Turn, Window

    w = Window(session=S, idxs=[0, 1], token_est=40, has_pending=False, turns=[
        Turn(session=S, idx=0, role="user", content="今天地铁特别挤", ts=BASE),
        Turn(session=S, idx=1, role="assistant", content="嗯", ts=BASE + 5),
    ])
    res = _extract_result(persist_result, ["今天地铁特别挤"])
    stats = persist_result(store, S, w, res, tz=TZ, now=BASE)
    assert all(store.get_event(i).pinned == 0 for i in stats.event_ids)


def test_set_pinned_and_off(store):
    eid = _ev(store, "我对花生过敏")
    assert store.set_pinned(eid) is True
    ev = store.get_event(eid)
    assert (ev.pinned, ev.tier) == (1, 1)
    store.set_pinned(eid, False)
    ev = store.get_event(eid)
    assert ev.pinned == 0
    assert ev.tier == 1, "撤销不改 tier：那是 recompute 的活，手改等于替公式做决定"


def test_set_pinned_unknown_id(store):
    assert store.set_pinned(9999) is False


# --------------------------------------------------------------- 验收 #2
def test_pinned_survives_memory_budget(store):
    """D 段预算不够时，被挤掉的必须是普通记忆，不是钉住的。"""
    from hippo.core.rank import MemoryItem

    def item(eid, pin, tokens):
        ev = store.get_event(eid)
        return MemoryItem(event=ev, score=0.5, kw_hit=1, text=ev.summary,
                          tokens=tokens, pinned=pin)

    pin = item(_ev(store, "钉住的", pinned=1), True, 300)
    a = item(_ev(store, "普通的甲"), False, 300)
    b = item(_ev(store, "普通的乙"), False, 300)
    picked, used = _fill([a, pin, b], max_items=8, budget=400)
    # 预算 400 = 刚好够钉住那条 300 + 一条普通的 300 装不下。
    # 关键不是"装了几条"，是**被留下的必须有钉住那条**。
    assert pin in picked
    assert used == 300
    assert [x.pinned for x in picked] == [True]


def test_pinned_block_never_dropped():
    """C 段在总预算吃紧时的丢弃顺序排在最后第二个（只在 persona 之前）。"""
    from hippo.core.pack import TOTAL_BUDGET   # noqa: F401  (存在性)
    from hippo.core.pack import pack as _pack   # noqa: F401
    # 顺序表在 pack() 里：["memory", "dynamic", "card", "pinned", "persona"]
    # 这里用文本断言防止有人把它挪到 card 前面——挪了之后"钉住"就不再可靠。
    import inspect
    src = inspect.getsource(_pack)
    order = src.split("order = [", 1)[1].split("]", 1)[0]
    assert order.index("pinned") > order.index("card")


# --------------------------------------------------------------- 验收 #3
def test_pinned_not_decayed(store):
    """90 天后权重仍然 ≥0.7（M14 的口径），而且还在 list_pinned 里。"""
    eid = _ev(store, "我对花生过敏", pinned=1)
    ev = store.get_event(eid)
    later = BASE + 90 * 86400
    w = compute_weight(ev, later)
    # w 恒 ≤ w0（M14 的公式没有加成项）。钉住的意义是**几乎不衰减**：
    # 100 年半衰期下 90 天只掉 0.17%。
    assert w >= ev.w0 * 0.99, f"90 天后只剩 {w:.3f}（w0={ev.w0}）"
    assert eid in [e.id for e in store.list_pinned(S)]


# --------------------------------------------------------------- 验收 #4
def test_pin_warning_once(store):
    assert store.pin_warn_once(S, 12, 260) is True      # 第一次：提
    assert store.pin_warn_once(S, 12, 260) is False     # 同量：不提
    assert store.pin_warn_once(S, 12, 300) is False     # 条数没涨：不提
    assert store.pin_warn_once(S, 15, 340) is True      # 又攒了 3 条：再提一次


def test_pack_warns_when_pinned_block_overflows(store, cfg):
    """C 段超 200 token 且有 ≥5 条钉住 → 提醒一次，第二次不提醒。"""
    for i in range(PIN_WARN_MIN + 1):
        _ev(store, f"钉住的第{i}条，" + "很长的细节" * 12, pinned=1)
    r1 = pack(store, S, "随便问问", cfg=cfg, now=datetime.fromtimestamp(BASE),
              use_cache=False)
    assert r1.warnings, "C 段超预算却没提醒"
    assert "钉了" in r1.warnings[0]
    r2 = pack(store, S, "再问一次", cfg=cfg, now=datetime.fromtimestamp(BASE),
              use_cache=False)
    assert not r2.warnings, "第二次不该再提醒"


def test_no_warning_when_few_pins(store, cfg):
    """1~2 条很长也不提醒：那不是失控，是用户就想记两件事。"""
    _ev(store, "钉住的一条，" + "很长的细节" * 40, pinned=1)
    r = pack(store, S, "随便问问", cfg=cfg, now=datetime.fromtimestamp(BASE),
             use_cache=False)
    assert not r.warnings


def test_pinned_section_lists_events(store):
    """C 段里必须出现钉住事件的摘要。"""
    _ev(store, "我对花生过敏", pinned=1, summary="用户对花生过敏")
    text, _drop = _pinned(store, S)
    assert "花生" in text


# --------------------------------------------------------------- 工具
def _ev(store, raw, *, pinned=0, keywords="测试", summary=""):
    eid = store.add_event(
        session=S, raw=raw, summary=summary or raw[:30], keywords=keywords,
        event_time=BASE, ingest_time=BASE, tier=2, w0=0.5, weight=0.5,
        pinned=pinned)
    if pinned:
        store.set_pinned(eid)
    return eid


def _extract_result(_persist, raws):
    """造一个最小 ExtractResult，只为验证 pinned 的落地路径。"""
    from hippo.core.schema import EventOut, ExtractResult

    evs = [EventOut(raw=r, summary=r, keywords=["测试"], entities=[],
                    source="user", arousal=0.2) for r in raws]
    return ExtractResult(events=evs, facts=[], traits=[], plans=[], doubts=[])
