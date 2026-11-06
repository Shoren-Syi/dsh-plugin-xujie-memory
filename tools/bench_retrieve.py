"""检索基准（M9/M10 DoD）。

跑法：
    python -m tools.bench_retrieve --stage L1      # 分类层：压缩率与耗时
    python -m tools.bench_retrieve --stage L2      # 摘要层：漏斗与装填

指标只有两个，手册里写死了：
    压缩率  候选 / 全库 ≤ 5%
    耗时    单次 L1 过滤 < 5ms（8000 条）

**关于耗时的口径**：这里报的是 best-of-N，不是平均。
平均会被 GC 和 WAL checkpoint 拉高，而用户感知的是"最快能多快"。
但 best-of-N 会让人误以为线上就是这个数——所以同时报 p50，
两者差距大就说明有抖动，抖动比慢更值得查。
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from hippo.config import load_config
from hippo.core.db import connect
from hippo.core.migrate import migrate
from hippo.core.rank import l2_search
from hippo.core.retrieve import l1_candidates, parse_query
from hippo.core.store import Store

N_EVENTS = 8000
N_ENTITIES = 40          # 8000/40 = 200 条/实体，正好落在手册"主角实体 >200 条"的边界
REPEAT = 20


TOPICS = ["搬家", "房租", "通勤", "加班", "体检", "房贷", "出差", "猫粮"]


def build(n_events: int = N_EVENTS, n_entities: int = N_ENTITIES) -> Store:
    path = Path(tempfile.mkdtemp()) / "bench.db"
    conn = connect(path)
    migrate(conn)
    store = Store(conn)
    ids = [store.upsert_entity(session="s1", canonical=f"人物{i:02d}", type="person")
           for i in range(n_entities)]
    with store.tx():
        for i in range(n_events):
            eid = ids[i % n_entities]
            ts = 1_700_000_000 + i * 60
            store.conn.execute(
                """
                INSERT INTO events (session, event_time, ingest_time, w0, weight,
                                    keywords, summary, raw, entities)
                VALUES ('s1', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, ts, 0.3 + (i % 7) / 10, 0.3 + (i % 7) / 10,
                 f"人物{i % n_entities:02d} {TOPICS[i % len(TOPICS)]} "
                 f"{TOPICS[(i * 3) % len(TOPICS)]}",
                 f"事件{i}", f"这是第{i}条事件", json.dumps([eid])),
            )
        # 直插 events 不会写 kw_index（那是 add_event 干的活）。
        # 第一版忘了这一步，L2 基准跑出来"命中 0 条"，看着像倒排坏了。
        rows = store.conn.execute("SELECT id, keywords FROM events").fetchall()
        pairs = [(t.lower(), r["id"]) for r in rows for t in r["keywords"].split()]
        store.conn.executemany(
            "INSERT OR IGNORE INTO kw_index(token, event_id) VALUES (?,?)", pairs)
    store.conn.commit()
    return store


def run(store: Store, text: str, cap: int) -> tuple[int, float, int]:
    """返回 (候选数, 毫秒, 全库数)。"""
    q = parse_query(text, "s1", store)
    t0 = time.perf_counter()
    res = l1_candidates(store, "s1", q, cap=cap)
    return len(res.event_ids), (time.perf_counter() - t0) * 1000, res.total


def run_l2(store: Store, text: str, repeat: int) -> dict:
    """一次 L2 的漏斗：L1 候选 → 关键词命中 → 装填。"""
    times, out = [], None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = l2_search(store, "s1", text)
        times.append((time.perf_counter() - t0) * 1000)
    return {"cand": out.candidates, "hit": out.matched, "n": len(out.items),
            "tokens": out.total_tokens, "budget": out.budget,
            "best": min(times), "p50": statistics.median(times),
            "degraded": out.degraded}


def run_layer3(store: Store, session: str) -> dict:
    """M11 原话层：100 条真实口吻的句子，量触发率与花费。

    两个口径分开报：
      `判据`  只问"这句话是不是在要细节"，与库里有几条无关
      `下钻`  端到端，受候选/预算/max_raws 影响

    只报后者会把判据的准确率和检索的召回率混成一个数，调参时看不出
    该改哪一层。手册要的 25% 是**判据**口径——"要不要拉"是判据决定的，
    "拉不拉得到"是检索决定的。
    """
    from collections import Counter

    from hippo.core.drill import drill, wants_detail

    cases = [json.loads(line) for line in
             (Path("tests/fixtures/drill_queries.jsonl")).read_text(
                 encoding="utf-8").splitlines() if line.strip()]

    detail = fired = with_cand = hit_cand = 0
    tokens, totals = [], []
    per_turn: Counter[int] = Counter()
    skips: Counter[str] = Counter()
    over = 0

    for r in cases:
        q = r["q"]
        if wants_detail(q):
            detail += 1
        res = l2_search(store, session, q)
        if res.candidates:
            with_cand += 1
        if not res.degraded:
            hit_cand += 1
        out = drill(store, res, text=q)
        if out.fired:
            fired += 1
            per_turn[len(out.drilled)] += 1
            for it in out.drilled:
                tokens.append(it.raw_tokens)
            totals.append(res.total_tokens + out.used)
            if res.total_tokens + out.used > 600:
                over += 1
        else:
            skips[out.rate_hint] += 1

    n = len(cases)
    avg = sum(tokens) / len(tokens) if tokens else 0.0
    print(f"库 {store.conn.execute('SELECT COUNT(*) c FROM events').fetchone()['c']}"
          f" 条事件    session={session}")
    mix = " ".join(f"{k}条×{v}" for k, v in sorted(per_turn.items()))
    print(f"判据 {detail:3d}/{n} ({detail / n:5.0%})   "
          f"下钻 {fired:3d}/{n} ({fired / n:5.0%})   "
          f"有候选 {with_cand:3d}/{n}   [{mix}]")
    if tokens:
        print(f"原文 {avg:.0f} token/条（max {max(tokens)}，min {min(tokens)}）  "
              f"L2+原文 平均 {sum(totals) / len(totals):.0f} / 600 token")
    if hit_cand:
        print(f"命中 {hit_cand} 条里下钻 {fired} 条（{fired / hit_cand:.0%}）"
              f"  ← 判据只是上限，真下钻还要检索命中")
    print("未下钻 " + " / ".join(f"{k}:{v}" for k, v in skips.most_common()))
    ok = detail / n <= 0.25 and over == 0
    if over:
        print(f"  ← {over} 轮超预算")
    print("PASS" if ok else "FAIL")
    return {"detail": detail / n, "fired": fired / n, "over": over, "ok": ok}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="L1", choices=["L1", "L2"])
    ap.add_argument("--layer3-rate", action="store_true",
                    help="M11 原话层触发率（配 --db 用真机库）")
    ap.add_argument("--session", default="main")
    ap.add_argument("--n", type=int, default=N_EVENTS)
    ap.add_argument("--cap", type=int, default=800)
    ap.add_argument("--repeat", type=int, default=REPEAT)
    ap.add_argument("--db", default="", help="用现成的库，不造数据")
    args = ap.parse_args()

    if args.db:
        # 带上配置：不然 bench 跑的是代码里的默认值，环境变量改了也不生效，
        # 而"用环境变量验证某个门槛真的会挡住"正是 M11 要证明的事。
        store = Store.open(args.db, load_config())
        total = store.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    else:
        store = build(n_events=args.n)
        total = args.n

    cases = [
        ("有实体", "人物07搬家"),
        ("实体+时间", "人物07去年冬天搬走之前"),
        ("历史意图", "我以前住在哪"),
        ("无实体", "我最近搬家房租"),
    ]

    if args.layer3_rate:
        r = run_layer3(store, args.session)
        store.close()
        return 0 if r["ok"] else 1

    print(f"库 {total} 条事件    cap={args.cap}    repeat={args.repeat}")

    if args.stage == "L2":
        ok = True
        for label, text in cases:
            r = run_l2(store, text, args.repeat)
            flag = f"  降级:{r['degraded']}" if r["degraded"] else ""
            print(f"{label:6s} {r['cand']:4d} -> {r['hit']:3d} 命中 -> "
                  f"{r['n']} 条 / {r['tokens']}/{r['budget']} token "
                  f"in {r['best']:.2f}ms p50={r['p50']:.2f}ms{flag}")
            if r["tokens"] > r["budget"]:
                flag, ok = "  ← token 超预算", False
        store.close()
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1

    ok = True
    for label, text in cases:
        times, n = [], 0
        for _ in range(args.repeat):
            n, ms, _ = run(store, text, args.cap)
            times.append(ms)
        best, p50 = min(times), statistics.median(times)
        ratio = n / total
        flag = ""
        if label == "有实体" and ratio > 0.05:
            flag, ok = "  ← 压缩率超标", False
        print(f"{label:6s} {total} -> {n:4d} candidates ({ratio:5.1%}) "
              f"in {best:.2f}ms  p50={p50:.2f}ms{flag}")

    store.close()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
