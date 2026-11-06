"""M9.3 验收：候选集上限保护。

这一节防的是"某次查询把整库捞出来"。它自己没什么逻辑，
难的是**让截断可见**——静默截断的表现不是报错，是用久了之后它开始忘事，
而日志里什么都查不到。所以这里的断言一半在测行为，一半在测"有没有留下痕迹"。
"""

from __future__ import annotations

import json
import logging

import pytest

from hippo.core.retrieve import Query, l1_candidates


@pytest.fixture()
def capture(caplog):
    """抓 hippo.core.retrieve 的日志。

    `hippo` 根 logger 的 propagate=False（log.py 里刻意的，避免污染宿主程序），
    所以 pytest 的 caplog 默认收不到。这里直接挂到目标 logger 上。
    """
    records: list[logging.LogRecord] = []

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _H()
    logger = logging.getLogger("hippo.core.retrieve")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _seed(store, n=1200, session="s1"):
    """造 n 条事件，权重从 0.01 到 0.99 递增（i 越大权重越高）。"""
    with store.tx():
        for i in range(n):
            store.conn.execute(
                """
                INSERT INTO events (session, event_time, ingest_time, w0, weight,
                                    keywords, summary, raw, entities)
                VALUES (?, ?, ?, ?, ?, '', ?, ?, '[]')
                """,
                (session, 1_700_000_000 + i * 60, 1_700_000_000 + i * 60,
                 0.01 + 0.98 * i / max(1, n - 1),
                 0.01 + 0.98 * i / max(1, n - 1),
                 f"事件{i}", f"第{i}条"),
            )
    store.conn.commit()


def test_over_cap_is_truncated_to_cap(store):
    _seed(store, 1200)
    res = l1_candidates(store, "s1", Query(text=""), cap=800)
    assert len(res.event_ids) == 800
    assert res.truncated is True


def test_truncation_keeps_highest_weight(store):
    """截断按 weight DESC，不是按 id 或随机。

    按 id 截断（`LIMIT 800` 但 ORDER BY id）的表现是"它只记得最初那 800 件事"，
    而最近的事一件都不记得——这在任何冒烟测试里都看不出来，
    只有用上两个月才会被用户发现。
    """
    _seed(store, 1200)
    res = l1_candidates(store, "s1", Query(text=""), cap=800)
    kept = set(res.event_ids)
    n = store.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    # 权重最低的 400 条必须全部被丢掉
    low = store.conn.execute(
        "SELECT id FROM events ORDER BY weight ASC LIMIT 400").fetchall()
    assert not (kept & {int(r["id"]) for r in low})
    assert len(res.event_ids) == n - 400


def test_truncation_logs_warning(store, capture):
    _seed(store, 900)
    l1_candidates(store, "s1", Query(text=""), cap=800)
    msgs = [r.msg for r in capture]
    assert "retrieve.l1.truncated" in msgs, msgs
    rec = next(r for r in capture if r.msg == "retrieve.l1.truncated")
    assert rec.levelno == logging.WARNING
    assert rec.fields["cap"] == 800


def test_under_cap_does_not_truncate_or_warn(store, capture):
    _seed(store, 100)
    res = l1_candidates(store, "s1", Query(text=""))
    assert res.truncated is False
    assert len(res.event_ids) == 100
    assert "retrieve.l1.truncated" not in [r.msg for r in capture]


def test_exactly_at_cap_is_not_truncated(store):
    """正好 800 条不算截断。

    实现里 `LIMIT cap+1` 是为了区分"刚好等于 cap"和"超过 cap"。
    用 `LIMIT cap` 时两者无法区分，日志会永远报警——报警久了就没人看，
    真正超限的那次也就被淹没了。
    """
    _seed(store, 800)
    res = l1_candidates(store, "s1", Query(text=""), cap=800)
    assert len(res.event_ids) == 800
    assert res.truncated is False


def test_cap_comes_from_config_when_present(store):
    """配置里的 max_candidates 优先于默认 800。"""
    _seed(store, 100)

    class _Cfg:
        max_candidates = 10
        recent_days = 30

    store.retrieve_cfg = _Cfg()
    res = l1_candidates(store, "s1", Query(text=""))
    assert len(res.event_ids) == 10
    assert res.truncated is True


def test_truncation_is_per_session(store):
    """别的会话的 1200 条不该占本会话的名额。"""
    _seed(store, 1200, session="other")
    _seed(store, 5, session="s1")
    res = l1_candidates(store, "s1", Query(text=""), cap=800)
    assert len(res.event_ids) == 5
    assert res.truncated is False
