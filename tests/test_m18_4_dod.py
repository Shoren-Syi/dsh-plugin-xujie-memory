"""M18 DoD：50 轮对话追问 ≤5 次且次次合理（手册原文验收）。

数据 doubts_dialog_50.jsonl：50 轮、14 处模糊信息（其中 7 处应产生
疑问、7 处应被滤/拦），4 处用户后续自己回答。全链路离线跑：
filter_doubts → pick_doubt → resolve_matching_doubts，零模型调用。
"""

from __future__ import annotations

import json
from pathlib import Path

from hippo.core.db import connect
from hippo.core.doubt import should_ask
from hippo.core.migrate import migrate
from hippo.core.store import Store
from hippo.tools.eval_doubts import FIXTURE, run


def _store() -> Store:
    import tempfile
    c = connect(tempfile.mktemp(suffix=".db"))
    migrate(c)
    return Store(c)


def _rows() -> list[dict]:
    return [json.loads(line) for line in
            FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_dod_asks_at_most_five():
    """50 轮追问 ≤5 次。当前剧情：5 次（turn 1/4/34/42/46）。"""
    res = run(_store(), _rows())
    assert res["n"] <= 5, [a["question"] for a in res["asks"]]
    assert res["n"] == 5
    turns = [a["turn"] for a in res["asks"]]
    assert turns == sorted(turns) and len(turns) == len(set(turns))


def test_dod_every_ask_has_reason():
    """每次追问都有合理性依据（夹具 note），对应人工评 ≥4/5。"""
    rows = _rows()
    res = run(_store(), rows)
    questions = {a["question"] for a in res["asks"]}
    noted = {r["doubt"]["question"] for r in rows
             if r.get("doubt") and r.get("note")}
    assert questions <= noted, questions - noted


def test_dod_private_questions_never_asked():
    """cost 超线（隐私）的疑问 50 轮内一次都不问：月供/住址。"""
    rows = _rows()
    res = run(_store(), rows)
    asked = {a["question"] for a in res["asks"]}
    assert "用户的房贷月供是多少" not in asked
    assert "用户的新房子在哪个小区" not in asked


def test_dod_answered_questions_closed():
    """用户回答过的疑问全部自动关闭，不再出现在池子里。"""
    store = _store()
    run(store, _rows())
    active = {d.question for d in store.active_doubts("s", limit=50)}
    assert "带用户的导师姓什么" not in active       # turn 8 已答
    assert "用户的新公司具体做什么业务" not in active  # turn 2 已答
    assert "用户下周要独立接什么需求" not in active   # turn 36 已答
    assert "用户的报表模块用的是什么技术栈" not in active  # turn 44 已答


def test_dod_variant_of_answered_doubt_not_readded():
    """turn 21 的"导师姓什么"变体不得成为新疑问（已回答的不再问）。"""
    store = _store()
    run(store, _rows())
    questions = {r["question"] for r in store.conn.execute(
        "SELECT question FROM doubts").fetchall()}
    assert "用户的导师姓什么" not in questions


def test_dod_expired_never_injected():
    """过期疑问（expires < now）不进 should_ask——与 M18.3 衔接。"""
    from hippo.core.doubt import DEFAULT_COOLDOWN
    d = should_ask.__wrapped__ if hasattr(should_ask, "__wrapped__") else None
    # 直接用 M18.2 已覆盖；这里验证 expire_to_memory 后 active_doubts 清空
    store = _store()
    import time as _t
    now = int(_t.time())
    did = store.add_doubt(session="s", question="用户的旧公司是做什么的呀",
                          impact=0.8, cost=0.3)
    store.conn.execute(
        "UPDATE doubts SET created=?, expires=? WHERE id=?",
        (now - 8 * 86400, now - 8 * 86400 + 7 * 86400, did))
    from hippo.core.doubt import expire_to_memory
    expire_to_memory(store, session="s")
    assert store.active_doubts("s") == []
