"""M25.3 只读约束：好奇能产生问题，不能产生动作。

这条是"不会闯祸"的物理保证——不是靠提示词里写一句"请不要改文件"，
是靠写路径上有一道闸：explore_mode 一开，任何 store 写方法都抛异常。
"""

from __future__ import annotations

import time

import pytest

from hippo.core.curiosity import (explore_scope, propose_check, run_explore,
                                  toolset)


def _writes(store) -> dict:
    """一份"想写库"的清单。漏一个，M25.3 的保证就漏一个口子。"""
    return {
        "add_event": lambda: store.add_event(session="s", raw="x"),
        "add_doubt": lambda: store.add_doubt(session="s", question="x？"),
        "add_plan": lambda: store.add_plan(session="s", due_at=0, content="x"),
        "add_turn": lambda: store.add_turn(session="s", role="user",
                                           content="x", ts=0),
        "add_curiosity": lambda: store.add_curiosity(
            session="s", question="x？", gap_kind="doubt", gap_ref="doubt:1",
            impact=1, urgency=1, novelty=1, priority=1),
        "set_fact": lambda: store.set_fact(session="s", key="居住城市", value="杭州"),
        "add_trait": lambda: store.add_trait(entity_id=1, trait="x", evidence=[1]),
        "upsert_entity": lambda: store.upsert_entity(session="s", canonical="x"),
        "set_state": lambda: store.set_state("s", "k", "v"),
        "mark_doubt_asked": lambda: store.mark_doubt_asked(1, 1),
        "set_pinned": lambda: store.set_pinned(1, True),
        "delete_event": lambda: store.delete_event(1),
        "upsert_affect": lambda: store.upsert_affect("s", joy=50),
        "add_ledger": lambda: store.add_ledger(session="s", action="x"),
    }


def test_write_blocked(store):
    with pytest.raises(PermissionError):
        with explore_scope(store):
            store.add_event(session="s", raw="偷写一条")


def test_all_write_paths_blocked(store):
    """清单上每一个写方法都必须被拦。漏一个 = 只读承诺作废。"""
    with explore_scope(store):
        for name, call in _writes(store).items():
            with pytest.raises(PermissionError):
                call()


def test_toolset_empty(store):
    """探索模式不给任何工具——不是"过滤掉写工具"，是一个都不给。"""
    assert toolset(explore=True) == ()
    assert "write_event" in toolset(explore=False)


def test_restore_after_scope(store):
    """退出之后必须恢复。恢复不了，一次探索就把插件变成只读。"""
    assert store.explore_mode is False
    with explore_scope(store):
        assert store.explore_mode is True
    assert store.explore_mode is False
    eid = store.add_event(session="s", raw="正常写入")
    assert eid > 0


def test_propose_check_records(store):
    """想查点库外的东西 → 先问用户，并留一条痕迹。"""
    now = int(time.time())
    text = propose_check(store, "s", "老王的公司最近是不是裁员了", now=now)

    assert "要我去看看吗" in text
    row = store.conn.execute(
        "SELECT question, context FROM doubts WHERE session = 's'").fetchone()
    assert row["context"] == "curiosity:check"
    assert row["question"].endswith("？")


def test_hundred_attempts(store):
    """压力测试：100 次越权，0 次成功，库里一行都不多。"""
    before = store.counts()["events"]
    calls = list(_writes(store).values())

    ok = 0
    with explore_scope(store):
        for i in range(100):
            try:
                calls[i % len(calls)]()
                ok += 1
            except PermissionError:
                pass
            except Exception:      # noqa: BLE001 - 别的异常也算没写进去
                pass
    assert ok == 0
    assert store.counts()["events"] == before


def test_run_explore_hands_empty_tools(store):
    """run_explore 里伸手要工具，只能拿到空元组；想写库直接炸。"""
    def _try(question, tools):
        assert tools == ()
        store.add_event(session="s", raw="顺手记一条")
        return "never"

    with pytest.raises(PermissionError):
        run_explore(store, "查一下", _try)
    assert store.counts()["events"] == 0
