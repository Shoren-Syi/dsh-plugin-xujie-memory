# -*- coding: utf-8 -*-
"""M39.2 验收：被动纠正挖掘。

原则：插件能自己修的别让模型插手；必须让模型"反思"的部分，也要**绝对无感**——
不调模型、不改提示词、不加延迟。所以这里测的是"本地能不能可靠认出旧值→新值"，
以及"宁可少改，不可改错"的边界。
"""
from __future__ import annotations

from hippo.config import Config
from hippo.core.reflect import Correction, apply_corrections, parse_correction


def test_parse_not_but_is():
    assert parse_correction("不对，不是上海，是北京") == [Correction("上海", "北京")]


def test_parse_change_to():
    got = parse_correction("把复查时间改成十四号")
    assert got and got[0].new == "十四号"


def test_parse_is_not_word_order():
    got = parse_correction("是杭州不是苏州")
    assert got and got[0] == Correction("苏州", "杭州")


def test_parse_ignores_emotional_correction():
    # 纯情绪/评价不是纠正：不能把"不好"当旧值去改库
    for text in ("不对，我觉得不行", "其实还好吧", "算了"):
        assert parse_correction(text) == []


def test_parse_ignores_self_reference():
    assert parse_correction("不是我，是他") == []


def test_apply_only_updates_existing_value(store):
    cfg = Config()
    store.set_fact(session="s", key="city", value="上海", when=100)
    applied = apply_corrections(store, "s", [Correction("上海", "北京")], cfg=cfg, now=1000)
    assert applied and applied[0]["key"] == "city"
    vals = [r[0] for r in store.conn.execute(
        "select value from facts where session='s' and key='city' and valid_to is null")]
    assert vals == ["北京"]


def test_apply_does_not_invent_new_facts(store):
    cfg = Config()
    store.set_fact(session="s", key="city", value="上海", when=100)
    # 库里没有"苏州"，不能凭空写一条
    applied = apply_corrections(store, "s", [Correction("苏州", "南京")], cfg=cfg, now=1000)
    assert applied == []
    n = store.conn.execute("select count(*) from facts where session='s'").fetchone()[0]
    assert n == 1


def test_daily_limit(store):
    cfg = Config()
    assert cfg.reflect.max_passive_per_day == 3
    for key, value in (("city", "上海"), ("hometown", "宁波"), ("employer", "甲公司"),
                       ("school", "乙大学")):
        store.set_fact(session="s", key=key, value=value, when=100)
    pairs = [Correction("上海", "北京"), Correction("宁波", "杭州"),
             Correction("甲公司", "乙公司"), Correction("乙大学", "丙大学")]
    applied = apply_corrections(store, "s", pairs, cfg=cfg, now=1000)
    assert len(applied) == 3, "每天最多应用 3 条"
    # 同一天再来一条，应该被限额挡住
    again = apply_corrections(store, "s", [Correction("乙大学", "丙大学")], cfg=cfg, now=2000)
    assert again == []
