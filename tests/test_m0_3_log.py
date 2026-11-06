"""M0.3.1 结构化日志验收。"""

from __future__ import annotations

import json

import pytest

from hippo import log as hlog


@pytest.fixture()
def logdir(tmp_path):
    hlog.reset()
    hlog.setup(to_stderr=False, log_dir=tmp_path)
    yield tmp_path
    hlog.reset()


def test_each_line_is_valid_json(logdir):
    lg = hlog.get_logger("t")
    for i in range(5):
        lg.info("tick", extra={"fields": {"i": i}})

    path = hlog.current_log_file(logdir)
    assert path.exists(), "日志文件没生成"
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l]
    assert len(lines) == 5
    for line in lines:
        obj = json.loads(line)          # 不合法会直接抛
        assert obj["event"] == "tick"
        assert {"ts", "level", "logger", "event"} <= obj.keys()


def test_bind_injects_context_and_restores(logdir):
    lg = hlog.get_logger("t")
    with hlog.bind(session="a1", req_id="r-1", stage="proxy"):
        lg.info("inside")
        with hlog.bind(stage="pack"):
            lg.info("nested")
        lg.info("back")

    rows = [
        json.loads(l) for l in
        hlog.current_log_file(logdir).read_text(encoding="utf-8").splitlines() if l
    ]
    assert rows[0]["session"] == "a1" and rows[0]["stage"] == "proxy"
    # 嵌套是叠加，不是替换
    assert rows[1]["stage"] == "pack" and rows[1]["session"] == "a1"
    # 退出嵌套必须还原到外层，而不是清空
    assert rows[2]["stage"] == "proxy"


def test_extra_fields_flattened(logdir):
    lg = hlog.get_logger("t")
    lg.info("msg", extra={"fields": {"model": "deepseek-chat", "n": 3}})
    row = json.loads(hlog.current_log_file(logdir).read_text(encoding="utf-8").strip())
    assert row["model"] == "deepseek-chat"
    assert row["n"] == 3


def test_exception_is_captured(logdir):
    lg = hlog.get_logger("t")
    try:
        raise ValueError("boom")
    except ValueError:
        lg.exception("failed")
    row = json.loads(hlog.current_log_file(logdir).read_text(encoding="utf-8").strip())
    assert "ValueError: boom" in row["exc"]
