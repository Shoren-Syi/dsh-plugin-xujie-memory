# -*- coding: utf-8 -*-
"""M22.3 记录与能力边界。验收：全部记录 / 可聚合 / 重复提醒一次 /
不进常规 pack。
"""
import pytest

from hippo.core.giveup import Giveup, format_stats, parse_tag, record


def _g(level="decline", reason="缺少文件权限") -> Giveup:
    return parse_tag(f'<giveup level="{level}" reason="{reason}"/>')


def test_records_every_giveup(store):
    """验收 #1：全部记录。三条不同 level 都要落库。"""
    record(store, "s", _g("decline", "缺少文件权限"), "读这个文件")
    record(store, "s", _g("ask_help", "需要确认目标格式"), "导出报表")
    record(store, "s", _g("partial", "只做了一半"), "翻译全文")
    rows = store.giveup_stats(session="s")
    assert len(rows) == 3
    assert {r["level"] for r in rows} == {"decline", "ask_help", "partial"}


def test_event_shape(store):
    """事件契约：kind=giveup / tier=3(cold) / level 进专列 / summary 格式。"""
    eid, _ = record(store, "s", _g(), "读这个文件")
    e = store.get_event(eid)
    assert e.kind == "giveup"
    assert e.tier == 3
    assert e.giveup_level == "decline"
    assert e.summary == "做不了：读这个文件 / 原因：缺少文件权限"
    assert e.source == "ai"


def test_aggregate_top10(store):
    """验收 #2：可聚合。同 reason 重复 5 次排第一，top 限制生效。"""
    for i in range(5):
        record(store, "s", _g("decline", "缺少文件权限"), f"读文件{i}")
    for i in range(3):
        record(store, "s", _g("ask_help", "需要确认目标格式"), f"导出{i}")
    for i in range(2):
        record(store, "s", _g("partial", "只做一半"), f"翻译{i}")
    rows = store.giveup_stats(session="s")
    assert rows[0]["reason"] == "缺少文件权限" and rows[0]["n"] == 5
    assert rows[1]["n"] == 3
    top2 = store.giveup_stats(session="s", top=2)
    assert len(top2) == 2


def test_warn_once_at_five(store):
    """验收 #3：同 reason ≥5 次提醒一次，第二次不再提。"""
    warns = []
    for i in range(6):
        _, w = record(store, "s", _g(), f"读文件{i}")
        if w:
            warns.append(w)
    assert len(warns) == 1
    assert "5 次" in warns[0] and "缺少文件权限" in warns[0]


def test_no_warn_below_five(store):
    for i in range(4):
        _, w = record(store, "s", _g(), f"读文件{i}")
        assert w == ""


def test_different_reasons_warn_independently(store):
    """不同 reason 各自计数、各自提醒一次。"""
    for i in range(5):
        record(store, "s", _g("decline", "缺少文件权限"), f"a{i}")
    _, w = record(store, "s", _g("ask_help", "需要确认格式"), "b")
    assert w == ""                    # 新 reason 才 1 次
    for i in range(4):
        _, w = record(store, "s", _g("ask_help", "需要确认格式"), f"b{i}")
    assert "需要格式" in w or "确认格式" in w


def test_not_in_retrieval(store):
    """验收 #4：不进常规检索（_EXCLUDE_KINDS）。"""
    record(store, "s", _g("decline", "缺少文件权限"), "读这个文件")
    from hippo.core.rank import l2_search
    res = l2_search(store, "s", "读文件 缺少文件权限")
    assert all("弃权" not in (i.summary or "") for i in res.items)


def test_not_in_pack(store):
    """不进常规 pack：建了 giveup 记录后 pack 里不出现其文案。"""
    record(store, "s1", _g("decline", "缺少文件权限"), "读这个文件")
    from hippo.core.pack import pack
    res = pack(store, "s1", "帮我读个文件")
    assert "缺少文件权限" not in res.text
    assert "做不了" not in res.text


def test_format_stats():
    rows = [{"level": "decline", "reason": "缺少文件权限", "n": 5},
            {"level": "ask_help", "reason": "需要确认目标格式", "n": 3}]
    assert format_stats(rows) == ("decline: 缺少文件权限 (5)\n"
                                 "ask_help: 需要确认目标格式 (3)")
    assert "还没有弃权记录" in format_stats([])


def test_cli_stats_giveup(store, tmp_path, capsys, monkeypatch):
    """CLI 端到端：hippo stats --giveup 打得出来。"""
    record(store, "s", _g(), "读这个文件")
    from hippo import cli
    args = type("A", (), {
        "db": str(store.conn.execute("PRAGMA database_list").fetchone()[2]),
        "config": "", "session": "", "disk": False, "giveup": True,
    })()
    monkeypatch.setattr(cli, "_db_path", lambda a: __import__("pathlib").Path(a.db))
    monkeypatch.setattr(cli, "_config_for", lambda a: None)
    cli.cmd_stats(args)
    out = capsys.readouterr().out
    assert "能力边界" in out and "缺少文件权限" in out
