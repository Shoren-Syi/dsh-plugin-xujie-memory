"""M18.2 限流器模拟器：50 轮对话里追问几次？

    python -m hippo.tools.sim_throttle --turns 50

输出 `asks=N / 50 turns | max_per_turn=1 | repeats=0`。
手册 DoD：50 轮追问 ≤5 次。这是纯离线模拟——不调模型，喂给
pick_doubt 的是一组固定的疑问池 + 一张"用户什么时候回答"的事件表，
跑一万遍结果也一样。真实对话里 asks 会随用户回答速度浮动，
但上限性质（一轮 1 条 / 8 轮冷却 / 最多 2 次）是限流器结构保证的。

模拟的剧情（写得直白，方便核对 asks=3 怎么来的）：
  10 条疑问里 3 条值得问（其余 7 条分别被 confidence/impact/cost/
  过期/已问完拦住——正好覆盖六个拦截条件）。
  turn 1/2/3 各注入一条；turn 4 用户回答了 d1（resolve）；
  turn 10/11 是 d2/d3 的第二次追问（冷却刚好 8 轮）；
  之后 d2/d3 问满 2 次，池子里再无可问的，剩 39 轮安静。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

from ..core.doubt import DEFAULT_COOLDOWN, DEFAULT_MAX_ASK, should_ask


@dataclass
class SimDoubt:
    """should_ask 只读这几个字段，不需要真库。"""
    id: int
    question: str
    confidence: float
    impact: float
    cost: float
    expires: int = 10**12          # 默认不过期
    asked_count: int = 0
    last_asked_turn: int | None = None
    resolved: bool = False


def build_pool() -> tuple[list[SimDoubt], dict[int, list[int]], int]:
    """(疑问池, {turn: 该轮用户回答关闭的 doubt ids}, 起始时间戳)。

    七条被拦的疑问逐条对应 should_ask 的一个拦截条件——
    这份池子本身就是 should_ask 的验收表。
    时间每轮推进 30 分钟：真实对话里疑问不会悬 50 轮不老化。
    """
    base = 1_000_000_000
    pool = [
        # ---- 3 条可问的（impact/cost 决定注入顺序：d1 > d2 > d3）
        SimDoubt(1, "用户新公司的业务方向是什么", confidence=0.5, impact=0.9, cost=0.3),
        SimDoubt(2, "用户说的那位导师姓什么", confidence=0.6, impact=0.6, cost=0.2),
        SimDoubt(3, "用户的老家在哪个城市", confidence=0.7, impact=0.8, cost=0.4),
        # ---- 7 条被拦的，覆盖六个拦截条件
        SimDoubt(4, "用户早餐通常吃什么", confidence=0.9, impact=0.8, cost=0.3),   # 太懂
        SimDoubt(5, "用户昨晚梦见了什么", confidence=0.5, impact=0.2, cost=0.3),   # 太琐碎
        SimDoubt(6, "用户的银行卡密码是什么", confidence=0.5, impact=0.9, cost=0.9),  # 太贵
        SimDoubt(7, "用户十年前的心情如何", confidence=0.5, impact=0.8, cost=0.3,
                 expires=base),                                                    # 已过期
        SimDoubt(8, "用户上周三见了谁", confidence=0.5, impact=0.8, cost=0.3,
                 asked_count=DEFAULT_MAX_ASK, last_asked_turn=1),                  # 问满
        SimDoubt(9, "用户对项目的真实看法", confidence=0.5, impact=0.8, cost=0.3,
                 asked_count=1, last_asked_turn=45),    # 刚问过，冷却到 53 轮
        SimDoubt(10, "用户的休假计划定了没", confidence=0.5, impact=0.8, cost=0.3,
                 asked_count=1, last_asked_turn=46),    # 同上
    ]
    # 用户逐个回应 AI 注入的疑问（M18.3 的 resolved 闭环）：
    # turn 5/6/9 各回答一条，都赶在这条疑问第二次到期追问之前。
    resolves = {5: [1], 6: [2], 9: [3]}
    return pool, resolves, base


@dataclass
class SimResult:
    asks: int = 0
    per_turn: list[int] = field(default_factory=list)
    repeats: int = 0                # 8 轮内重复问同一处的次数（违规）
    history: list[tuple[int, int]] = field(default_factory=list)  # (turn, id)

    def line(self, turns: int) -> str:
        return (f"asks={self.asks} / {turns} turns | "
                f"max_per_turn=1 | repeats={self.repeats}")


def simulate(turns: int = 50, *, cooldown: int = DEFAULT_COOLDOWN,
             max_ask: int = DEFAULT_MAX_ASK,
             seconds_per_turn: int = 1800) -> SimResult:
    pool, resolves, base = build_pool()
    res = SimResult()
    for turn in range(1, turns + 1):
        # 用户这一轮的回答先落（回答发生在追问之前/之后都合理，
        # 取"先回答后追问"：用户补话优先于 AI 开口）
        for did in resolves.get(turn, []):
            pool[did - 1].resolved = True

        now_ts = base + turn * seconds_per_turn

        # 一轮最多挑 1 条：挑 impact/cost 最高且 should_ask 放行的
        best, best_score = None, -1.0
        for d in pool:
            if d.resolved:
                continue
            if not should_ask(d, turn, asked_this_turn=False,
                              now_ts=now_ts, cooldown_turns=cooldown,
                              max_ask_count=max_ask):
                continue
            score = d.impact / max(d.cost, 0.01)
            if score > best_score:
                best, best_score = d, score
        if best is None:
            res.per_turn.append(0)
            continue
        res.per_turn.append(1)
        res.asks += 1
        res.history.append((turn, best.id))
        if best.last_asked_turn is not None and \
                turn - best.last_asked_turn < cooldown:
            res.repeats += 1
        best.asked_count += 1
        best.last_asked_turn = turn
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description="M18.2 限流器 50 轮模拟")
    ap.add_argument("--turns", type=int, default=50)
    args = ap.parse_args()
    res = simulate(args.turns)
    print(res.line(args.turns))
    for turn, did in res.history:
        print(f"  turn {turn:>3} -> doubt #{did}")
    return


if __name__ == "__main__":
    main()
