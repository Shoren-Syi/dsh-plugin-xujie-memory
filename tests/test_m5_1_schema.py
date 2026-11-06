"""M5.1 验收：输出 schema、解析容错、raw 逐字锚定。

这一组测试里最重要的是 `test_anchor_*` 和 `test_render_prompt_*`。
前者守住"原文是唯一真相源"，后者守住"用户说一句带花括号的话不会让抽取崩"。
"""

from __future__ import annotations

import json

import pytest

from hippo.core.schema import (
    AffectDelta, ExtractResult, ParseError, anchor_raw, clean_keywords,
    load_template, parse_result, render_prompt,
)

# ---------------------------------------------------------------- 真实输出样本
# 这些不是"理想的 JSON"，是模型实际会吐出来的十种样子。
# 每一条都对应一次我调 prompt 时真实遇到的形态。
SAMPLES: list[tuple[str, str]] = [
    ("纯 JSON", json.dumps({
        "events": [{"raw": "我昨天搬家了", "summary": "用户昨天搬家",
                    "keywords": ["搬家"], "entities": [{"name": "用户"}],
                    "event_time_hint": "昨天", "arousal": 0.2}],
        "facts": [], "traits": [], "plans": [], "doubts": [],
        "affect_delta": {"joy": 2},
    }, ensure_ascii=False)),

    ("markdown 围栏", "```json\n" + json.dumps({
        "events": [{"raw": "我在字节做后端", "summary": "用户是字节后端工程师",
                    "keywords": ["字节", "后端"], "entities": [],
                    "arousal": 0}],
    }, ensure_ascii=False) + "\n```"),

    ("带开场白", "好的，以下是抽取结果：\n" + json.dumps({
        "events": [{"raw": "我妈住院了", "summary": "用户母亲住院",
                    "keywords": ["母亲", "住院"], "arousal": 0.8}],
    }, ensure_ascii=False) + "\n希望对你有帮助。"),

    ("null 字段", json.dumps({
        "events": None, "facts": None, "traits": None,
        "plans": None, "doubts": None, "affect_delta": None,
    })),

    ("缺字段", json.dumps({"events": [{"raw": "我养了只猫", "keywords": "猫"}]},
                         ensure_ascii=False)),

    ("arousal 十分制", json.dumps({
        "events": [{"raw": "我们吵架了", "arousal": 9}],
    }, ensure_ascii=False)),

    ("多余字段", json.dumps({
        "events": [{"raw": "我换工作了", "confidence_note": "高"}],
        "meta": {"model": "x"}, "_thinking": "...",
    }, ensure_ascii=False)),

    ("字符串下标", json.dumps({
        "events": [{"raw": "老王在创业"}],
        "traits": [{"entity": "老王", "trait": "在创业", "status": "observed",
                    "evidence": ["0"]}],
    }, ensure_ascii=False)),

    ("嵌套引号", json.dumps({
        "events": [{"raw": "他说\"我不干了\"，然后走了",
                    "summary": "用户转述同事辞职"}],
    }, ensure_ascii=False)),

    ("被截断后补全", '{"events": [{"raw": "我下周三去杭州", "summary": "出差"}]}'),
]


@pytest.mark.parametrize("name,text", SAMPLES, ids=[s[0] for s in SAMPLES])
def test_parse_all_samples(name, text):
    """10/10 可解析、schema 合法。"""
    r = parse_result(text)
    assert isinstance(r, ExtractResult)


def test_parse_fenced_with_trailing_text():
    r = parse_result('```json\n{"events":[{"raw":"abc"}]}\n```\n以上。')
    assert r.events[0].raw == "abc"


def test_parse_empty_raises():
    with pytest.raises(ParseError):
        parse_result("   ")


def test_parse_truncated_raises_with_hint():
    """被 max_tokens 截断时，错误信息要指向"截断"而不是"JSON 语法错"。
    这两种故障的修法完全不同（调大 max_tokens vs 改 prompt）。"""
    with pytest.raises(ParseError) as ei:
        parse_result('{"events": [{"raw": "我说了一半')
    assert "闭合" in str(ei.value)


def test_parse_no_json():
    with pytest.raises(ParseError):
        parse_result("抱歉，我无法完成这个任务。")


def test_parse_top_level_array_rejected():
    with pytest.raises(ParseError):
        parse_result('["events"]')


def test_parse_error_keeps_head():
    try:
        parse_result("这不是 JSON")
    except ParseError as e:
        assert "这不是 JSON" in e.head


# ---------------------------------------------------------------- 归一化
def test_arousal_tens_scale():
    r = parse_result('{"events":[{"raw":"x","arousal":9}]}')
    assert r.events[0].arousal == 0.9


def test_arousal_clamped():
    r = parse_result('{"events":[{"raw":"x","arousal":-3}]}')
    assert r.events[0].arousal == 0.0
    r2 = parse_result('{"events":[{"raw":"x","arousal":999}]}')
    assert r2.events[0].arousal == 1.0


def test_summary_truncated():
    r = parse_result(json.dumps({"events": [{"raw": "x", "summary": "长" * 300}]},
                                ensure_ascii=False))
    assert len(r.events[0].summary) == 100


def test_keywords_from_string():
    r = parse_result('{"events":[{"raw":"x","keywords":"上海,搬家、房租"}]}')
    assert r.events[0].keywords == ["上海", "搬家", "房租"]


def test_affect_delta_clamped():
    d = AffectDelta(joy=100, anger=-100)
    assert d.joy == 20 and d.anger == -20


def test_is_empty():
    assert ExtractResult().is_empty()
    assert not parse_result('{"events":[{"raw":"x"}]}').is_empty()


# ---------------------------------------------------------------- 关键词清洗
# M6.1 把清洗拆成了两层：
#   解析层（这里）  只做机械归一化，因为此时还不知道有哪些实体名
#   入库层（persist）做策略清洗，那时才知道"猫"是用户养的猫
# 下面四条锁的是**解析层不做策略判断**，策略本身见 test_m6_1_keywords.py。
def test_parse_layer_keeps_policy_for_later():
    """空词在解析层必须留着。

    如果这里就把"事情"扔掉，persist 拿不到原始列表，
    "模型到底产了多少空词"这个指标就永远算不出来——
    而这正是 M6.1 要评的东西。
    """
    assert clean_keywords(["事情", "问题", "上海", "那个"]) == [
        "事情", "问题", "上海", "那个"]


def test_parse_layer_dedup_and_keep_single_char():
    """去重是机械活；单字不能在这里砍——"猫"可能是实体名。"""
    assert clean_keywords(["上海", "上海", "沪", "a" * 20]) == [
        "上海", "沪", "a" * 20]


def test_parse_layer_cap_is_generous():
    """解析层的上限（16）只是防止模型吐出几百个词占内存。

    真正的上限 8 在入库层，因为要靠实体名/长度排序来选。
    """
    assert len(clean_keywords([f"词{i}" for i in range(40)])) == 16


def test_parse_layer_empty():
    assert clean_keywords(None) == []
    assert clean_keywords("") == []


# ---------------------------------------------------------------- raw 锚定
SRC = """我昨天下午搬家了，从朝阳搬到海淀，房租涨了两千。
对了，老王说他下周要去深圳出差。
我实在受不了了，这破公司天天加班。"""


def test_anchor_exact():
    assert anchor_raw("我昨天下午搬家了", SRC) == "我昨天下午搬家了"


def test_anchor_substring():
    assert anchor_raw("从朝阳搬到海淀", SRC) == "从朝阳搬到海淀"


def test_anchor_nfkc_fullwidth():
    """"８点" 和 "8点" 在语义上没区别，但对不上字节。归一化后要能锚上，
    且返回的必须是**原文的写法**，不是归一化后的。"""
    src = "明天８点叫我起床"
    got = anchor_raw("明天8点叫我", src)
    assert got == "明天８点叫我", got


def test_anchor_whitespace_rearranged():
    src = "我 昨天 去了 杭州"
    assert anchor_raw("我昨天去了杭州", src) == "我 昨天 去了 杭州"


def test_anchor_rejects_polished():
    """模型把"受不了"润色成"无法忍受"——必须拒绝。"""
    assert anchor_raw("我无法忍受了，这破公司天天加班", SRC) is None


def test_anchor_rejects_ai_words():
    """AI 自己的话不在 user 原文里，锚不上。规则 2 的机器执行版。"""
    assert anchor_raw("您母亲住院期间需要多休息", SRC) is None


def test_anchor_rejects_merged_sentences():
    """把两句合成一句是模型最爱的"优化"，必须拒绝。"""
    merged = "我昨天下午搬家了从朝阳搬到海淀"
    assert anchor_raw(merged, SRC) is None


def test_anchor_short_raw_is_source_kept():
    """短 raw（"嗯""好的"）允许，但要能在原文里找到。"""
    assert anchor_raw("嗯", "嗯，我知道了") == "嗯"
    assert anchor_raw("嗯", "完全无关的话") is None


def test_anchor_empty():
    assert anchor_raw("", SRC) is None
    assert anchor_raw("x", "") is None


def test_anchor_partial_punctuation():
    """模型删掉句末句号——归一化不看标点前后的空白，但标点本身要一致。"""
    assert anchor_raw("我实在受不了了，这破公司天天加班", SRC) is not None


# ---------------------------------------------------------------- prompt
def test_template_has_json_keyword():
    """DeepSeek 的 response_format=json_object 要求 prompt 里出现 "json"，
    否则直接 400。这条断言防止有人"优化"掉那个词。"""
    assert "json" in load_template().lower()


def test_template_placeholders():
    t = load_template()
    for ph in ("<<KNOWN_ENTITIES>>", "<<NOW_HINT>>", "<<CONVERSATION>>"):
        assert ph in t, f"模板缺占位符 {ph}"


def test_render_basic():
    out = render_prompt("user: 你好", known_entities="- 老王", now_hint="2026-11-04")
    assert "user: 你好" in out
    assert "- 老王" in out
    assert "2026-11-04" in out
    assert "<<" not in out


def test_render_survives_braces():
    """用户说"我写了个 {x} 的模板"不能让抽取崩。

    这是 str.format 的经典坑：对话内容里的 `{` 会被当成占位符。
    我们全程用 replace，所以安全——但要有一条测试钉住它。
    """
    out = render_prompt('user: 我写了个 {x} 的模板，还用了 {0} 和 %}')
    assert "{x}" in out and "{0}" in out


def test_render_defaults():
    out = render_prompt("")
    assert "（空）" in out
    assert "（暂无）" in out


def test_render_custom_template():
    out = render_prompt("TXT", template="A <<CONVERSATION>> B")
    assert out == "A TXT B"
