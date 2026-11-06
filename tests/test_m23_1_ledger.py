# -*- coding: utf-8 -*-
"""M23.1 动作登记。

验收：自主动作 100% 登记 / 被动响应 0 条 / predicted 合法 / 不进 prompt。
"""
from __future__ import annotations

import math

from hippo.core.ledger import (
    ACTION_KINDS, KIND_EDIT, KIND_EXPLORE, KIND_MESSAGE, KIND_PROPOSE,
    clamp_predicted, detect_kind, looks_requested, register,
)
from hippo.core.pack import pack


def test_four_kinds_detected():
    """四类自主动作都认得出。这是"100% 登记"的前提。"""
    cases = {
        "把 config.yaml 改了": KIND_EDIT,
        "顺手改一下数据库设置": KIND_EDIT,
        "主动给用户发了条消息": KIND_MESSAGE,
        "建议用户换个方案": KIND_PROPOSE,
        "翻看了一下项目目录": KIND_EXPLORE,
    }
    for text, want in cases.items():
        assert detect_kind(text) == want, text
    assert set(ACTION_KINDS) == {KIND_EDIT, KIND_MESSAGE, KIND_PROPOSE, KIND_EXPLORE}


def test_autonomous_actions_registered(store):
    """验收 #1：自主动作 100% 登记，四类各一条。"""
    ids = []
    for text in ("翻了用户的工作目录", "改了配置文件", "建议用户早点睡",
                 "主动发了条晚安消息"):
        aid = register(store, "s", text, 0.3)
        assert aid, text
        ids.append(aid)
    rows = [store.get_ledger(a) for a in ids]
    assert all(r is not None and r.settled == 0 for r in rows)
    assert {r.action_kind for r in rows} == set(ACTION_KINDS)


def test_passive_requests_not_registered(store):
    """验收 #2：被动响应 0 条。

    用户说"帮我改一下配置"，AI 去改就是干活，不是自主判断。
    给本职工作记奖惩，等于替用户的行为打分。
    """
    text = "帮我改一下 config.yaml，把超时改成 30 秒"
    assert looks_requested(text)
    assert register(store, "s", "改了 config.yaml", 0.5, user_text=text) is None
    assert register(store, "s", "改了 config.yaml", 0.5,
                    user_text="麻烦你把这段代码整理一下") is None
    assert store.ledger_stats("s")["open"] == 0
    assert store.ledger_stats("s")["settled"] == 0


def test_chitchat_is_not_autonomous(store):
    """归不到四类动作的，不登记。"回复用户的问题"是本职。"""
    assert register(store, "s", "回答了用户关于快速排序的问题") is None
    assert register(store, "s", "", 0.5) is None
    assert register(store, "s", "   ", 0.5) is None


def test_predicted_clamped(store):
    """验收 #3：predicted 恒在 [-1, 1]。

    模型两种越界都见过：十分制（7 分）和裸越界（1.5 / -3）。
    """
    cases = [(7, 0.7), (-8, -0.8), (3, 0.3), (-3, -0.3),
             (1.5, 1.0), (-1.5, -1.0), (12, 1.0), (-15, -1.0),
             ("0.4", 0.4), ("abc", 0.0), (None, 0.0), (float("nan"), 0.0),
             (float("inf"), 0.0)]
    for raw, want in cases:
        got = clamp_predicted(raw)
        assert math.isclose(got, want, abs_tol=1e-9), (raw, got, want)
        assert -1.0 <= got <= 1.0

    aid = register(store, "s", "改了配置文件", 1.5)
    assert store.get_ledger(aid).predicted == 1.0


def test_numbers_never_reach_prompt(store):
    """验收 #4：账簿数字不进 prompt。

    不只是"pack.py 里不出现 predicted 这个词"——真的装配一遍，
    把成品的每个字节扫一遍。
    """
    for i in range(3):
        register(store, "s", f"主动改了第{i}个配置文件", 0.83, now=1_700_000_000 + i)
    out = pack(store, "s", "今天干点什么", now=__import__("datetime").datetime.now())
    text = out.text
    for word in ("predicted", "rpe", "0.83", "ledger", "账簿"):
        assert word not in text, word

    # 源码层面兜底：装配器根本不知道有这个字段
    src = __import__("pathlib").Path("hippo/core/pack.py").read_text(encoding="utf-8")
    assert "predicted" not in src
    assert "rpe" not in src


def test_ts_follows_conversation_time(store):
    """回填历史对话时，动作时刻必须跟着对话走，不能跟墙钟。

    这不是洁癖：3 天过期线按 ts 算。用墙钟的话，补录三个月前的
    对话时所有历史动作都"刚刚发生"，永远不会过期，然后被今天的
    一句抱怨批量结算——凭空造教训。
    """
    old = 1_600_000_000
    aid = register(store, "s", "改了配置文件", 0.2, now=old)
    assert store.get_ledger(aid).ts == old
    expired = store.expire_ledger("s", now=old + 4 * 86400)
    assert expired == [aid]
    assert store.get_ledger(aid).settled == -1
