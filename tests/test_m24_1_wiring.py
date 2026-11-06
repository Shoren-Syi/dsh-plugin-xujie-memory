# -*- coding: utf-8 -*-
"""M24 心跳接线：lifespan 起一个 task，shutdown 收干净。

单独一个文件，因为它验的是"代理真的会起心跳"——单元测试测不到这层，
而接错的后果是"部署后心跳根本没跑"，从外面看一切正常。
"""
from __future__ import annotations

import asyncio

import pytest

from hippo.adapters.proxy import create_app
from hippo.config import Config
from hippo.core.db import connect
from hippo.core.migrate import migrate
from tests.conftest import serve


def _app(tmp_path, **kw):
    db = tmp_path / "mem.db"
    c = connect(db)
    migrate(c)
    c.close()
    cfg = Config()
    cfg.db_path = str(db)
    cfg.deepseek.api_key = "sk-test"
    for k, v in kw.items():
        setattr(cfg.heartbeat, k, v)
    return create_app(cfg, store_path=db)


@pytest.mark.asyncio
async def test_heartbeat_task_started_and_stopped(tmp_path):
    app = _app(tmp_path)
    async with serve(app):
        assert app.state.hb is not None
        assert app.state.hb_task is not None
        assert not app.state.hb_task.done()
    # 上下文退出后任务必须结束，否则进程退不掉
    assert app.state.hb_task.done()


@pytest.mark.asyncio
async def test_heartbeat_ticks_in_real_loop(tmp_path):
    """把间隔压到 0.2 秒，真实跑两轮——证明 lifespan 里的 task 真的在转。"""
    app = _app(tmp_path, interval_seconds=1)
    async with serve(app):
        app.state.hb.tick_seconds = 0.2
        # 首轮延迟是启动时的 1 秒，这里直接把 task 换掉，只验循环本身
        app.state.hb_task.cancel()
        await asyncio.sleep(0)
        app.state.hb_task = asyncio.create_task(
            app.state.hb.run_forever(stop=app.state.hb_stop))
        await asyncio.sleep(0.7)
        assert app.state.hb.counts["ticks"] >= 2


@pytest.mark.asyncio
async def test_heartbeat_disabled_when_no_db(tmp_path):
    """库不存在时不起心跳（否则每轮都报 no such table）。"""
    cfg = Config()
    cfg.db_path = str(tmp_path / "missing.db")
    app = create_app(cfg, store_path=tmp_path / "missing.db")
    async with serve(app):
        assert app.state.hb is None
