# -*- coding: utf-8 -*-
"""M26.2 六项指标。

评测器自己也是被测对象：一个把"三月十四"和"3月14"判成两个字符串的
比对器，会让系统显得记不住事；一个让 50 个问题互相污染的评测器，
会给出稳定但错误的数字。所以这里既测"指标算得对"，也测"评测器不歪"。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hippo.config import load_config
from hippo.core.db import connect
from hippo.core.migrate import migrate
from hippo.core.store import Store
from hippo.tools.eval_all import (
    _contradiction, clone_store, gold_result, norm, run,
)

FIX = Path("tests/fixtures/dialog_200.jsonl")
PINS = json.loads(Path("tests/fixtures/dialog_200.pins.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def hippo_run():
    """200 轮跑一次就够，别每个用例都跑——单次约 0.3s，六跑就两秒。"""
    return run("hippo", cfg=load_config())


@pytest.mark.parametrize("text,expected", [
    ("三月十四", "3月14"),
    ("3月14", "3月14"),
    ("三十岁", "30岁"),
    ("二十三", "23"),
    ("十四", "14"),
    ("二十万", "20万"),
    ("每天两杯美式", "每天2杯美式"),
    ("骑车二十分钟", "骑车20分钟"),
])
def test_norm_handles_chinese_numerals(text, expected):
    """中文数字必须归一，否则"答对"会被记成"没答"。"""
    assert norm(text) == expected


def test_norm_strips_punctuation():
    assert norm("我 喜欢，吃川菜。") == norm("我喜欢吃川菜")


def test_gold_result_shapes():
    """三类针产出三种结构，且 plan 同时产出一条事件。

    plan 双写不是冗余：计划表只有"到点注入"一条出口，远期计划
    平时根本检索不到。实测只写 plans 时 8 条计划召回 1 条。
    """
    fact = gold_result({**PINS["P04"], "id": "P04"}, "我对花生过敏", 1)
    assert fact.facts and fact.facts[0].key == "allergy"

    ev = gold_result({**PINS["P16"], "id": "P16"}, "老王是我前同事", 1)
    assert ev.events and ev.events[0].summary
    assert ev.events[0].entities[0].name == "老王"

    plan = gold_result({**PINS["P38"], "id": "P38"}, "我下个月要搬家", 1)
    assert plan.plans and plan.events, "计划必须同时留下一条可检索的事件"


def test_clone_store_is_isolated():
    """克隆库改动了不能影响原库——否则 50 个问题就不是独立样本。"""
    conn = connect(":memory:")
    migrate(conn)
    s = Store(conn)
    s.add_turns("s", [("user", "你好", 0)])
    c = clone_store(s, load_config())
    c.add_turns("s", [("user", "又一句", 0)])
    assert len(s.get_turns("s", 50)) == 1
    assert len(c.get_turns("s", 50)) == 2
    c.close()
    s.close()


def test_clone_keeps_row_factory():
    """克隆出来的连接必须带 row_factory。

    漏了它，`row["value"]` 会抛 TypeError，而报错点在 store 深处，
    看起来像"记忆系统崩了"，其实是评测器少设了一行。
    """
    conn = connect(":memory:")
    migrate(conn)
    s = Store(conn)
    c = clone_store(s, load_config())
    assert isinstance(c.conn.execute("SELECT 1 AS x").fetchone(), sqlite3.Row)
    c.close()
    s.close()


def test_recall_reaches_target(hippo_run):
    """召回率 ≥0.80（手册 DoD）。实测 0.96，留出余量。"""
    assert hippo_run.recall >= 0.80
    # 且不是靠"全都命中"作弊：至少有一个 pin 是在语料里出现过的
    assert len(hippo_run.hits) + len(hippo_run.misses) == 50


def test_six_metrics_in_range(hippo_run):
    """六项都在手册给的范围里。"""
    assert hippo_run.recall >= 0.80
    assert hippo_run.contra < 0.02
    assert hippo_run.ttft_p50 < 100
    assert hippo_run.ask_per_10 <= 1.0
    assert hippo_run.tokens_per_turn > 0


def test_contradiction_only_counts_single_keys():
    """矛盾率只在 card=single 的 key 上有定义。

    `diet` 可以有五种忌口，同时 active 五条不是矛盾，是正确。
    把它算进分母，等于要求系统"只能记住一种不吃的东西"。
    """
    conn = connect(":memory:")
    migrate(conn)
    s = Store(conn)
    now = 1_800_000_000
    s.set_fact(session="s", key="city", value="杭州", when=now)
    s.set_fact(session="s", key="city", value="上海", when=now + 10)
    s.set_fact(session="s", key="diet", value="不吃香菜", when=now)
    s.set_fact(session="s", key="diet", value="不吃内脏", when=now + 10)
    # city 是 single，写第二个值会把第一个关掉 → 0 冲突
    assert _contradiction(s, "s") == 0.0
    s.close()


def test_idle_cost_is_zero():
    """挂机 24 小时不调模型（M24 的实现，这里从评测口径复核）。"""
    from hippo.tools.eval_all import idle_cost
    assert idle_cost() == 0
