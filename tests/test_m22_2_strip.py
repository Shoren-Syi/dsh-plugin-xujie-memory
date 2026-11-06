# -*- coding: utf-8 -*-
"""M22.2 流式拦截与改写。验收：标签零泄漏 / 流式不破 /
四档改写正确 / 延迟 <30 字符。

这一节的敌人只有一个：**chunk 边界**。逐块正则替换在 `<giv` | `eup`
分块时必然漏网，所以必须跨块缓冲，测试也就必须逐位置切块扫一遍。
"""
import pytest

from hippo.core.giveup import (LEVELS, StreamFilter, audit, finish, parse_tag,
                               rewrite, strip_tags)


def _stream(text: str, size: int) -> tuple[str, StreamFilter]:
    f = StreamFilter()
    out = "".join(f.feed(text[i:i + size]) for i in range(0, len(text), size))
    return finish(out, f), f


def test_zero_leak_every_cut_position():
    """验收 #1 的最狠版本：标签在每个可能位置切开，零残留。"""
    body = '先给结论：做不了。<giveup level="decline" reason="缺少文件权限"/>'
    for cut in range(1, len(body)):
        f = StreamFilter()
        out = f.feed(body[:cut]) + f.feed(body[cut:])
        text = finish(out, f)
        assert "giveup" not in text.lower(), (cut, text)
        assert "reason=" not in text.lower(), (cut, text)


def test_zero_leak_char_by_char():
    """逐字符喂（最坏情况：每字符一块，标签横跨 40 块）。"""
    body = '正文<giveup level="ask_help" reason="需要确认目标格式"/>尾巴'
    text, f = _stream(body, 1)
    assert not audit(text)
    assert f.tags and f.tags[0].level == "ask_help"


def test_normal_text_no_tag_passthrough():
    """验收 #2：无标签文本必须原样流出（不能吞字）。"""
    body = "这是一段很长的正常回复，" * 30
    text, f = _stream(body, 5)
    assert text == body
    assert f.tags == []


def test_delay_under_30_chars():
    """验收 #4：无标签时 hold back 只有 7 字符（<giveup 的长度）。"""
    f = StreamFilter()
    for ch in "正常回复" * 50:
        f.feed(ch)
    assert f.max_held <= 30
    assert f.max_held == 7          # "<giveup" 的长度，最小可能


def test_rewrite_ask_help():
    g = parse_tag('<giveup level="ask_help" reason="目标格式"/>')
    assert rewrite(g) == "这块我卡住了——我需要你告诉我 目标格式，不然我只能瞎猜。"


def test_rewrite_decline():
    g = parse_tag('<giveup level="decline" reason="缺少文件权限"/>')
    assert rewrite(g) == "这个我做不到。原因：缺少文件权限。"


def test_rewrite_partial_appends():
    g = parse_tag('<giveup level="partial" reason="Y 部分没做，因为缺数据"/>')
    out = rewrite(g, "前半部分已经做完。")
    assert out == "前半部分已经做完。\n\n另外，Y 部分没做，因为缺数据"
    # 没正文时只输出补述，不留空行
    assert rewrite(g, "") == "另外，Y 部分没做，因为缺数据"


def test_rewrite_retry_alt_silent():
    """retry_alt 不告诉用户（验收：内部重试，用户无感）。"""
    g = parse_tag('<giveup level="retry_alt" reason="换算法"/>')
    assert rewrite(g) is None
    text, f = _stream('让我换个法子。<giveup level="retry_alt" reason="换算法"/>', 4)
    assert text == ""                      # 用户什么都没看到


def test_partial_stream_keeps_body():
    """partial：正文正常流出，末尾追加补述。"""
    body = '这部分我做了。<giveup level="partial" reason="另一部分缺数据"/>'
    text, f = _stream(body, 3)
    assert text.startswith("这部分我做了。")
    assert text.endswith("另外，另一部分缺数据")


def test_unclosed_tag_swallowed():
    """流结束时标签未闭合（模型被截断）：吞掉，不半截漏出。"""
    f = StreamFilter()
    out = f.feed('正文。<giveup level="decline" reason="没写完')
    text = finish(out, f)
    assert "giveup" not in text.lower()
    assert f.incomplete is True
    assert text.startswith("正文。")


def test_multiple_tags_first_wins():
    """多个标签（模型啰嗦）：取第一个改写，其余全部剥掉。"""
    body = ('A<giveup level="ask_help" reason="需要X"/>'
            'B<giveup level="decline" reason="不行"/>')
    text, f = _stream(body, 2)
    assert not audit(text)
    assert "需要X" in text
    assert "不行" not in text


def test_plain_angle_brackets_not_swallowed():
    """普通尖括号（如 `<div>`）不是标签，必须原样保留。"""
    body = "用 <div> 包一下，再加 <br> 换行。"
    text, f = _stream(body, 1)
    assert text == body
    assert f.tags == []


def test_finish_never_leaks_even_if_dirty():
    """兜底：即使传入文本本身带标签，finish 也要清干净。"""
    f = StreamFilter()
    f.feed("正文")
    text = finish('脏文本<giveup level="decline" reason="x"/>', f)
    assert not audit(text)
