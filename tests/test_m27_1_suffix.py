"""M27.1 后缀兜底：中文"姓+职业"结构的最后一公里。

M26 的基线里 P23（"我平时看哪个医生"）一直漏，因为库里的词是"陈医生"。
前缀兜底管的是"老王头 ⊃ 老王"，管不了"陈医生 ⊃ 医生"。
这里锁四件事：能捞到、不乱捞、只在前面全空时捞、能关掉。
"""

from __future__ import annotations

import pytest

from hippo.core.store import Store


@pytest.fixture()
def s(store):
    return store


def _add(store: Store) -> int:
    return store.add_event(
        session="s", raw="我约了陈医生复查。", summary="用户约了陈医生复查",
        keywords="陈医生 复查 体检", w0=0.7)


def test_suffix_matches_tail_of_word(s):
    _add(s)
    rows = s.kw_search("s", ["医生"])
    assert rows, "陈医生 应该被 医生 后缀命中"
    assert "陈医生" in rows[0][2]


def test_single_char_is_not_suffix_matched(s):
    """单字不参与：'医' 会捞回兽医/牙医/医院，噪声比收益大。"""
    _add(s)
    assert s.kw_search("s", ["医"]) == []


def test_suffix_only_runs_when_nothing_else_matches(s, monkeypatch):
    """精确命中时不许再扫一遍——那是拿 1.6ms 换零收益。"""
    _add(s)
    modes: list[str] = []
    real = s._kw_aggregate

    def spy(tokens, mode, candidates, cap):
        modes.append(mode)
        return real(tokens, mode, candidates, cap)

    monkeypatch.setattr(s, "_kw_aggregate", spy)
    s.kw_search("s", ["复查"])
    assert "suffix" not in modes


def test_suffix_can_be_disabled(s):
    _add(s)

    class _Cfg:
        suffix_fallback = False

    s.retrieve_cfg = _Cfg()
    assert s.kw_search("s", ["医生"]) == []


def test_suffix_fallback_keeps_word_order(s):
    """命中的 token 要原样带回来（'陈医生'），不能回填查询词（'医生'）。"""
    _add(s)
    rows = s.kw_search("s", ["医生"])
    assert rows[0][2] == ["陈医生"]
