"""情绪与关系：短期会消，长期不会自己好。

方案第六章的全部难点，可以压成一句：

    消气不等于原谅，原谅不等于遗忘。

所以这里是**两条独立的线**，用两套规则：

    情绪 emotion_events      有衰减。半天不生气就不生气了。
    关系 relationship_events 没有衰减。信任掉下去，不会因为过了三十天自己长回来。

把它们合成一条曲线，就会出现「三个月后 AI 自己原谅了」——
那不是仿人，那是没有记性。

---

## 情绪为什么也可以"只记事件"

和权重曲线同一个理由：**存下来的值总有一天会和事实对不上。**

情绪是"被什么推了多少次"的结果。存一个 `anger = 37` 的数字，
就再也答不了"这三十七是怎么来的"。而这句话恰恰是这个模块存在的意义。

所以只记 `emotion_events`（谁在什么时候把哪种情绪推了多少、为什么），
当前强度由 `intensity()` 重放算出来。

## 为什么要有饱和

6.5：避免无条件叠加负面情绪，导致永久敌对或关系无法修复。

纯累加的话，被同一件事激怒二十次就等于 20 倍愤怒，
而人不是这样的——第一次最疼，第十次已经麻了。
所以重放时每一步都夹在 `[0, cap]` 里，越往上越难加。

## 为什么"未了结的事"要单独一张表

因为它是**唯一不随时间消失的东西**。

情绪可以自己退到 0（消气），但只要这条矛盾还开着，
关系就不能自动回暖（没原谅）。方案里那句「不生气，但还等着一个解释」，
就是这张表存在的全部理由。

必测场景 F（高冷角色：未修复关系前不自动恢复亲密）也落在这儿：
`grievances` 有 open 的时候，`closeness` 不许回到 baseline 以上。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .engine import digest

# 情绪种类。名字用英文，因为它们是键；中文只出现在渲染里。
EMOTIONS = ("anger", "joy", "sadness", "anxiety", "grievance")

# 各情绪的**半衰期（天）**。用半衰期而不是衰减系数，
# 因为"多久消一半"是人能直接判断的数，λ 不是。
#
#   anger     半天   —— 气来得快去得也快
#   joy       一天
#   sadness   三天   —— 难过比生气黏人
#   anxiety   一天
#   grievance 两天   —— 委屈散得慢，但不至于留一辈子
_HALF_LIFE_DAYS = {
    "anger": 0.5,
    "joy": 1.0,
    "sadness": 3.0,
    "anxiety": 1.0,
    "grievance": 2.0,
}

# 情绪强度上限。**这个数就是 6.5 说的"不无限累积"。**
EMOTION_CAP = 100.0

# 关系维度。这三个是方案 6.2 列的长期项：信任、亲密、隔阂感、尊重。
# 「隔阂」不单列一维，它是 closeness 的反面——同一根轴的两端，
# 拆成两个字段就会出现"又亲密又隔阂"这种自相矛盾的状态。
RELATION_DIMENSIONS = ("trust", "closeness", "respect")

# 初见时的关系基线。没有这组数，第一次见面的人 trust 会是 0，
# 而 0 意味着"我本来就不信你"——那不是初见，那是结过仇。
_BASELINE = {
    "trust": 50.0,
    "closeness": 30.0,
    "respect": 50.0,
}

RELATION_CAP = 100.0

# 未了结的事的状态
GRIEVANCE_OPEN = "open"
GRIEVANCE_REPAIRED = "repaired"      # 事情说清楚了
GRIEVANCE_FORGIVEN = "forgiven"      # 人原谅了，但事发生过

_NAMES = {
    "anger": "生气",
    "joy": "高兴",
    "sadness": "难过",
    "anxiety": "不安",
    "grievance": "委屈",
    "trust": "信任",
    "closeness": "亲近",
    "respect": "尊重",
}


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def elapsed_days(since: str | None, now: datetime) -> float:
    then = _parse(since)
    if then is None:
        return 0.0
    return max(0.0, (now - then).total_seconds() / 86400.0)


class Emotion:
    """短期情绪。会自己消退。"""

    def __init__(self, engine):
        self.engine = engine
        self._seq = 0

    # ---------------------------------------------------------- 写
    def feel(self, profile: dict, emotion: str, delta: float, *,
             reason: str, evidence_trace_id: str | None = None) -> dict:
        """被一件事推了一下。

        `delta` 正数是加剧，负数是安抚。**必须给 reason**——
        没有原因的情绪，事后答不了"你当时为什么生气"，
        而那个问题正是这个模块要能回答的。
        """
        if emotion not in EMOTIONS:
            raise ValueError(f"未知情绪: {emotion}")
        if not reason:
            raise ValueError("情绪事件必须说明原因")

        self._seq += 1
        event_id = "em_" + digest(
            "emotion", profile["profile_id"], emotion, reason,
            self.engine.now(), str(self._seq),
        )[:20]
        self.engine._conn().execute(
            "INSERT INTO emotion_events(event_id, scope_id, profile_id, emotion,"
            " delta, reason, evidence_trace_id, occurred_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (event_id, profile["scope_id"], profile["profile_id"], emotion,
             float(delta), reason, evidence_trace_id, self.engine.now()),
        )
        return {
            "event_id": event_id,
            "emotion": emotion,
            "intensity": self.intensity(profile, emotion),
        }

    # ---------------------------------------------------------- 读
    def intensity(self, profile: dict, emotion: str,
                  now: datetime | None = None,
                  half_life_days: float | None = None) -> float:
        """**重放**出当前强度。

        逐步重放而不是求和，是为了让饱和生效：

            每到一个事件：先按流逝的时间衰减，再加上这次的影响，夹到上限里

        过一段时间再衰减一次到此刻。

        `half_life_days` 让角色卡覆盖默认衰减速度（7.2「衰减速度」）。
        它只改**气消得多快**，不改"这件事发生过"。
        """
        now = now or _parse(self.engine.now()) or datetime.now(timezone.utc)
        rate = _rate(emotion, half_life_days)

        rows = self.engine._conn().execute(
            "SELECT delta, occurred_at FROM emotion_events"
            " WHERE profile_id=? AND emotion=? ORDER BY occurred_at, rowid",
            (profile["profile_id"], emotion),
        ).fetchall()
        if not rows:
            return 0.0

        value = 0.0
        prev = _parse(rows[0]["occurred_at"]) or now
        for i, row in enumerate(rows):
            at = _parse(row["occurred_at"]) or prev
            if i > 0:
                value *= math.exp(-rate * max(0.0, (at - prev).total_seconds() / 86400.0))
            value = _clamp(value + float(row["delta"]), 0.0, EMOTION_CAP)
            prev = at

        value *= math.exp(-rate * max(0.0, (now - prev).total_seconds() / 86400.0))
        return round(value, 3)

    def mood(self, profile: dict, now: datetime | None = None) -> dict[str, float]:
        """当前所有情绪的强度。0 的不返回——空着的情绪不是信息。"""
        got = {e: self.intensity(profile, e, now=now) for e in EMOTIONS}
        return {e: v for e, v in got.items() if v > 0}

    def dominant(self, profile: dict) -> tuple[str | None, float]:
        """现在最强烈的那个情绪。没有就是 None。"""
        mood = self.mood(profile)
        if not mood:
            return None, 0.0
        name = max(mood, key=lambda k: mood[k])
        return name, mood[name]


class Relationship:
    """长期关系。**不随时间变化**——只有事件能改它。"""

    def __init__(self, engine):
        self.engine = engine
        self._seq = 0

    # ---------------------------------------------------------- 写
    def shift(self, profile: dict, dimension: str, delta: float, *,
              reason: str, is_repair: bool = False,
              evidence_trace_id: str | None = None) -> dict:
        """关系动了一下。

        **为什么不给这个方法加一个"时间衰减"参数**：
        因为那样这个模块就失去了它存在的理由。
        信任掉下去只能靠两件事回来——对方做了新的可信的事，或者把话说开了。
        时间不是其中之一。
        """
        if dimension not in RELATION_DIMENSIONS:
            raise ValueError(f"未知关系维度: {dimension}")
        if not reason:
            raise ValueError("关系事件必须说明原因")

        self._seq += 1
        event_id = "re_" + digest(
            "relation", profile["profile_id"], dimension, reason,
            self.engine.now(), str(self._seq),
        )[:20]
        self.engine._conn().execute(
            "INSERT INTO relationship_events(event_id, scope_id, profile_id,"
            " dimension, delta, reason, evidence_trace_id, is_repair, occurred_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (event_id, profile["scope_id"], profile["profile_id"], dimension,
             float(delta), reason, evidence_trace_id, 1 if is_repair else 0,
             self.engine.now()),
        )
        return {
            "event_id": event_id,
            "dimension": dimension,
            "value": self.value(profile, dimension),
        }

    # ---------------------------------------------------------- 读
    def value(self, profile: dict, dimension: str) -> float:
        """当前值 = 基线 + 全部事件。**没有时间项。**"""
        if dimension not in RELATION_DIMENSIONS:
            raise ValueError(f"未知关系维度: {dimension}")
        row = self.engine._conn().execute(
            "SELECT COALESCE(SUM(delta), 0) s FROM relationship_events"
            " WHERE profile_id=? AND dimension=?",
            (profile["profile_id"], dimension),
        ).fetchone()
        return round(_clamp(_BASELINE[dimension] + float(row["s"]),
                            0.0, RELATION_CAP), 3)

    def state(self, profile: dict) -> dict[str, float]:
        return {d: self.value(profile, d) for d in RELATION_DIMENSIONS}

    def repairs(self, profile: dict) -> int:
        """修复过几次。给"这段关系是不是被认真处理过"一个凭据。"""
        row = self.engine._conn().execute(
            "SELECT COUNT(*) c FROM relationship_events"
            " WHERE profile_id=? AND is_repair=1",
            (profile["profile_id"],),
        ).fetchone()
        return int(row["c"])


class Grievances:
    """未了结的事。**唯一不随时间消失的东西。**

    情绪会退，这条记录不会。它一直开着，直到有人把话说开。
    """

    def __init__(self, engine):
        self.engine = engine
        self._seq = 0

    def open(self, profile: dict, summary: str, *,
             severity: float = 1.0,
             evidence_trace_id: str | None = None) -> dict:
        if not summary:
            raise ValueError("未了结的事必须有概述")
        self._seq += 1
        grievance_id = "gr_" + digest(
            "grievance", profile["profile_id"], summary,
            self.engine.now(), str(self._seq),
        )[:20]
        self.engine._conn().execute(
            "INSERT INTO grievances(grievance_id, scope_id, profile_id, summary,"
            " severity, status, evidence_trace_id, opened_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (grievance_id, profile["scope_id"], profile["profile_id"],
             summary, float(severity), GRIEVANCE_OPEN, evidence_trace_id,
             self.engine.now()),
        )
        return self._by_id(grievance_id) or {}

    def close(self, grievance_id: str, *,
              forgiven: bool = False) -> dict | None:
        """把一件事了结掉。

        `repaired` 和 `forgiven` 都关闭，但**不是一回事**：

            repaired  事情说清楚了（误会解除、对方解释了）
            forgiven  我原谅了（事发生过，我记得，但不再追究）

        两个都留着，因为「他原谅过我一件事」和「那件事讲清楚了」
        在以后翻旧账时是两种完全不同的证据。
        """
        status = GRIEVANCE_FORGIVEN if forgiven else GRIEVANCE_REPAIRED
        self.engine._conn().execute(
            "UPDATE grievances SET status=?, closed_at=? WHERE grievance_id=?",
            (status, self.engine.now(), grievance_id),
        )
        return self._by_id(grievance_id)

    def open_ones(self, profile: dict) -> list[dict]:
        rows = self.engine._conn().execute(
            "SELECT * FROM grievances WHERE profile_id=? AND status=?"
            " ORDER BY severity DESC, opened_at",
            (profile["profile_id"], GRIEVANCE_OPEN),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_of(self, profile: dict) -> list[dict]:
        rows = self.engine._conn().execute(
            "SELECT * FROM grievances WHERE profile_id=? ORDER BY opened_at",
            (profile["profile_id"],),
        ).fetchall()
        return [dict(r) for r in rows]

    def _by_id(self, grievance_id: str) -> dict | None:
        row = self.engine._conn().execute(
            "SELECT * FROM grievances WHERE grievance_id=?", (grievance_id,)
        ).fetchone()
        return dict(row) if row else None


class Mind:
    """三样东西放在一起，因为**它们总是一起被问**。

    「我现在什么心情、跟他什么关系、还有什么事没说开」——
    这是主 AI 决定怎么开口之前要看的全部。

    `character` 是可选的角色卡（阶段五）。它只调节**幅度与倾向**：

        同样被冒犯，直率角色气得多、温和角色气得少（6.3）
        气消得多快，由角色的半衰期决定（7.2）
        气消了之后怎么表现，由 `stance()` 给（7.4）

    它**不能**让一件没发生的事发生，也不能让发生过的事消失。
    温柔角色的气只有 10，但那件事照样开着，照样要有人把话说开。
    """

    def __init__(self, engine, character=None):
        self.engine = engine
        self.character = character
        self.emotion = Emotion(engine)
        self.relationship = Relationship(engine)
        self.grievances = Grievances(engine)

    # ---------------------------------------------------------- 角色调节
    def _half_life(self, emotion: str) -> float | None:
        """角色卡给的半衰期。没给就用默认。"""
        c = self.character
        if c is None:
            return None
        if emotion == "anger":
            return c.anger_half_life_days
        if emotion == "sadness":
            return c.sadness_half_life_days
        return None

    def mood(self, profile: dict, now: datetime | None = None) -> dict[str, float]:
        """当前情绪，按**这个角色**的衰减速度算。"""
        got = {
            e: self.emotion.intensity(profile, e, now=now,
                                      half_life_days=self._half_life(e))
            for e in EMOTIONS
        }
        return {e: v for e, v in got.items() if v > 0}

    def anger(self, profile: dict) -> float:
        return self.emotion.intensity(profile, "anger",
                                      half_life_days=self._half_life("anger"))

    def snapshot(self, profile: dict) -> dict:
        return {
            "mood": self.mood(profile),
            "relationship": self.relationship.state(profile),
            "open_grievances": self.grievances.open_ones(profile),
        }

    def stance(self, profile: dict):
        """此刻大概会怎么做。**不是台词**——台词由主 AI 来写。

        没有角色卡时返回一个中性倾向（不主动、不记嘴、语气平），
        而不是报错：没有角色设定也该能算出一个立场。
        """
        from .character import CharacterSpec, stance as _stance

        c = self.character or CharacterSpec(name="(未设定)")
        rel = self.relationship.state(profile)
        return _stance(
            c,
            anger=self.anger(profile),
            trust=rel["trust"],
            closeness=rel["closeness"],
            open_grievances=len(self.grievances.open_ones(profile)),
        )

    def record_count(self, profile: dict) -> int:
        """这个人的情绪与关系事件一共有多少条。

        给"事实抹不掉"一个可见的数字：性格可以让人气消得更快，
        但那些事件一直在库里。
        """
        row = self.engine._conn().execute(
            "SELECT (SELECT COUNT(*) FROM emotion_events WHERE profile_id=?)"
            " + (SELECT COUNT(*) FROM relationship_events WHERE profile_id=?) c",
            (profile["profile_id"], profile["profile_id"]),
        ).fetchone()
        return int(row["c"])

    def render(self, profile: dict) -> str:
        """人话版。给主 AI 塞进提示词。

        刻意把三件事分开写，因为它们**不能互相换算**：
        气消了不代表关系回来了，关系回来了不代表那件事就没了。

        最后一行是角色倾向。**它不写台词**——只写"会不会先开口、
        嘴上放不放过"。台词留给主 AI，因为同一份倾向可以有很多种说法，
        而写死一句「哼」就是模板了（7.3）。
        """
        snap = self.snapshot(profile)
        lines = []

        if snap["mood"]:
            parts = [f"{_NAMES.get(k, k)} {v:.0f}" for k, v in
                     sorted(snap["mood"].items(), key=lambda kv: -kv[1])]
            lines.append("现在的心情：" + "、".join(parts))
        else:
            lines.append("现在的心情：平静")

        rel = snap["relationship"]
        lines.append("跟他的关系：" + "、".join(
            f"{_NAMES.get(k, k)} {v:.0f}" for k, v in rel.items()))

        if snap["open_grievances"]:
            lines.append("还没说开的事：")
            for g in snap["open_grievances"]:
                lines.append(f"  - {g['summary']}")

        if self.character is not None:
            st = self.stance(profile)
            lines.append("此刻大概会：" + _describe_stance(st))
        return "\n".join(lines)

    # ---------------------------------------------------------- 复合动作
    def offended(self, profile: dict, *, summary: str, severity: float,
                 anger: float, trust_delta: float,
                 reason: str,
                 evidence_trace_id: str | None = None) -> dict:
        """被冒犯了：气上来、信任掉、留一条未了结的事。

        这三件事**必须一起做**，所以合成一个动作。
        少做任何一件都会出现奇怪的状态：

            只加情绪          → 三十天后气消了，关系自动恢复（假的）
            只掉信任          → 他不知道为什么，也看不到有件事没解决
            只记未了结的事    → 情绪上毫无反应，像一台录音机

        给了角色卡时，`anger` 和 `trust_delta` 会被**乘**上角色的敏感度
        （6.3：直率角色 +30，温和角色 +10）。这是角色卡唯一能碰的地方——
        它调幅度，不调有没有这件事。
        """
        c = self.character
        anger_amount = anger * (c.anger_sensitivity if c else 1.0)
        trust_amount = trust_delta * (c.trust_damage if c else 1.0)

        emo = self.emotion.feel(profile, "anger", anger_amount, reason=reason,
                                evidence_trace_id=evidence_trace_id)
        self.emotion.feel(profile, "grievance", severity * 10,
                          reason=reason, evidence_trace_id=evidence_trace_id)
        rel = self.relationship.shift(profile, "trust", trust_amount,
                                      reason=reason,
                                      evidence_trace_id=evidence_trace_id)
        g = self.grievances.open(profile, summary, severity=severity,
                                 evidence_trace_id=evidence_trace_id)
        return {"emotion": emo, "relationship": rel, "grievance": g}

    def repair(self, profile: dict, *, grievance_id: str,
               trust: float = 5.0, closeness: float = 5.0,
               reason: str, forgiven: bool = False,
               evidence_trace_id: str | None = None) -> dict:
        """把话说开了：关掉那件事，关系回暖一点。

        注意这里是**唯一**能让 `closeness` 回到基线以上的路径。
        时间做不到，别的正向事件也做不到——
        方案必测场景 F 说的就是这个：未修复关系前不自动回复亲密。

        角色卡在这里调的是**回流速度**（7.2「关系修复的偏好」）：
        傲娇只回八成，温柔回一点三倍。同样一句道歉，
        在不同的人那里分量不一样——但**都要有那句话**。
        """
        c = self.character
        trust_amount = trust * (c.trust_repair if c else 1.0)
        closeness_amount = closeness * (c.trust_repair if c else 1.0)

        g = self.grievances.close(grievance_id, forgiven=forgiven)
        self.relationship.shift(profile, "trust", trust_amount, reason=reason,
                                is_repair=True,
                                evidence_trace_id=evidence_trace_id)
        self.relationship.shift(profile, "closeness", closeness_amount,
                                reason=reason, is_repair=True,
                                evidence_trace_id=evidence_trace_id)
        # 气消一点，但**不等于消气**——那要看时间。
        self.emotion.feel(profile, "anger", -10.0, reason=reason,
                          evidence_trace_id=evidence_trace_id)
        self.emotion.feel(profile, "grievance", -20.0, reason=reason,
                          evidence_trace_id=evidence_trace_id)
        return {"grievance": g}


def _rate(emotion: str, half_life_days: float | None = None) -> float:
    """半衰期换算成衰减系数：λ = ln2 / 半衰期。

    `half_life_days` 由角色卡给（7.2「衰减速度」）。
    给负数或 0 视为无效，退回默认——宁可让角色卡不生效，
    也不要让一个笔误把情绪变成永不消退。
    """
    half = half_life_days if half_life_days and half_life_days > 0 \
        else _HALF_LIFE_DAYS.get(emotion, 1.0)
    return math.log(2) / half


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


_TONE_WORDS = {
    "sharp": "话里带刺",
    "cold": "语气平淡、不多说",
    "warm": "语气缓和",
    "normal": "语气正常",
}


def _describe_stance(st) -> str:
    """把倾向写成一句人话。**只写行为，不写台词。**"""
    parts = [_TONE_WORDS.get(st.tone, st.tone)]
    parts.append("会先开口" if st.speak_first else "不会先开口")
    if st.will_mention_it:
        parts.append("会提那件事")
    if st.needs_apology:
        parts.append("在等一个解释")
    return "，".join(parts)
