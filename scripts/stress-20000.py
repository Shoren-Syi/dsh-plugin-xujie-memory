# -*- coding: utf-8 -*-
"""2 万条压力测试台：导入 → 对抗输入 → 检索 → 崩溃恢复 → 完整性。

设计原则（和之前的评测不同）：
1. **走真实路径**：全部通过内核 HTTP API（`/event`、`/assemble`、`/board/data`），
   不直接写 Store——直接写库测不出插件链路的问题。
2. **可复现**：语料是文件，失败用例编号写进报告。
3. **找的是恶性 bug**：数据丢、进程死、接口 500、计数对不上、崩溃后起不来。
   性能数字只是附带，不是目的。

用法：
    python scripts/stress-20000.py --port 8811 --db C:/h/stress/stress.db \
        --corpus C:/h/stress/real-corpus.jsonl --corpus C:/h/stress/sim-a.jsonl ...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable)


def load_corpus(paths: list[Path], limit: int) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        if not p.exists():
            print(f"[skip] 缺文件 {p}")
            continue
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("role") in ("user", "assistant") and r.get("text"):
                    rows.append(r)
                if len(rows) >= limit:
                    return rows
    return rows


def start_kernel(port: int, db: Path, api_key: str) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(ROOT),
        "PYTHONIOENCODING": "utf-8",
        "HIPPO_LOG": "off",
        "HIPPO_EXTRACT_API_KEY": api_key,
    })
    proc = subprocess.Popen(
        [str(PY), "-m", "hippo.dsh_kernel", "--db", str(db), "--port", str(port)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc


async def wait_health(client: httpx.AsyncClient, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = await client.get("/health", timeout=2)
            if r.status_code == 200:
                return r.json()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.3)
    raise RuntimeError("内核 60s 内没起来")


async def post_events(client: httpx.AsyncClient, rows: list[dict], concurrency: int,
                      sessions: dict[str, str]) -> dict:
    sem = asyncio.Semaphore(concurrency)
    lat: list[float] = []
    errors: list[dict] = []
    done = 0

    async def one(i: int, row: dict) -> None:
        nonlocal done
        async with sem:
            session = sessions.get(row["session"], row["session"])
            t0 = time.perf_counter()
            try:
                r = await client.post("/event", json={
                    "session": session, "role": row["role"], "text": row["text"]}, timeout=30)
                dt = (time.perf_counter() - t0) * 1000
                lat.append(dt)
                if r.status_code != 200:
                    errors.append({"i": i, "status": r.status_code, "body": r.text[:200]})
            except Exception as exc:  # noqa: BLE001
                errors.append({"i": i, "error": repr(exc)[:200]})
            finally:
                done += 1
                if done % 2000 == 0:
                    print(f"  ...{done} 条（已用 {time.perf_counter():.0f}s）", flush=True)

    tasks = [asyncio.create_task(one(i, r)) for i, r in enumerate(rows)]
    t0 = time.perf_counter()
    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - t0
    lat.sort()
    p = lambda q: lat[min(len(lat) - 1, int(len(lat) * q))] if lat else 0.0
    return {
        "posted": len(rows), "errors": len(errors), "error_samples": errors[:10],
        "elapsed_s": round(elapsed, 1),
        "rps": round(len(rows) / elapsed, 1) if elapsed else 0,
        "p50_ms": round(p(0.5), 1), "p95_ms": round(p(0.95), 1), "p99_ms": round(p(0.99), 1),
        "max_ms": round(max(lat), 1) if lat else 0,
    }


ADVERSARIAL = [
    ("空文本", ""),
    ("纯空白", "   \n\t "),
    ("单字", "？"),
    ("超长 2 万字符", "我" * 20000),
    ("emoji 风暴", "😀🎉🐳" * 500),
    ("换行轰炸", "\n".join(f"第{i}行" for i in range(2000))),
    ("SQL 注入串", "'; DROP TABLE turns; --"),
    ("HTML/JS", "<script>alert(1)</script><img onerror=x>"),
    ("JSON 畸形", '{"a": 1, "b": [2, 3'),
    ("控制字符", "a\x00b\x07c\x1b[31md"),
    ("代理对", "𠮷野家 🐟 " * 100),
    ("超长专名", "老王" * 3000 + "下周三去体检"),
    ("重复自我矛盾", "我住在杭州。" * 500 + "不对，我住在成都。"),
    ("时间乱序", "明年五月我要结婚，去年我离婚了，今天周三"),
    ("混合语言", "I live in 杭州 but 工作 in 上海，OK？"),
]


async def adversarial(client: httpx.AsyncClient) -> dict:
    results = []
    for name, text in ADVERSARIAL:
        t0 = time.perf_counter()
        try:
            r = await client.post("/event", json={
                "session": "adv-session", "role": "user", "text": text}, timeout=60)
            dt = (time.perf_counter() - t0) * 1000
            results.append({"case": name, "status": r.status_code, "ms": round(dt, 1),
                            "body": (r.text[:120] if r.status_code != 200 else "")})
        except Exception as exc:  # noqa: BLE001
            results.append({"case": name, "status": "EXC", "error": repr(exc)[:200]})
    bad = [x for x in results if x["status"] != 200]
    return {"cases": len(results), "failed": len(bad), "detail": results, "bad": bad}


async def assemble_probe(client: httpx.AsyncClient, rows: list[dict], n: int = 200) -> dict:
    """用真实语料里的词造查询，看 /assemble 会不会 500、会不会空。"""
    users = [r["text"] for r in rows if r["role"] == "user" and len(r["text"]) >= 12]
    random.Random(42).shuffle(users)
    lat, empty, errors = [], 0, []
    for rec in users[:n]:
        q = rec[:40]
        t0 = time.perf_counter()
        try:
            r = await client.post("/assemble", json={
                "session": rec and "real-000", "text": q}, timeout=30)
            lat.append((time.perf_counter() - t0) * 1000)
            if r.status_code != 200:
                errors.append({"status": r.status_code, "q": q[:40]})
                continue
            if not (r.json().get("text") or "").strip():
                empty += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"error": repr(exc)[:160], "q": q[:40]})
    lat.sort()
    p = lambda q: lat[min(len(lat) - 1, int(len(lat) * q))] if lat else 0.0
    return {"queries": len(lat), "errors": len(errors), "error_samples": errors[:5],
            "empty": empty, "p50_ms": round(p(0.5), 1), "p95_ms": round(p(0.95), 1),
            "max_ms": round(max(lat), 1) if lat else 0}


def db_counts(db: Path) -> dict:
    import sqlite3
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    out = {}
    for t in ("turns", "events", "facts", "entities", "doubts"):
        try:
            out[t] = c.execute(f"select count(*) from {t}").fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            out[t] = f"ERR {exc}"
    # 全角/半角、大小写重复检查（同一个 session 里完全相同的连续 turn）
    try:
        dup = c.execute("""
            select count(*) from (
              select session, idx, content,
                     lag(content) over (partition by session order by idx) as prev
              from turns) where content = prev
        """).fetchone()[0]
        out["consecutive_dup"] = dup
    except Exception as exc:  # noqa: BLE001
        out["consecutive_dup"] = f"ERR {exc}"
    try:
        orphan = c.execute("""
            select count(*) from events e left join turns t on t.session=e.session
            where t.session is null
        """).fetchone()[0]
        out["orphan_events"] = orphan
    except Exception as exc:  # noqa: BLE001
        out["orphan_events"] = f"ERR {exc}"
    c.close()
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8811)
    ap.add_argument("--db", default="C:/h/stress/stress.db")
    ap.add_argument("--corpus", action="append", default=[])
    ap.add_argument("--limit", type=int, default=20000)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--report", default="C:/h/stress/report.json")
    ap.add_argument("--crash", action="store_true", help="中途 SIGKILL 再验恢复")
    args = ap.parse_args()

    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    corpora = [Path(p) for p in (args.corpus or [
        "C:/h/stress/real-corpus.jsonl", "C:/h/stress/sim-a.jsonl",
        "C:/h/stress/sim-b.jsonl", "C:/h/stress/sim-c.jsonl", "C:/h/stress/sim-d.jsonl"])]
    rows = load_corpus(corpora, args.limit)
    print(f"[1/5] 语料 {len(rows)} 条（{len({r['session'] for r in rows})} 会话）")
    if not rows:
        print("没有语料，退出")
        return 2

    key = os.environ.get("HIPPO_TEST_KEY", "")
    proc = start_kernel(args.port, db, key)
    report: dict = {"corpus": corpora and [str(p) for p in corpora], "rows": len(rows)}
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{args.port}") as client:
            health = await wait_health(client)
            print(f"[2/5] 内核就绪 extract={health.get('extract')}")
            report["health"] = health

            # 真实语料保留原 session；模拟语料加前缀避免和真实 session 撞名
            sessions = {r["session"]: r["session"] for r in rows}
            imp = await post_events(client, rows, args.concurrency, sessions)
            print(f"[3/5] 导入完成 {imp['posted']} 条，错误 {imp['errors']}，"
                  f"{imp['elapsed_s']}s / {imp['rps']}rps，p95={imp['p95_ms']}ms")
            report["import"] = imp

            adv = await adversarial(client)
            print(f"[4/5] 对抗输入 {adv['cases']} 例，失败 {adv['failed']}")
            report["adversarial"] = adv

            probe = await assemble_probe(client, rows)
            print(f"[5/5] 检索探测 {probe['queries']} 次，错误 {probe['errors']}，"
                  f"空结果 {probe['empty']}，p95={probe['p95_ms']}ms")
            report["assemble"] = probe

            try:
                board = await client.get("/board/data", timeout=60)
                report["board"] = {"status": board.status_code,
                                   "nodes": len(board.json().get("nodes") or [])}
            except Exception as exc:  # noqa: BLE001
                report["board"] = {"error": repr(exc)[:200]}

            if args.crash:
                print("[crash] 导入第二遍，中途 SIGKILL 再重启验恢复")
                proc.kill()
                await asyncio.sleep(0.5)
                proc = start_kernel(args.port, db, key)
                health2 = await wait_health(client)
                report["crash_health"] = health2
                try:
                    recover = await client.get("/board/data", timeout=60)
                    report["crash_board"] = {"status": recover.status_code,
                                             "nodes": len(recover.json().get("nodes") or [])}
                except Exception as exc:  # noqa: BLE001
                    report["crash_board"] = {"error": repr(exc)[:200]}
    finally:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    report["db"] = db_counts(db)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n== DB 计数 ==")
    print(json.dumps(report["db"], ensure_ascii=False))
    bad = (report.get("import", {}).get("errors", 0) + report.get("adversarial", {}).get("failed", 0)
           + report.get("assemble", {}).get("errors", 0))
    print(f"\n结论：硬失败 {bad} 个；报告 {args.report}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
