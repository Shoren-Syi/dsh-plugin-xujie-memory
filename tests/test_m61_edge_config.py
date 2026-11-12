# -*- coding: utf-8 -*-
"""M61 验收：坏配置不能拖垮启动；/event 只收 user/assistant。

两个都是实测出来的：
1. 把 `~/.hippo/config.yaml` 写坏（少个括号）→ 内核直接起不来，
   用户看到的是"装完没反应"，极难排查。
2. `/event` 的 role 原来是任意字符串，`role=system` 也能写进库——污染记忆。
"""
from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

import hippo.config as config_mod
from hippo.config import Config, load_config
from hippo.dsh_kernel import EventBody


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def test_bad_yaml_falls_back_to_defaults(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("deepseek: [unclosed\n  bad: : :\n", encoding="utf-8")
    cfg = load_config(path)
    assert isinstance(cfg, Config)
    assert cfg.db_path  # 默认值可用


def test_bad_field_type_falls_back_to_defaults(tmp_path):
    path = tmp_path / "bad-field.yaml"
    path.write_text('extract:\n  every_n_turns: "not-a-number"\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.extract.every_n_turns == Config().extract.every_n_turns


def test_non_dict_yaml_falls_back(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    cfg = load_config(path)
    assert isinstance(cfg, Config)


def test_valid_yaml_still_applies(tmp_path):
    path = tmp_path / "ok.yaml"
    path.write_text('extract:\n  every_n_turns: 3\n', encoding="utf-8")
    assert load_config(path).extract.every_n_turns == 3


def test_event_body_rejects_unknown_role():
    assert EventBody(session="s", role="user", text="hi").role == "user"
    assert EventBody(session="s", role="assistant", text="hi").role == "assistant"
    with pytest.raises(ValidationError):
        EventBody(session="s", role="system", text="hi")


def test_event_body_rejects_empty_session():
    with pytest.raises(ValidationError):
        EventBody(session="", role="user", text="hi")
    with pytest.raises(ValidationError):
        EventBody(session="a" * 201, role="user", text="hi")
    assert EventBody(session="s1", role="user", text="hi").session == "s1"
