"""M9 真机验证：L1 在真实抽取出来的记忆上，砍得对不对。

单测里的库是我自己插的：40 个实体各 200 条，规规矩矩。
真机的库不长这样——一个窗口能抽出 0 条也能抽出 6 条，实体名是"老王""年糕"
这种两字词，事件时间一半是 guessed。所以这一层必须用真机语料验一次。

**复用库**：`reports/data/eval_l1.db` 存在就直接用。
抽取要花钱（一次约 ¥0.03），每次跑都重抽既慢又没意义——
而且库一变，"这次候选 200 条"和"上次 180 条"就没法比了。

用法：
    python3 -m tools.eval_l1              # 有库就用，没有就抽
    python3 -m tools.eval_l1 --rebuild    # 强制重抽
    python3 -m tools.eval_l1 --dump       # 只打印库里的事件，用于标注
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

from hippo.config import load_config
from hippo.core.db import connect
from hippo.core.encode import Encoder
from hippo.core.extractor import Extractor
from hippo.core.migrate import migrate
from hippo.core.retrieve import l1_candidates, parse_query
from hippo.core.store import Store
from tools.live_extract import CORPUS

DB = Path("reports/data/eval_l1.db")

# 金标：查询 → 必须在 L1 候选里的"事件指纹"（在 raw/summary 里找子串）。
# 指纹刻意选**独有的短语**，不用泛词（"搬家"会在多条里出现）。
# keep=True 表示"这条必须留下"；False 表示"这条必须被砍掉"（负样本）。
CASES: list[tuple[str, str, bool]] = [
    ("老王最近怎么样", "喂猫", True),
    ("老王要去哪出差", "深圳", True),
    ("我搬家搬去了哪", "从朝阳搬到海淀", True),
    ("我房租涨了多少", "两千", True),
    ("我以前住哪个区", "朝阳", True),
    ("我能吃辣吗", "辣", True),
    ("我讨厌什么菜", "香菜", True),
    ("我几点睡", "十一点", True),
    ("老板骂我那次", "当众", True),
    ("我那只猫叫什么", "年糕", True),
    ("我老婆是做什么的", "护士", True),
    ("我在哪上班", "字节", True),
    ("我什么时候要交报告", "季度报告", True),
    ("我要去医院复查什么", "甲状腺", True),
    ("我妈生日是哪天", "生日", True),
    ("我学的什么乐器", "吉他", True),
    ("小李怕坐什么", "飞机", True),
    # 负样本：这条事件被抽取器记到了 2027 年（因为原文里有"孩子明年上小学"），
    # 查"今年"按 AT 过滤把它砍掉是**对的**——它确实不发生在今年。
    # 这条同时验证了时间过滤真的在生效，而不只是"条件拼上去了"。
    ("我今年多大", "三十二", False),
    ("孩子什么时候上学", "小学", True),
    # 负样本：问的是 A，不该把 B 灌进来
    ("老王最近怎么样", "香菜", False),
    ("小李怕坐什么", "香菜", False),
]


async def build() -> float:
    """跑一次真机抽取建库，返回花费。"""
    from hippo.core.turns import record_pair

    if DB.exists():
        DB.unlink()
    conn = connect(str(DB))
    migrate(conn)
    conn.close()

    for name, turns in CORPUS:
        for u, a in turns:
            record_pair(str(DB), name, u, a)

    cfg = load_config()
    cfg.deepseek.thinking = "disabled"
    store = Store.open(DB)
    cost = 0.0
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
        ) as client:
            enc = Encoder(cfg, client, cfg.deepseek.api_key)
            ext = Extractor(store, cfg, enc)
            for name, _turns in CORPUS:
                rep = await ext.run(name, force=True)
                cost += sum(r.cost for r in rep.results)
                print(f"  {name:<12} {[r.status for r in rep.results]}")
    finally:
        store.close()
    return cost


def unify_session(store: Store) -> dict[str, int]:
    """把 15 个窗口并成一个会话，实体同名合并。

    真实部署里只有一个用户，`session` 是"窗口/话题"，不是"人"。
    但 M7 的实体表是 `UNIQUE(session, canonical)`——同一个"老王"在
    narrative 和 multiperson 里是两条记录（已登记缺陷 22）。

    后果在 L1 上很具体：在 narrative 会话里问"老王最近怎么样"能命中，
    在 multiperson 会话里问同一个问题，实体认不出来 → 不加实体条件 →
    返回该会话全库。**检索退化成了全表扫，而且不报错。**

    这一步是评测装置，不是修 bug：它把库摆成"本来的样子"，再去量 L1 能不能砍。
    缺陷 22 本身仍然开着。

    **必须整体成功或整体失败**：改名腾位 → 改 session → 合并，三步里任何一步
    失败都会留下 `用户~6` 这种半成品（这次就留下了），而半成品实体认不出来，
    会让后面所有指标静默变差。
    """
    dup = 0
    with store.tx():
        rows = store.conn.execute(
            "SELECT id, canonical, type FROM entities ORDER BY id").fetchall()
        seen: dict[tuple[str, str], int] = {}
        pending: list[tuple[int, str, str]] = []
        for r in rows:
            key = (r["canonical"], r["type"])
            if key in seen:
                # 临时后缀：不先腾位，改 session 时会撞 UNIQUE(session, canonical)
                store.conn.execute(
                    "UPDATE entities SET canonical = ? WHERE id = ?",
                    (f"{r['canonical']}~{int(r['id'])}", int(r["id"])))
                pending.append((int(r["id"]), r["canonical"], r["type"]))
                dup += 1
            else:
                seen[key] = int(r["id"])
        store.conn.execute("UPDATE entities SET session = 'main'")
        store.conn.execute("UPDATE events SET session = 'main'")
        for tbl in ("facts", "doubts", "plans", "kw_index_none"):
            if tbl == "kw_index_none":
                continue
            store.conn.execute(f"UPDATE {tbl} SET session = 'main'")
        for sid, name, typ in pending:
            store.merge_entities(sid, seen[(name, typ)])
    # 报**累计**合并数而不是本次合并数：归并不可逆，第二次跑本次会是 0，
    # 而报告里的"13 个"和复跑输出对不上时，看报告的人会以为数字是编的。
    total = store.conn.execute(
        "SELECT COUNT(*) c FROM entities WHERE merged_into IS NOT NULL"
    ).fetchone()["c"]
    return {"merged_entities": int(total), "this_run": dup}


def dump(store: Store) -> None:
    rows = store.conn.execute(
        "SELECT e.id, e.session, e.event_time, e.kind, e.superseded_by,"
        " e.weight, e.summary, e.entities"
        " FROM events e ORDER BY e.session, e.id").fetchall()
    for r in rows:
        ents = store.entities_of_event(int(r["id"]))
        names = []
        for eid in ents:
            ent = store.get_entity(eid)
            names.append(ent.canonical if ent else str(eid))
        mark = "*" if r["superseded_by"] else " "
        print(f"{mark}[{r['id']:3d}] {r['session']:<12} {r['summary'][:46]:<48}"
              f" {','.join(names)}")


def evaluate(store: Store) -> dict:
    total = store.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    rows = []
    hit_n = miss_n = fp_n = tn_n = 0
    for text, needle, keep in CASES:
        q = parse_query(text, "main", store)
        ids = l1_candidates(store, "main", q).event_ids
        hit_ids = {int(r["id"]) for r in store.conn.execute(
            "SELECT id FROM events WHERE raw LIKE ? OR summary LIKE ?",
            (f"%{needle}%", f"%{needle}%"))}
        found = bool(hit_ids & set(ids))
        if keep:
            hit_n += found
            miss_n += (not found)
        else:
            fp_n += found
            tn_n += (not found)
        rows.append({
            "q": text, "needle": needle, "keep": keep,
            "expect": len(hit_ids), "found": found,
            "cand": len(ids), "entities": q.entity_names, "mode": q.mode,
        })
    return {"total_events": total, "rows": rows, "merged": 0,
            "must_keep": (hit_n, hit_n + miss_n),
            "must_drop": (fp_n, fp_n + tn_n)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--json", default="reports/data/eval_l1.json")
    args = ap.parse_args()

    if not DB.exists() or args.rebuild:
        print("建库（真机抽取）…")
        cost = asyncio.run(build())
        print(f"  花费 ¥{cost:.4f}")

    store = Store.open(DB)
    try:
        merged = unify_session(store)
        if args.dump:
            dump(store)
            return 0

        t0 = time.perf_counter()
        out = evaluate(store)
        out["merged"] = merged["merged_entities"]
        wall = (time.perf_counter() - t0) * 1000

        k_hit, k_all = out["must_keep"]
        d_fp, d_all = out["must_drop"]
        print(f"库 {out['total_events']} 条事件   {len(out['rows'])} 条查询   {wall:.0f}ms")
        print(f"实体合并 {out['merged']} 个（缺陷 22 的现场）")
        for r in out["rows"]:
            tag = "OK " if r["found"] == r["keep"] else "BAD"
            exp = "必留" if r["keep"] else "必砍"
            ents = ",".join(r["entities"]) or "-"
            print(f"  {tag} {exp} [{r['q']}] 金标 {r['expect']} 条  "
                  f"候选 {r['cand']:3d}  {r['mode']:<10} 实体[{ents}]  "
                  f"{'命中' if r['found'] else '未命中'}")
        print(f"必留命中 {k_hit}/{k_all}   必砍误留 {d_fp}/{d_all}")
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2))
        ok = k_hit == k_all and d_fp == 0
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
