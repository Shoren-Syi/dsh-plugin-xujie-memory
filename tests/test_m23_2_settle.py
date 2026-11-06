# -*- coding: utf-8 -*-
"""M23.2 反馈识别与结算。

验收：10 组结算全对 / 过期不猜 / 只结算 1 条 / RPE 符号正确。
"""
from __future__ import annotations

import json

import pytest

from hippo.core.ledger import register, settle
from hippo.core.persist import persist_result
from hippo.core.schema import ExtractResult, parse_result
from hippo.core.store import StoreError
from hippo.core.turns import slice_windows

DAY = 86400
T0 = 1_760_000_000


def _reg(store, action, predicted, kind="", ts=T0, user_text=""):
    return register(store, "s", action, predicted, kind=kind,
                    now=ts, user_text=user_text)


def _row(store, aid):
    return store.get_ledger(aid)


def test_negative_feedback_settles(store):
    """验收 #4 的前半：被骂 → actual=-1、rpe 为负、settled=1。"""
    aid = _reg(store, "改了配置文件", 0.8)
    st = settle(store, "s", "你怎么又擅自改我的配置，下次先问我", -1.0, now=T0 + 3600)
    assert st is not None and st.action_id == aid
    assert st.actual == -1.0
    assert st.rpe == pytest.approx(-1.8)
    assert st.rpe < 0
    r = _row(store, aid)
    assert r.settled == 1 and r.actual == -1.0
    assert r.settled_at == T0 + 3600


def test_positive_feedback(store):
    """表扬 → actual=+1，rpe 为正。"""
    aid = _reg(store, "给用户发了条消息", 0.2)
    st = settle(store, "s", "谢谢你提醒我吃药，很及时", 1.0, now=T0 + 60)
    assert st.rpe == pytest.approx(0.8)
    assert _row(store, aid).settled == 1


def test_pick_most_relevant(store):
    """验收 #1/#3：两条未结算，反馈只指向其中一条。

    "改配置"被骂，"发消息"没被提。若按时间取最近会结算错那条——
    这正是"选相关度不选最近"存在的理由。
    """
    edit = _reg(store, "改了配置文件", 0.5, ts=T0)
    msg = _reg(store, "给用户发了条消息", 0.5, ts=T0 + 60)   # 更像"最近"
    st = settle(store, "s", "谁让你改我配置的，下次先问", -1.0, now=T0 + 3600)
    assert st.action_id == edit
    assert _row(store, edit).settled == 1
    assert _row(store, msg).settled == 0        # 验收 #3：只结算 1 条


def test_pick_latest_when_nothing_matches(store):
    """相关度全为 0 → 退化成"取最近那条"（手册兜底规则）。"""
    old = _reg(store, "翻了项目目录", 0.1, ts=T0)
    new = _reg(store, "给用户发了条消息", 0.1, ts=T0 + 600)
    st = settle(store, "s", "行吧", -1.0, now=T0 + 3600)
    assert st.action_id == new
    assert _row(store, old).settled == 0


def test_expired_not_settled(store):
    """验收 #2：超过 3 天没反馈 → 作废，不猜。

    这一步必须在"选候选"之前做。反过来的话，三天前的动作还在候选里，
    今天一句无关的抱怨就会把它结算成 -1——凭空造一条教训。
    """
    aid = _reg(store, "改了配置文件", 0.5, ts=T0)
    st = settle(store, "s", "今天真烦", -1.0, now=T0 + 4 * DAY)
    assert st is None
    r = _row(store, aid)
    assert r.settled == -1 and r.actual is None and r.rpe is None
    assert r.settled_at == T0 + 4 * DAY


def test_window_edge_still_settles(store):
    """差 1 分钟到 3 天：还在窗口内，照常结算。

    边界不测的话，"3 天"要么变成 2 天 23 小时 59 分，要么变成 4 天，
    而两种错都不会报错。
    """
    aid = _reg(store, "改了配置文件", 0.5, ts=T0)
    st = settle(store, "s", "改我配置干嘛", -1.0, now=T0 + 3 * DAY - 60)
    assert st is not None and _row(store, aid).settled == 1


def test_double_settle_rejected(store):
    """同一个 action 不许结算两次。

    用户连骂两句是常态。第二句若也结算，RPE 被算两遍，
    懊悔记忆重复成两条——"我明明记过一次了"变成两段重复的回声。
    """
    aid = _reg(store, "改了配置文件", 0.5)
    settle(store, "s", "别改我配置", -1.0, now=T0 + 60)
    assert settle(store, "s", "真的很烦", -1.0, now=T0 + 120) is None
    with pytest.raises(StoreError):
        store.settle_ledger(aid, -1.0, now=T0 + 180)


def test_rpe_is_actual_minus_predicted(store):
    """RPE 口径：actual - predicted。

    自己本来就不看好的事做砸了，RPE 接近 0——不该懊悔，
    因为"我早就知道会这样"。反过来自我感觉良好却翻车的，
    RPE 最负，才是最该记住的那种。
    """
    a = _reg(store, "改了配置A", -0.9, ts=T0)
    b = _reg(store, "改了配置B", 0.9, ts=T0 + 10)
    sa = settle(store, "s", "配置A那事我早说了不行", -1.0, now=T0 + 60)
    sb = settle(store, "s", "配置B呢", -1.0, now=T0 + 120)
    assert sa.rpe == pytest.approx(-0.1) and not sa.is_regret
    assert sb.rpe == pytest.approx(-1.9) and sb.is_regret
    assert _row(store, a).settled == 1 and _row(store, b).settled == 1


# ---------------------------------------------------------------- persist 接线
def _window(store, texts, ts=T0):
    items = []
    for t in texts:
        items.append(("user", t, 0))
        items.append(("assistant", "嗯。", 0))
    store.add_turns("s", items, ts=ts)
    return slice_windows(store.pending_turns("s"), session="s", per_window=5)[0]


def test_persist_settles_from_extract(store):
    """端到端：抽取结果带 neg_feedback，persist 一个事务里顺手结算。"""
    aid = _reg(store, "改了配置文件", 0.7, ts=T0 - DAY)
    w = _window(store, ["你怎么擅自改我配置，下次先问我"], ts=T0)
    r: ExtractResult = parse_result(json.dumps({
        "events": [{"raw": "你怎么擅自改我配置，下次先问我",
                    "summary": "用户不满 AI 擅自改配置",
                    "keywords": ["配置", "擅自"], "arousal": 0.7}],
        "neg_feedback": "你怎么擅自改我配置，下次先问我",
    }, ensure_ascii=False))
    st = persist_result(store, "s", w, r)
    assert st.ledger_settled == 1
    assert _row(store, aid).settled == 1
    assert _row(store, aid).actual == -1.0


def test_persist_no_feedback_keeps_ledger_open(store):
    """没有反馈字段（老版本模型）→ 账簿原样不动，不报错。

    兼容性要求：模型少给字段是常态，缺字段必须等价于"没表态"，
    而不是"表态了但内容为空"。
    """
    aid = _reg(store, "改了配置文件", 0.7, ts=T0 - DAY)
    w = _window(store, ["今天天气不错"], ts=T0)
    r = parse_result(json.dumps({
        "events": [{"raw": "今天天气不错", "summary": "天气不错",
                    "keywords": ["天气"], "arousal": 0.1}],
    }, ensure_ascii=False))
    st = persist_result(store, "s", w, r)
    assert st.ledger_settled == 0
    assert _row(store, aid).settled == 0


def test_feedback_only_result_is_not_empty():
    """只有反馈、没有事件的结果不算空——否则抽取器会把它当"没内容"丢掉。"""
    r = parse_result('{"neg_feedback": "别乱动我东西"}')
    assert not r.is_empty()
    assert len(r.neg_feedback) == 6
