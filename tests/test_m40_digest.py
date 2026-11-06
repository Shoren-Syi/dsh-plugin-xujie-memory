# -*- coding: utf-8 -*-
"""M40 验收：会话摘要必须真的进 prompt。

这条是回归线：摘要"生成了、也存了、但被预算裁掉"曾经悄悄发生，
只看 get_digest 返回非空是发现不了的——必须看装配结果。
"""
from __future__ import annotations

from hippo.config import Config
from hippo.core.digest import DIGEST_KEY, get_digest, save_digest
from hippo.core.pack import pack


def test_save_and_get_digest(store):
    save_digest(store, "s", "  用户在做记忆插件，卡在召回率上。 ")
    assert get_digest(store, "s") == "用户在做记忆插件，卡在召回率上。"
    assert store.get_state("s", DIGEST_KEY, "") != ""


def test_digest_reaches_prompt_even_without_pinned_facts(store):
    """C 段原本只放钉住事实；摘要是单独一行、要单独给额度，不能被裁掉。"""
    save_digest(store, "s", "用户在给记忆插件做召回评测，当前结论是摘要必须注入。")
    text = pack(store, "s", "今天干点啥", cfg=Config(), use_cache=False).text
    assert "[会话摘要]" in text
    assert "摘要必须注入" in text


def test_digest_absent_does_not_add_noise(store):
    text = pack(store, "s", "今天干点啥", cfg=Config(), use_cache=False).text
    assert "[会话摘要]" not in text
