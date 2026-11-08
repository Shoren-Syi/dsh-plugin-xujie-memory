# -*- coding: utf-8 -*-
"""M46 验收：摘要自动生成的触发条件。

只靠 /drain 等于永远不生成（DSH 很少走到那里）。策略：攒够 N 轮，
或者会话静下来一段时间且有新内容，就补一条；同一条会话最短 refresh 才重写。
"""
from __future__ import annotations

from hippo.config import Config
from hippo.core.digest import need_refresh, read_meta, save_meta


def _cfg(**over):
    cfg = Config()
    cfg.digest.enabled = True
    cfg.digest.min_new_turns = 8
    cfg.digest.refresh_minutes = 30
    for k, v in over.items():
        setattr(cfg.digest, k, v)
    return cfg


def test_no_turns_no_digest():
    assert need_refresh(last_idx=-1, meta={}, now=1000, cfg=_cfg()) is False


def test_first_digest_generated():
    assert need_refresh(last_idx=3, meta={}, now=1000, cfg=_cfg()) is True


def test_not_enough_new_turns_and_too_soon():
    meta = {"idx": 10, "at": 1000}
    assert need_refresh(last_idx=12, meta=meta, now=1060, cfg=_cfg()) is False


def test_enough_new_turns_triggers():
    meta = {"idx": 10, "at": 1000}
    assert need_refresh(last_idx=20, meta=meta, now=1060, cfg=_cfg()) is True


def test_refresh_after_interval():
    meta = {"idx": 10, "at": 1000}
    # 只有 2 条新内容，但距上次生成已超过 30 分钟 → 重写
    assert need_refresh(last_idx=12, meta=meta, now=1000 + 31 * 60, cfg=_cfg()) is True


def test_refresh_interval_respected():
    meta = {"idx": 10, "at": 1000}
    # 有 5 条新内容、只过了 16 分钟：不到 30 分钟重写间隔，也还没攒够 8 条
    assert need_refresh(last_idx=15, meta=meta, now=1000 + 16 * 60, cfg=_cfg()) is False


def test_disabled_never_triggers():
    assert need_refresh(last_idx=99, meta={}, now=10_000, cfg=_cfg(enabled=False)) is False


def test_meta_roundtrip(store):
    save_meta(store, "s", idx=42, at=1234)
    assert read_meta(store, "s") == {"idx": 42, "at": 1234}
    assert read_meta(store, "other") == {}
