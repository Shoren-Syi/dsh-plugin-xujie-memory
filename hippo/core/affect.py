# -*- coding: utf-8 -*-
"""M20 情绪状态机：衰减 / delta 应用 / 旋钮映射。纯数学，零 token。

设计取向下（对照手册 M20）：

  1. **衰减是 lazy 的**（手册推荐路线）。affect 表存的是"上次写下的
     状态 + updated 时间戳"，读到时按 Δt 现算衰减——不写库、无副作用、
     幂等。写只发生在 persist 应用 delta 时（decay 到当前时刻 → 叠加 →
     抑制 → clamp → 落库并刷 updated）。
  2. **τ 是分钟级**：joy 180 / sadness 360 / anger 10 / fear 30 /
     disgust 240 / surprise 3。anger 快是因为"气一会儿就消"是人格
     的一部分；surprise 最快是因为惊讶本质是信息，知道就不是惊讶了。
  3. **baseline 不是 0**：人格基线决定"它本来是什么样的人"，衰减的
     终点是基线不是零点。全零基线的 AI 是死鱼眼，不是冷静。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# 手册默认 τ（分钟）。config.affect.tau_minutes 覆盖。
# M21.2 直觉拟合校准（2026-11-06，夹具 affect_scenarios.jsonl，见 R-21.2）：
#   fear 30→14（"十几分钟平复"）、sadness 360→480（"一下午缓不过来"）、
#   surprise 3→5（惊讶的尾巴比想象长一点）。joy/anger/disgust 偏差 <30% 保留。
TAU_MINUTES: dict[str, float] = {
    "joy": 180, "sadness": 480, "anger": 10,
    "fear": 14, "disgust": 240, "surprise": 5,
}

MOODS = ("joy", "sadness", "anger", "fear", "disgust", "surprise")

# 手册默认基线。config.affect.baseline 覆盖。
BASELINE: dict[str, float] = {
    "joy": 60, "sadness": 10, "anger": 5,
    "fear": 5, "disgust": 5, "surprise": 20,
}


# ============================================================ M20.1 衰减

def decay_value(v: float, baseline: float, minutes: float, tau: float) -> float:
    """单维衰减。手册公式：v += (baseline - v) * (1 - exp(-Δt/τ))。

    Δt≤0 或 τ≤0 时原样返回（不过期的时间不该改变状态，坏参数
    宁可退化为不衰减也别炸成 NaN）。
    """
    if minutes <= 0 or tau <= 0:
        return v
    return v + (baseline - v) * (1.0 - math.exp(-minutes / tau))


def decay_moods(moods: dict[str, float], *, baseline: dict[str, float],
                tau: dict[str, float], minutes: float) -> dict[str, float]:
    """六维整体衰减，clamp 后返回。MOODS 之外的键原样保留
    （config 里若扩了自定义维度，别在这里弄丢）。"""
    out: dict[str, float] = {}
    for m, v in moods.items():
        if m not in MOODS:
            out[m] = v
            continue
        b = baseline.get(m, BASELINE[m])
        t = tau.get(m, TAU_MINUTES[m])
        out[m] = max(0.0, min(100.0, decay_value(v, b, minutes, t)))
    return out


# ============================================================ M20.2 delta

# 手册交叉抑制矩阵：外层维度的**正向** delta 压制/带动其它维度。
#   joy: {sadness: -0.3, anger: -0.2}  意为 joy 上升时 sadness 的 delta
#   额外 -0.3*joy_delta、anger 额外 -0.2*joy_delta。
# 只作用于正向（下降不该牵连别人——"我高兴了"不该把别人的悲伤拉回来）。
INHIBITION: dict[str, dict[str, float]] = {
    "joy": {"sadness": -0.3, "anger": -0.2},
    "anger": {"joy": -0.4, "fear": -0.2},
    "fear": {"anger": -0.3},
    "sadness": {"joy": -0.2},
}

DELTA_CAP = 20.0   # 手册：单次 delta 上限 ±20，一句话不许把情绪打满


def apply_delta(current: dict[str, float], delta: dict[str, float], *,
                inhibition: dict[str, dict[str, float]] | None = None,
                cap: float = DELTA_CAP) -> dict[str, float]:
    """delta 应用三步：cap → 交叉抑制 → clamp。返回新状态（不改入参）。

    顺序是手册钦定的：先限幅（抑制读到的才是真实幅度），再牵连，
    最后夹界。反过来的话 clamp 会把抑制"夹没"（先到 100 再被压
    等于白压），cap 后置会让抑制放大失控的 delta。
    """
    inh = INHIBITION if inhibition is None else inhibition
    # 1) cap：防止一句话把情绪打满。负向同样限幅（-80 的 sadness
    #    delta 一样是异常输出）。
    d = {m: max(-cap, min(cap, float(delta.get(m, 0.0)))) for m in MOODS}

    # 2) 交叉抑制：只对正向 delta 牵连。累加而非覆盖——多个来源同时
    #    压制同一个维度时应叠加（joy↑ 和 sadness↑ 同时发生，anger
    #    收到两份压制）。
    adj = {m: 0.0 for m in MOODS}
    for src, targets in inh.items():
        if d.get(src, 0.0) > 0:
            for tgt, coef in targets.items():
                if tgt in adj:
                    adj[tgt] += coef * d[src]
    d = {m: d[m] + adj[m] for m in MOODS}

    # 3) clamp：终值不许越界。
    return {m: max(0.0, min(100.0, current.get(m, 0.0) + d[m])) for m in MOODS}


# ============================================================ M20.3 旋钮

@dataclass
class Knobs:
    """四旋钮。0 是中性，±1 是明显偏移。语义见手册 M20.3 映射表。"""
    verbosity: float = 0.0        # 句长
    initiative: float = 0.0       # 主动追问
    warmth: float = 0.0           # 语气温度
    recall_breadth: float = 0.0   # 检索广度


# 显著偏移阈值：偏离基线超过它才驱动旋钮。太小则每轮都在变，
# 模型读到的指令抖来抖去；太大则情绪"看起来影响不了行为"。
_SIGMA = 15.0


def knobs_of(moods: dict[str, float], *,
             baseline: dict[str, float] | None = None) -> Knobs:
    """六维 → 四旋钮。基线相对偏移驱动，不是绝对值驱动——
    基线 joy 70 的人常年 62 不算"低落"，基线 55 的人 62 是真的愉快。
    """
    base = baseline or BASELINE
    off = {m: moods.get(m, base.get(m, 0.0)) - base.get(m, 0.0) for m in MOODS}
    k = Knobs()
    # joy↑：warmth+、verbosity+（手册映射表）
    if off["joy"] > _SIGMA:
        k.warmth += 0.5
        k.verbosity += 0.5
    # sadness↑：verbosity−、initiative−
    if off["sadness"] > _SIGMA:
        k.verbosity -= 0.5
        k.initiative -= 0.5
    # anger↑：verbosity−、直接度+（归入 warmth− 侧的"不绕弯"）、recall+
    if off["anger"] > _SIGMA:
        k.verbosity -= 0.5
        k.recall_breadth += 0.5
    # fear↑：initiative−（确认倾向并进"少自作主张"）
    if off["fear"] > _SIGMA:
        k.initiative -= 0.5
    # disgust↑：warmth−
    if off["disgust"] > _SIGMA:
        k.warmth -= 0.5
    # surprise↑：initiative+（追问）
    if off["surprise"] > _SIGMA:
        k.initiative += 0.5
    return k


# 旋钮 → 行为短语。出现在一行指令里，不解释为什么。
_WARMTH = {1: "语气很暖", 0.5: "语气轻快", -0.5: "语气偏冷", -1: "明显疏远"}
_INIT = {1: "主动追问+1", 0.5: "可追问一句", -0.5: "少主动发问", -1: "不主动发问"}
_VERB = {1: "话可以多", 0.5: "话可稍多", -0.5: "话少", -1: "话极短"}
_RECALL = {0.5: "检索广度大", 1: "检索广度大"}

# 情绪自述黑名单（验收 #2 的正则校验）。"不写'我现在很生气'"是手册原话。
_SELF_DECLARE = re.compile(
    r"我(很|好|超级|特别|真的|现在)?(生气|愤怒|难过|伤心|害怕|恐惧|开心|高兴|愉悦|恶心|惊讶|激动)"
)


def render_line(moods: dict[str, float], *,
                baseline: dict[str, float] | None = None) -> str:
    """六维 → 一行 ≤60 字的行为指令。

    格式：`[状态] 愉悦62 愤怒8 → 语气轻快，主动追问+1，检索广度中`。
    只列显著维度（偏离基线 ≥15），四旋钮全中性时只报数字不带指令。
    """
    base = baseline or BASELINE
    label = {"joy": "愉悦", "sadness": "难过", "anger": "愤怒",
             "fear": "紧张", "disgust": "反感", "surprise": "好奇"}
    # 缺省按基线算，不按 0 算——测试只传 {"joy": 80} 时其它维度不是
    # "0"，是"没读到"，等于基线。按 0 算的话 surprise 0-20=-20 会被
    # 误判成显著偏移，行里出现"好奇0"这种鬼东西。
    off = {m: moods.get(m, base.get(m, 0.0)) - base.get(m, 0.0) for m in MOODS}
    sig = [m for m in MOODS if abs(off[m]) >= _SIGMA]
    head = "[状态] " + " ".join(
        f"{label[m]}{moods.get(m, 0.0):.0f}" for m in sig) if sig else "[状态] 平静"

    k = knobs_of(moods, baseline=base)
    phrases: list[str] = []
    for knob, table in ((k.warmth, _WARMTH), (k.initiative, _INIT),
                        (k.verbosity, _VERB), (k.recall_breadth, _RECALL)):
        if knob == 0:
            continue   # 零旋钮不给短语——"语气偏冷"凭空出现比没有更糟
        # 取符号一致的最接近档位
        cand = [v for v in table if (v > 0) == (knob > 0)] or list(table)
        best = min(cand, key=lambda v: abs(abs(knob) - v))
        phrases.append(table[best])
    # 去重保序，逐个装配——60 字装不下就丢次要的（温度 > 追问 > 话长 > 广度）
    seen: list[str] = []
    for p in phrases:
        if p not in seen:
            seen.append(p)
    tail = "→ " + "，".join(seen) if seen else "→ 一切照常"
    line = f"{head} {tail}"
    if len(line) > 60:
        # 短语从后往前丢，直到 ≤60；全丢还不行就砍尾部短语列表
        while len(seen) > 1 and len(f"{head} → " + "，".join(seen)) > 60:
            seen.pop()
        line = f"{head} → " + "，".join(seen)
    assert len(line) <= 60, f"render_line 超长: {line!r} ({len(line)})"
    return line


# ============================================================ M21.1 预设
#
# "它本来是什么样的人"由 baseline 决定。三套预设的手册口径：
#   warm    joy 70 / 温和——高暖起点，难过恢复慢
#   neutral joy 55 / 平和——默认
#   sharp   joy 45 / 直接——低暖起点，anger 基线略高（说话带锋）
# τ 三套共用：性格决定"平时什么样"，事件决定"现在什么样"，恢复速度
# 是人的生理属性，不随人设变（手册 M21.2 也是单独标 τ 的）。
PRESETS: dict[str, dict[str, float]] = {
    "warm": {
        "joy": 70, "sadness": 8, "anger": 3,
        "fear": 4, "disgust": 4, "surprise": 25,
    },
    "neutral": dict(BASELINE),
    "sharp": {
        "joy": 45, "sadness": 12, "anger": 10,
        "fear": 5, "disgust": 8, "surprise": 15,
    },
}
