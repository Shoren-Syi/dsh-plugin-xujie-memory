# -*- coding: utf-8 -*-
"""M22.1 标签协议：解析 / level 校验 / reason 约束 / 协议注入。"""
import pytest

from hippo.core.giveup import (GIVEUP_PROTOCOL, LEVELS, Giveup, audit,
                               parse_tag, strip_tags)


def test_parse_standard():
    g = parse_tag('做不了。<giveup level="decline" reason="缺少文件权限"/>')
    assert g is not None
    assert (g.level, g.reason, g.valid) == ("decline", "缺少文件权限", True)


def test_level_enum_all_four():
    """验收 #2：四种 level 都能解析。"""
    for lv in LEVELS:
        g = parse_tag(f'<giveup level="{lv}" reason="x"/>')
        assert g is not None and g.level == lv and g.valid


def test_illegal_level_degrades():
    """非法 level 不许原样透传：降级 decline 并记 problem（标签零泄漏优先）。"""
    g = parse_tag('<giveup level="surrender" reason="x"/>')
    assert g is not None and g.level == "decline" and not g.valid
    assert "surrender" in g.problem


def test_decline_requires_reason():
    """验收 #3：decline 缺 reason 补默认值并记 problem。"""
    g = parse_tag('<giveup level="decline"/>')
    assert g is not None and not g.valid and g.reason
    # 其它 level 不强制 reason
    g2 = parse_tag('<giveup level="partial"/>')
    assert g2 is not None and g2.valid


def test_parse_loose_forms():
    """模型输出不规整：单引号 / 无引号 / 大小写 / 空格。"""
    g = parse_tag("<giveup level='ask_help' reason='需要格式'/>")
    assert g is not None and (g.level, g.reason) == ("ask_help", "需要格式")
    g = parse_tag('<GIVEUP LEVEL="RETRY_ALT" REASON="换法子"/>')
    assert g is not None and g.level == "retry_alt"
    g = parse_tag("<giveup level=partial reason=只做了一半 />")
    assert g is not None and g.level == "partial"


def test_no_tag_returns_none():
    assert parse_tag("正常回复，没有任何标签") is None
    assert parse_tag("") is None
    assert parse_tag(None) is None


def test_strip_and_audit():
    """剥离后零残留——验收 #1 不变式。"""
    text = '前文<giveup level="decline" reason="x"/>后文'
    clean = strip_tags(text)
    assert clean == "前文后文"
    assert audit(clean) == []
    assert audit(text)                     # 有标签 → 有问题


def test_protocol_text_contract():
    """协议文本必须包含：四个 level 名、reason 要求、"放弃不算失败"。"""
    for lv in LEVELS:
        assert lv in GIVEUP_PROTOCOL
    assert "reason" in GIVEUP_PROTOCOL
    assert "放弃不算失败" in GIVEUP_PROTOCOL
    assert "<giveup" in GIVEUP_PROTOCOL


def test_protocol_injected_into_persona(store):
    """M22.1 接入：A 段人设末尾恒定带协议（用户没写人设文件也一样）。"""
    from hippo.core.pack import pack

    res = pack(store, "s1", "你好")
    persona = res.by_name["persona"]
    assert "<giveup" in persona.text
    assert GIVEUP_PROTOCOL.strip() in persona.text


def test_long_persona_keeps_protocol(tmp_path):
    """人设文件超长：截人设保协议（协议丢了功能就没了）。"""
    from hippo.core.pack import _persona

    class C:
        persona_path = str(tmp_path / "persona.md")
        db_path = str(tmp_path / "x.db")

    (tmp_path / "persona.md").write_text("人设" * 2000, encoding="utf-8")
    text = _persona(C())
    assert "<giveup" in text
    assert len(text) < 2000               # 人设主体被截
