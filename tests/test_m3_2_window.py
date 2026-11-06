"""M3.2 会话切片验收。

切片的错误不会立刻显现：窗口切错，抽取器照样能跑，只是抽出来的
记忆时对时错。所以这里的用例全部拿"边界"开刀——正好 5 轮、正好超一 token。
"""

from __future__ import annotations

import pytest

from hippo.core.models import Turn
from hippo.core.turns import estimate_tokens, plan_windows, slice_windows


def _turns(rounds: int, text: str = "普通内容") -> list[Turn]:
    out: list[Turn] = []
    for _ in range(rounds):
        out.append(Turn(session="s", idx=len(out), role="user", content=text, ts=0))
        out.append(Turn(session="s", idx=len(out), role="assistant", content=text, ts=0))
    return out


def test_five_rounds_make_one_window():
    windows = slice_windows(_turns(5), session="s")
    assert len(windows) == 1
    w = windows[0]
    assert w.idxs == list(range(10))
    assert w.has_pending is False


def test_six_rounds_split_with_pending_tail():
    windows = slice_windows(_turns(6), session="s")
    assert len(windows) == 2
    assert windows[0].idxs == list(range(10))
    assert windows[1].idxs == [10, 11]
    assert windows[1].has_pending is True, "只有一轮的尾巴不该被立刻抽取"


def test_token_limit_splits_earlier():
    text = "啊" * 200          # ≈ 390 token / 条
    windows = slice_windows(_turns(5, text), session="s", max_tokens=1200)
    assert len(windows) >= 2, "超 token 没有提前切"
    assert windows[0].token_est >= 1200
    assert len(windows[0].idxs) < 10


def test_tail_ending_with_user_is_pending():
    """最后一条是 user：assistant 的回复还没落库，抽了会得到半轮对话。"""
    turns = _turns(2) + [Turn(session="s", idx=4, role="user", content="还在吗", ts=0)]
    w = slice_windows(turns, session="s")[0]
    assert w.has_pending is True


def test_consecutive_user_messages_never_create_incomplete_middle_window():
    turns = _turns(4) + [
        Turn(session="s", idx=8, role="user", content="第一条", ts=0),
        Turn(session="s", idx=9, role="user", content="补充", ts=0),
        Turn(session="s", idx=10, role="assistant", content="收到", ts=0),
        Turn(session="s", idx=11, role="user", content="下一轮", ts=0),
        Turn(session="s", idx=12, role="assistant", content="好", ts=0),
    ]
    windows = slice_windows(turns, session="s")
    assert [w.idxs for w in windows] == [list(range(11)), [11, 12]]
    assert all(w.complete for w in windows)


def test_empty_input():
    assert slice_windows([], session="s") == []


def test_window_text_format():
    turns = [
        Turn(session="s", idx=0, role="user", content="我叫阿岚", ts=0),
        Turn(session="s", idx=1, role="assistant", content="记住了", ts=0),
    ]
    w = slice_windows(turns, session="s")[0]
    assert w.text() == "user: 我叫阿岚\nassistant: 记住了"
    assert (w.start_idx, w.end_idx) == (0, 1)


# ---------------------------------------------------------------- 与库联动
def test_plan_windows_then_mark_extracted(store):
    store.add_turns("s", [("user", "一", 0), ("assistant", "答一", 0)] * 6)

    windows = plan_windows(store, "s")
    assert len(windows) == 2
    assert store.pending_turns("s")  # 还没消费

    store.mark_turns_extracted("s", windows[0].end_idx)
    remaining = plan_windows(store, "s")
    assert len(remaining) == 1
    assert remaining[0].idxs == [10, 11]

    store.mark_turns_extracted("s", remaining[0].end_idx)
    assert plan_windows(store, "s") == [], "已抽取的窗口被重复返回"


def test_pending_turns_is_ascending(store):
    store.add_turns("s", [("user", "a", 0), ("assistant", "b", 0)])
    store.add_turns("s", [("user", "c", 0), ("assistant", "d", 0)])
    idxs = [t.idx for t in store.pending_turns("s")]
    assert idxs == [0, 1, 2, 3]


def test_estimate_is_conservative():
    """估算必须偏高：低估会撞 400，高估只是窗口小一点。"""
    text = "这是一段中文"
    assert estimate_tokens(text) > len(text)      # 1 字 > 1 token
    assert estimate_tokens("abcdefgh") < len("abcdefgh") * 0.6
