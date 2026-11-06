"""真机探针：用户改口，系统会不会把旧值关掉。

M8.2 的 35 条用例全是我自己构造的输入，走的是 `set_fact` 直连。
它在真机上要成立，中间还隔着两件我控制不了的事：

  1. 模型会不会**再写一条 city**（而不是写个 event 了事）
  2. 模型会不会写成一个新 key（"现居城市"），于是两条并存

所以这个探针造一段"先说北京、后说上海"的对话，看端到端结果。
判定标准只有一条：**同一时刻 active 的 city 只能有一个，且是上海。**

用法：
    python3 -m tools.probe_contradiction
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path

import httpx

from hippo.config import load_config
from hippo.core.db import connect
from hippo.core.encode import Encoder
from hippo.core.migrate import migrate
from hippo.core.persist import persist_result
from hippo.core.store import Store
from hippo.core.turns import record_pair, slice_windows

# 每轮都自然带出事实，且第 4、5 轮是**改口**。
# 刻意不用"我搬家了"这种直白说法：真实用户会说"上个月搬过来了"。
TURNS = [
    # 第 1~5 轮：北京 / 字节。这一批会被切成**第一个窗口**。
    ("我住在北京，在字节做后端，天天挤地铁。", "北京挺好的，字节那边节奏快但还行。"),
    ("对了，我养了只猫叫年糕，三岁了。", "年糕这名字可爱，三岁的猫正是闹腾的时候。"),
    ("嗯，就是有点掉毛。", "掉毛确实麻烦，尤其换季。"),
    ("最近在考虑要不要换个房子。", "北京的房子确实贵，换房要算清楚。"),
    ("先看看吧，也不急。", "不急就对，慢慢挑。"),
    # 第 6~10 轮：改口。这一批是**第二个窗口**。
    # 刻意隔开 5 轮：同一窗口里模型会把"搬到上海"归纳成一条 city=上海，
    # 根本不写北京那条，于是矛盾检测永远没机会触发（第一次跑就是这样）。
    ("哦对了，上个月我已经搬到上海了，房子小一点但通勤近。", "上海通勤确实比北京舒服些。"),
    ("工作也换了，现在在腾讯。", "换到腾讯挺好的，节奏应该能缓一点。"),
    ("对，至少不用天天加班了。", "那挺好的，身体要紧。"),
    ("周末一般就在家陪年糕。", "猫挺独立的，陪一会儿就够。"),
    ("差不多吧，它自己玩得多。", "猫都这样。"),
]


async def run() -> dict:
    cfg = load_config()
    cfg.deepseek.thinking = "disabled"

    tmp = Path(tempfile.mkdtemp(prefix="hippo-c-"))
    db = tmp / "c.db"
    conn = connect(str(db))
    migrate(conn)
    conn.close()

    for u, a in TURNS:
        record_pair(str(db), "contra", u, a)

    cost = 0.0
    raw: list[dict] = []
    store = Store.open(db)
    async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.extract.timeout + 30)) as client:
        enc = Encoder(cfg, client, cfg.deepseek.api_key)
        for w in slice_windows(store.pending_turns("contra"), session="contra",
                               per_window=5, max_tokens=1200):
            if not w.complete:
                continue
            out = await enc.encode(w, known="（暂无）")
            cost += out.cost
            if out.status != "ok":
                print("抽取失败：", out.error)
                continue
            for f in out.result.facts:
                raw.append({"key": f.key, "value": f.value, "entity": f.entity,
                            "conf": f.confidence})
            persist_result(store, "contra", w, out.result)

    facts = [dict(r) for r in store.conn.execute(
        "SELECT f.id, f.key, f.key_raw, f.value, f.card, f.valid_from, f.valid_to,"
        " e.canonical AS who FROM facts f LEFT JOIN entities e ON e.id = f.entity_id"
        " ORDER BY f.id").fetchall()]

    result = {"cost": round(cost, 6), "model_output": raw, "facts": facts}

    # 判定：city / employer 的 active 数必须 = 1，且值必须是最新说的那个
    checks = {}
    for key, want in (("city", "上海"), ("employer", "腾讯")):
        active = [f for f in facts if f["key"] == key and f["valid_to"] is None]
        checks[key] = {
            "active": [f["value"] for f in active],
            "ok": len(active) == 1 and want in active[0]["value"] if active else False,
        }
    result["checks"] = checks
    store.close()
    return result


def main() -> int:
    rep = asyncio.run(run())
    print(f"成本 ¥{rep['cost']:.6f}")
    print("\n模型输出的 fact：")
    for f in rep["model_output"]:
        print(f"  {f['entity']} | {f['key']} = {f['value']}  ({f['conf']})")
    print("\n库里的事实：")
    when = lambda t: datetime.fromtimestamp(t).strftime("%m-%d") if t else "  -  "
    for f in rep["facts"]:
        print(f"  #{f['id']:<3} {f['who'] or '?':<6} {f['key']:<10} "
              f"{f['value']:<20} {f['card']:<7} {when(f['valid_from'])} → {when(f['valid_to'])}")
    print()
    ok = all(c["ok"] for c in rep["checks"].values())
    for k, c in rep["checks"].items():
        print(f"  {k:<10} active={c['active']}  {'OK' if c['ok'] else 'FAIL'}")
    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
