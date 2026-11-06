"""M29 · 预筛启发式：规则没命中时也要认得出真实对话里的信息。

背景：R-28.4 实测 120 轮真实对话里 95% 的窗口被旧规则预筛挡掉——
用户说的是"我是想2500以内""买二手没问题"，词表只认"住哪/工作/过敏"。
"""

from __future__ import annotations

import pytest

from hippo.config import Config
from hippo.core.prescreen import load_rules, prescreen, prescreen_heuristic

RULES = load_rules()


def h(text: str, **kw):
    return prescreen_heuristic(text, rules=RULES, **kw)


class TestRealDialogPasses:
    """R-28.4 里被旧规则挡掉、但明显有信息的真实句子。"""

    @pytest.mark.parametrize("text", [
        "我怎么说呢，我是想2500以内，弄一个好点的",
        "我买二手没问题的， 我完全可以接受，但是最大的问题是，现在电脑配件市场涨价涨的厉害",
        "2020版的r7000p",
        "其实我是很难受的，这个价格，现在只能配出这样的东西，离谱",
        "我不做流量池，我只是帮中国没有vpn的，不会注册api的人做，等于说，我和官方是一个价格。",
        "现在的价格很离谱的",
        "我现在的笔记本卖了差不多就这个价格",
        "所以，我有个问题，b350i和b450i差距在哪里，能不能用b350i",
    ])
    def test_passes(self, text: str) -> None:
        assert h(text).passed, text


class TestStillBlocks:
    @pytest.mark.parametrize("text", ["哪个好", "哈哈哈哈哈哈", "还行吧", "嗯", "好的"])
    def test_blocks(self, text: str) -> None:
        assert not h(text).passed, text

    def test_short_question_blocked(self) -> None:
        assert not h("在吗？").passed

    def test_pure_evaluation_blocked(self) -> None:
        assert not h("今天天气还不错吧").passed


class TestHeuristicDetails:
    def test_rule_hit_takes_priority(self) -> None:
        r = h("我住在杭州")
        assert r.passed
        assert r.reason == "fact"

    def test_numeric_short_passes(self) -> None:
        assert h("5500").passed
        assert h("现在是2026年").passed

    def test_long_question_passes(self) -> None:
        assert h("b350i和b450i差距在哪里？").passed

    def test_old_rules_miss_real_dialog(self) -> None:
        """对照组：证明旧规则确实漏，差异不是幻觉。"""
        assert not prescreen(
            "我怎么说呢，我是想2500以内，弄一个好点的", rules=RULES).passed


class TestConfig:
    def test_defaults(self) -> None:
        c = Config()
        assert c.extract.prescreen_mode == "heuristic"
        assert c.extract.heuristic_min_chars == 8

    def test_mode_validation(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Config(extract={"prescreen_mode": "bogus"})

    def test_rules_mode_configurable(self) -> None:
        c = Config(extract={"prescreen_mode": "rules"})
        assert c.extract.prescreen_mode == "rules"
