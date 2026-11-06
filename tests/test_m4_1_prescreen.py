"""M4.1 规则预筛。

这里的用例分两类：
  1. 行为用例——每条规则一个断言，改词表时立刻知道破坏了什么
  2. 标定用例——P/R/放行率/延迟的护栏，防止有人为了修一条漏报把整体搞崩

第 2 类特别重要。没有它，调词表就是纯手感：修好一条，悄悄弄坏十条，没人发现。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hippo.core.prescreen import (
    EntityIndex,
    PrescreenResult,
    RuleSet,
    load_rules,
    prescreen,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- 基础行为
def test_result_is_truthy_when_passed():
    r = prescreen("我喜欢吃辣")
    assert isinstance(r, PrescreenResult)
    assert r.passed and bool(r) is True


def test_greetings_are_blocked():
    """手册验收第 2 条：寒暄 0 误判。这五条是最容易漏的（长度都够）。"""
    for text in ["嗯", "好的", "哈哈哈哈哈哈", "收到收到", "嗯嗯，明白了"]:
        assert not prescreen(text).passed, f"{text!r} 不该放行"


def test_plan_passes_even_when_short():
    """计划句豁免长度限制："8点叫我" 只有 4 个字，但价值最高。"""
    for text in ["8点叫我", "明天8点叫我起来干活", "六点半叫我，我要赶早班车"]:
        res = prescreen(text)
        assert res.passed and res.reason == "plan", f"{text!r} → {res.reason}"


def test_question_guard_blocks_entity_mention():
    """疑问句护栏在实体之前：问"老王怎么样"没有新信息，不该触发抽取。"""
    idx = EntityIndex([("老王", 1)])
    res = prescreen("老王最近怎么样？", entities=idx)
    assert not res.passed
    assert res.reason == "question"

    res = prescreen("老王最近怎么样", entities=idx)  # 没有问号 → 当陈述处理
    assert res.passed and res.reason == "entity"


def test_question_guard_only_checks_tail():
    """"我不确定这是不是真的"含疑问词但是陈述，不能误挡。"""
    res = prescreen("我不确定这是不是真的，回头问问", prev_assistant="")
    assert res.reason != "question", "陈述句被当成提问挡掉了"


def test_short_but_entity_passes():
    """实体命中把长度门槛降到 3：豆豆病了（4 字）该放。"""
    idx = EntityIndex([("豆豆", 1)])
    assert prescreen("豆豆病了", entities=idx).passed
    assert not prescreen("嗯", entities=idx).passed


def test_fact_alone_is_enough():
    """手册写的是 time+fact 双命中。

    实测单命中更好："我住在杭州"只有 fact 没有 time，
    按原顺序会被刷掉，而居住地是最该记的事实之一。
    """
    for text in ["我住在杭州西湖区", "我养了只橘猫叫豆豆", "我对花生严重过敏"]:
        res = prescreen(text)
        assert res.passed, f"{text!r} → {res.reason}"


def test_preference_markers():
    for text in ["我喜欢喝黑咖啡", "我讨厌下雨天", "我吃不了辣", "我戒了烟"]:
        assert prescreen(text).passed, text


def test_oral_variants_are_covered():
    """口语省略是规则法的最大盲区，这几条是踩过的坑，别再退回去。"""
    cases = [
        "我有两个儿子",          # 数量词写死"一个"就漏
        "我前年离的婚",          # "离的婚"不是"离婚"
        "我今年四十二",          # 口语省略"岁"
        "我刚搬到广州",          # "刚"夹在"我"和"搬"之间
        "我家里养了只乌龟",      # 地点词夹在中间
        "我每天都被闹钟吵醒",    # 这条是放宽规则后的已知误报，锁住行为
    ]
    for text in cases:
        assert prescreen(text).passed, text


def test_answer_to_question():
    """上一轮 AI 在提问，用户这轮的回答默认放行——回答通常没有任何标记词。"""
    res = prescreen("叫豆豆，三岁了", prev_assistant="你家猫叫什么名字？")
    assert res.passed and res.reason == "answer"

    assert not prescreen("叫豆豆，三岁了").passed


def test_answer_without_content_is_blocked():
    """回答规则不能变成"AI 问一句、用户说啥都放行"。

    真机跑出来过：chitchat 窗口五句寒暄，因为其中一句跟在
    "有什么想聊的吗？" 后面被放行，整窗白花一次调用。
    """
    q = "有什么想聊的吗？"
    assert not prescreen("今天天气不错", prev_assistant=q).passed
    assert not prescreen("还行吧，就这样", prev_assistant=q).passed
    assert not prescreen("不知道哎", prev_assistant=q).passed

    # 但真回答必须照旧放行，否则就是修一个漏、造一个更大的漏
    assert prescreen("叫豆豆，三岁了", prev_assistant=q).passed
    assert prescreen("在字节做后端", prev_assistant=q).passed
    # 边界：带具体约束的句子即使含"还行"也要放
    assert prescreen("微辣还行，中辣受不了", prev_assistant=q).passed


def test_answer_disabled_by_config():
    rules = RuleSet({"fact": [], "preference": [], "plan": [],
                     "answer_to_question": False})
    assert not prescreen("叫豆豆，三岁了", prev_assistant="叫什么？", rules=rules).passed


def test_nfkc_normalizes_fullwidth():
    """全角数字要能命中同一条规则。"""
    assert prescreen("８点叫我").reason == "plan"
    assert prescreen("8点叫我").reason == "plan"


def test_pure_punctuation_is_empty():
    for text in ["", "   ", "。。。", "！！！"]:
        assert prescreen(text).reason == "empty"


def test_matched_span_is_reported():
    """matched 用于调参时看清是哪条规则咬的。"""
    res = prescreen("我对花生过敏")
    assert res.matched and "过敏" in res.matched[0]


# ---------------------------------------------------------------- 性能
def test_latency_under_2ms():
    """手册验收第 3 条：单次 < 2ms。实测 5~60µs，留足余量。"""
    texts = ["我对花生过敏，上次休克送医", "明天八点叫我", "嗯"] * 200
    prescreen(texts[0])  # 预热，避免把首次编译算进去
    t0 = time.perf_counter()
    for t in texts:
        prescreen(t)
    per_call = (time.perf_counter() - t0) / len(texts)
    assert per_call < 0.002, f"单次 {per_call * 1000:.3f}ms"


def test_no_catastrophic_backtracking_on_10k():
    """手册验收第 4 条：10k 字 < 50ms。正则里出现 .* 就会在这里炸。"""
    long_text = "我住在杭州，工作很忙，每天都要加班到很晚。" * 450  # ≈ 9000 字
    assert len(long_text) > 9000
    t0 = time.perf_counter()
    prescreen(long_text)
    elapsed = (time.perf_counter() - t0) * 1000
    assert elapsed < 50, f"10k 字耗时 {elapsed:.1f}ms"


def test_ruleset_compiles_once():
    """规则必须预编译。每次调用重新 compile 会让 40 条正则吃掉 3ms。"""
    r1 = load_rules()
    r2 = load_rules()
    assert r1 is r2
    assert all(hasattr(p, "search") for p in r1.fact)


# ---------------------------------------------------------------- 标定护栏
def _rows(split: str) -> list[dict]:
    path = FIXTURES / "prescreen_labeled.jsonl"
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r["split"] == split and r.get("tag") != "hard_neg":
                out.append(r)
    return out


def _metrics(rows: list[dict]) -> tuple[float, float, float]:
    idx = EntityIndex([
        (w.strip(), i) for i, w in enumerate(
            (FIXTURES / "prescreen_entities.txt").read_text(encoding="utf-8").splitlines())
        if w.strip()
    ])
    tp = fp = fn = 0
    for r in rows:
        got = prescreen(r["text"], entities=idx).passed
        want = r["should_extract"]
        tp += want and got
        fp += (not want) and got
        fn += want and not got
    p = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return p, rec, (tp + fp) / len(rows)


def test_calibration_dev():
    """dev 集：P≥0.85 R≥0.90 放行率 25%~40%。"""
    p, r, rate = _metrics(_rows("dev"))
    assert p >= 0.85, f"precision {p:.3f}"
    assert r >= 0.90, f"recall {r:.3f}"
    assert 0.25 <= rate <= 0.40, f"pass_rate {rate:.3f}"


def test_calibration_blind():
    """blind 集是最后一次未见过的检验。数字掉下来就说明规则在过拟合旧样本。

    注意：blind 没放难负样本，所以它的 precision 会偏高（1.000），
    真正可信的 precision 看 dev/holdout/test 的 0.90 左右。
    这里只锁 recall 和放行率。
    """
    p, r, rate = _metrics(_rows("blind"))
    assert r >= 0.90, f"recall {r:.3f}"
    assert 0.25 <= rate <= 0.40, f"pass_rate {rate:.3f}"


def test_corpus_integrity():
    """样本集的完整性：条数、正负比、每批都有正样本。"""
    all_rows = []
    path = FIXTURES / "prescreen_labeled.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            all_rows.append(json.loads(line))
    assert len(all_rows) >= 570
    splits = {r["split"] for r in all_rows}
    assert splits == {"dev", "holdout", "test", "blind"}
    for s in splits:
        rows = [r for r in all_rows if r["split"] == s]
        assert any(r["should_extract"] for r in rows), s
        assert any(not r["should_extract"] for r in rows), s
    for r in all_rows:
        assert r["why"], "每条样本必须写 why，否则调参时看不懂它在测什么"


# ---------------------------------------------------------------- 可覆盖
def test_custom_lexicon_changes_behaviour(tmp_path):
    """词表是配置不是代码：用户换一份文件就该换行为。"""
    custom = tmp_path / "lex.yaml"
    custom.write_text(
        "version: 1\nplan: []\npreference: ['我痴迷']\ntime: []\n"
        "fact: []\nquestion_enders: ['？']\nanswer_to_question: false\nmin_chars: 6\n",
        encoding="utf-8",
    )
    rules = load_rules(custom)
    assert prescreen("我痴迷于集邮", rules=rules).passed
    assert not prescreen("我喜欢吃辣", rules=rules).passed   # 默认词表才认"我喜欢"
    assert prescreen("我痴迷于集邮").passed is False        # 默认词表不认"我痴迷"
