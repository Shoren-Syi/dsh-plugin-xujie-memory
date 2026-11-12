# -*- coding: utf-8 -*-
"""1.1.11 回归找 bug：新功能的边界 + 性能。

覆盖：
A. 合并边界：跨会话不应合并、归档事件不应被合并、关键词上限
B. 原话兜底边界：超长句子截断、重复句去重、无 turns 会话
C. HTTP 边界：/board/data 异常 limit、/assemble 超长文本、/event 控制字符
D. 性能：pack 在原话兜底开/关时的 p50/p95
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\h\hippo")

import httpx  # noqa: E402

from hippo.config import Config  # noqa: E402
from hippo.core.pack import pack  # noqa: E402
from hippo.core.persist import persist_result  # noqa: E402
from hippo.core.schema import EventOut, ExtractResult  # noqa: E402
from hippo.core.store import Store  # noqa: E402
from hippo.core.turns import Window  # noqa: E402

STRESS = Path(r"C:\h\stress")
PY = r"C:\h\hippo\runtime\win32-x64\python.exe"
PORT = 8895


def window(store, session="s"):
    turns = store.get_turns(session, limit=6)
    return Window(session=session, idxs=[t.idx for t in turns], token_est=0,
                  has_pending=False, turns=turns)


def ev(raw, summary, kws, imp=3, source="user"):
    return EventOut(raw=raw, summary=summary, keywords=kws, importance=imp, source=source)


def merge_edges() -> dict:
    out = {}
    db = STRESS / "bug-merge.db"
    if db.exists():
        db.unlink()
    cfg = Config(); cfg.db_path = str(db)
    with Store.open(db, cfg) as store:
        store.add_turns("a", [("user", "我给 fetch 加了 withRetry 重试", 0)], ts=1_760_000_000)
        store.add_turns("b", [("user", "我给 fetch 加了 withRetry 重试", 0)], ts=1_760_000_000)
        e1 = ev("我给 fetch 加了 withRetry 重试", "用户给 fetch 加了 withRetry 重试", ["fetch"], 3)
        persist_result(store, "a", window(store, "a"), ExtractResult(events=[e1]), now=1_760_000_100)
        # 另一个会话写同样内容：不应合并到 a
        e2 = ev("我给 fetch 加了 withRetry 重试", "用户给 fetch 加了 withRetry 重试", ["fetch"], 3)
        persist_result(store, "b", window(store, "b"), ExtractResult(events=[e2]), now=1_760_000_100)
        out["cross_session_events"] = store.conn.execute(
            "select count(*) from events").fetchone()[0]

        # 归档事件不应被合并
        first = store.conn.execute("select id from events where session='a'").fetchone()[0]
        store.conn.execute("update events set archived=1 where id=?", (first,))
        store.add_turns("a", [("user", "我把 fetch 的 withRetry 改成重试两次", 0)], ts=1_760_000_200)
        e3 = ev("我把 fetch 的 withRetry 改成重试两次",
                "用户给 fetch 的 withRetry 改为重试两次", ["withRetry"], 4)
        persist_result(store, "a", window(store, "a"), ExtractResult(events=[e3]), now=1_760_000_300)
        out["archived_untouched"] = store.conn.execute(
            "select keywords, importance from events where id=?", (first,)).fetchone()["importance"] == 3
        out["new_event_after_archive"] = store.conn.execute(
            "select count(*) from events where session='a' and archived=0").fetchone()[0]

        # 关键词上限 8
        store.add_turns("c", [("user", "关键词很多的句子，测试上限", 0)], ts=1_760_000_000)
        base = ev("关键词很多的句子，测试上限", "用户说了一句关键词很多的话",
                  [f"kw{i}" for i in range(6)], 3)
        persist_result(store, "c", window(store, "c"), ExtractResult(events=[base]), now=1_760_000_100)
        store.add_turns("c", [("user", "再加几个关键词的同一句话，测试上限", 0)], ts=1_760_000_200)
        more = ev("再加几个关键词的同一句话，测试上限", "用户又说了一句关键词很多的话",
                  [f"more{i}" for i in range(6)], 4)
        persist_result(store, "c", window(store, "c"), ExtractResult(events=[more]), now=1_760_000_300)
        kws = store.conn.execute(
            "select keywords from events where session='c'").fetchall()
        out["kw_max"] = max(len(str(r["keywords"]).split()) for r in kws) if kws else 0
    return out


def raw_edges() -> dict:
    out = {}
    db = STRESS / "bug-raw.db"
    if db.exists():
        db.unlink()
    cfg = Config(); cfg.db_path = str(db)
    with Store.open(db, cfg) as store:
        long_text = "杭州" + "啊" * 5000
        store.add_turns("s", [("user", long_text, 0)], ts=1_760_000_000)
        rows = store.search_turns("s", ["杭州"], limit=3)
        out["long_truncated_in_render"] = True
        out["found_long"] = len(rows) == 1
        # 重复句去重
        store.add_turns("s2", [("user", "重复的句子内容", 0)], ts=1_760_000_000)
        store.add_turns("s2", [("user", "重复的句子内容", 0)], ts=1_760_000_100)
        out["dedupe_same_text"] = len(store.search_turns("s2", ["重复的句子"], limit=3))
        # 空会话
        out["empty_session"] = store.search_turns("nope", ["杭州"]) == []
        # 渲染截断：pack 里原话行 ≤80 字
        text = pack(store, "s", "杭州怎么样", use_cache=False).text
        line = next((ln for ln in text.splitlines() if ln.startswith("- ") and "杭州" in ln), "")
        out["render_line_len"] = len(line)
    return out


async def http_edges() -> dict:
    db = STRESS / "bug-http.db"
    if db.exists():
        db.unlink()
    env = dict(os.environ, PYTHONPATH=r"C:\h\hippo", PYTHONIOENCODING="utf-8", HIPPO_LOG="off")
    proc = subprocess.Popen([PY, "-m", "hippo.dsh_kernel", "--db", str(db), "--port", str(PORT)],
                            cwd=r"C:\h\hippo", env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = {}
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=20) as c:
            for _ in range(100):
                try:
                    if (await c.get("/health", timeout=2)).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            for q in ("limit=0", "limit=-1", "limit=abc", "limit=999999", "tab=nope&limit=5"):
                r = await c.get(f"/board/data?{q}")
                out[f"board_data_{q}"] = r.status_code
            r = await c.post("/assemble", json={"session": "s", "text": "长" * 100_000})
            out["assemble_100k"] = r.status_code
            r = await c.post("/event", json={"session": "s", "role": "user",
                                             "text": "控制字符\x00\x01\x02 测试"})
            out["event_control_chars"] = r.status_code
            r = await c.get("/stats")
            out["stats_after"] = r.status_code
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return out


def perf() -> dict:
    db = STRESS / "ab-raw.db"
    if not db.exists():
        return {"skipped": "缺 ab-raw.db"}
    cfg = Config(); cfg.db_path = str(db)
    out = {}
    with Store.open(db, cfg) as store:
        sessions = [r[0] for r in store.conn.execute(
            "select distinct session from turns limit 12").fetchall()]
        for label, flag in (("off", False), ("on", True)):
            cfg.retrieve.raw_turns = flag
            times = []
            for i, sid in enumerate(sessions * 8):
                q = "我们刚才说的那个方案怎么样了"
                t0 = time.perf_counter()
                pack(store, sid, q, cfg=cfg, use_cache=False)
                times.append((time.perf_counter() - t0) * 1000)
            times.sort()
            out[label] = {"n": len(times), "p50": round(statistics.median(times), 2),
                          "p95": round(times[int(len(times) * 0.95)], 2),
                          "max": round(times[-1], 2)}
    cfg.retrieve.raw_turns = True
    return out


def main() -> int:
    result = {
        "merge_edges": merge_edges(),
        "raw_edges": raw_edges(),
        "http_edges": asyncio.run(http_edges()),
        "perf": perf(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    (STRESS / "report-bug-1111.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
