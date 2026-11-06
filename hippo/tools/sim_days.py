"""M25.2 七天模拟：好奇一整天到底会不会变成话痨。

为什么不用真等七天：好奇的行为只跟"时间 + 库里的状态"有关，
把虚拟时钟按 30 分钟一格推过去，和真实挂机七天的执行路径是同一条
（同一个 prepare / take / sweep）。差别只有 CPU 时间。

模拟的是"一个人正常用一周"：
  - 每天 09:30 / 15:00 / 21:00 来说几句话
  - 23:30 也来一次——专门用来验证静默时段真的不出声
  - 前四天会接话，后三天不接（冷遇）
  - 每天都有新的记忆缺口冒出来（不然第五天起就没得问了）
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import Config
from ..core import curiosity as cu
from ..core.db import connect
from ..core.migrate import migrate
from ..core.store import Store

TZ = "Asia/Shanghai"
VISITS = ((9, 30), (15, 0), (21, 0), (23, 30))
# 哪几天会接话。5 天以后不接——连续两次没回应就该闭嘴。
ANSWERS = {1: True, 2: True, 3: True, 4: True, 5: False, 6: False, 7: False}
# 每天新冒出来的缺口（一条过期疑问、一条失效事实交替）
GAP_QUESTIONS = [
    "你说的「那边」是公司还是老家？",
    "上次提到要换工作，后来定了吗？",
    "你妈的手术是这周还是下周？",
    "那个项目最后是谁接的？",
    "你说的「老地方」是哪家店？",
    "你们组的预算最后批了多少？",
    "那只猫后来领养了吗？",
]
# 用户接话时说的是人话，不是复读问题。这里按话题给一句真实回答，
# 用来验证 answer_overlap 到底认不认得出"他在回答这个问题"——
# 如果只有原样复述才算回应，这个检测就是废的。
REPLIES = {
    "那边": "回老家了，没去公司",
    "换工作": "定了，下家是个小公司",
    "手术": "下周三做，医生说问题不大",
    "项目": "最后老李接了",
    "老地方": "就是楼下那家面馆",
    "预算": "批了八十万",
    "猫": "领养了，现在胖得不行",
}


def _at(day: int, hour: int, minute: int, start: datetime) -> int:
    d = start + timedelta(days=day - 1)
    return int(d.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp())


def run(days: int = 7, step: int = 1800, seed: int = 0) -> dict:
    """跑一遍，返回每天的发言数与违规数。"""
    conn = connect(":memory:")
    migrate(conn)
    store = Store(conn)
    cfg = Config()

    start = datetime(2026, 3, 2, 0, 0, tzinfo=ZoneInfo(TZ))   # 周一
    eng = cu.Curiosity(store, cfg)

    # 第一天之前就攒下的两个缺口
    store.add_doubt(session="main", question=GAP_QUESTIONS[0], impact=0.9,
                    expire_days=0, now=_at(1, 0, 5, start) - 9 * 86400)
    eid = store.upsert_entity(session="main", canonical="老王", type="person")
    store.add_event(session="main", raw="老王还在那家创业公司",
                    event_time=_at(1, 0, 5, start) - 20 * 86400,
                    pinned=1, entities=[eid])

    per_day: list[int] = []
    violations = 0
    spoken_log: list[tuple[str, str, str]] = []

    for day in range(1, days + 1):
        # 每天新冒一个缺口
        store.add_doubt(session="main",
                        question=GAP_QUESTIONS[(day - 1) % len(GAP_QUESTIONS)],
                        impact=0.6, expire_days=0,
                        now=_at(day, 0, 10, start) - 86400)

        begins = _at(day, 0, 0, start)
        ends = begins + 86400
        t = begins
        day_spoken = 0
        while t < ends:
            # 到访：用户说话 → 认领回应 → 打包时决定说不说
            for hh, mm in VISITS:
                if _at(day, hh, mm, start) <= t < _at(day, hh, mm, start) + step:
                    store.add_turns("main", [("user", "在吗", 0)], ts=t)
                    if ANSWERS.get(day, False):
                        last = [r for r in store.curiosity_rows("main", limit=3)
                                if r.outcome == "spoken"]
                        reply = "嗯，还行"
                        if last:
                            reply = next(
                                (v for k, v in REPLIES.items()
                                 if k in last[0].question), reply)
                        eng.note_user_turn("main", reply, now=t)
                    q = eng.take("main", t)
                    if q:
                        day_spoken += 1
                        spoken_log.append((eng.quota.day(t),
                                           datetime.fromtimestamp(t, ZoneInfo(TZ)).strftime("%H:%M"),
                                           q))
                        if eng.quota.in_quiet(t):
                            violations += 1
            eng.tick(t)
            t += step
        per_day.append(store.count_curiosity_spoken(
            since=begins, until=ends))

    out = {
        "days": days,
        "speaks": per_day,
        "quiet_violations": violations,
        "max_per_day": max(per_day) if per_day else 0,
        "log": spoken_log,
        "snoozed_days": [
            d for d in range(1, days + 1)
            if store.get_state(
                "main",
                f"cur:snooze:{datetime.fromtimestamp(_at(d, 12, 0, start), ZoneInfo(TZ)):%Y-%m-%d}",
                "") == "1"],
        "queued": store.count_curiosity(),
    }
    store.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="M25 好奇：七天行为模拟")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--step", type=int, default=1800, help="虚拟时钟步长（秒）")
    args = ap.parse_args()

    t0 = time.time()
    r = run(args.days, args.step)
    print(f"day1..{r['days']} speaks={','.join(map(str, r['speaks']))}"
          f" | quiet_violations={r['quiet_violations']}")
    print(f"单日最多 {r['max_per_day']} 次（上限 3）"
          f"　闭嘴日 {r['snoozed_days'] or '无'}"
          f"　共入队 {r['queued']} 条")
    for d, hm, q in r["log"]:
        print(f"  {d} {hm}  {q}")
    print(f"（虚拟 {r['days']} 天，实耗 {time.time() - t0:.2f}s）")
    return 0 if r["max_per_day"] <= 3 and r["quiet_violations"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
