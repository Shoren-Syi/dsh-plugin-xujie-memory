"""角色卡：性格怎么调节情绪、关系和行为。

方案第七章。最要紧的一句是 7.3：

> 「傲娇」不意味着每句都说「哼」。

所以这一层**不生成台词**。它输出的是**倾向**：

    现在会不会先开口
    嘴上放不放过
    要不要对方给个解释

台词由主 AI 根据这份倾向去写。把「哼」写进角色卡，
三个傲娇角色就会说同一句话——那不是角色，是模板。

---

## 角色卡调的是幅度，不是事实

这是这一层的边界，也是它安全的原因。

角色卡可以决定「同一句话，直率角色 +30，温和角色 +10」（6.3），
可以让气消得快一点或慢一点，可以在修复时多回一点信任。

但它**不能**让一件没发生的事发生，也不能让一件发生过的事消失。
一个温柔的角色被冒犯了，气可能只有 10——但那件事依然开着，
依然要有人把话说开。**性格决定反应，不决定事实。**

## 为什么用乘数而不是绝对值

因为绝对值会让角色卡变成"重新定义一遍情绪系统"。
写成 `anger_sensitivity = 1.5` 而不是 `anger = 60`，
意味着角色卡只描述"这个人比别人激烈多少"——
基准变了，角色跟着变，不会出现"角色卡里的数字和默认值打架"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CharacterSpec:
    """一个角色的设定。字段都是**调节量**，不是事实。"""

    name: str
    # 性格标签。只用于展示与检索，**不参与任何计算**。
    # 参与计算的永远是下面那些具体参数——
    # 否则"傲娇"就会变成一个写死的分支。
    persona: str = ""

    # ---- 情绪：同样的事，各人的反应幅度与消退速度不同（6.3）
    anger_sensitivity: float = 1.0      # 被冒犯时气多少的乘数
    anger_half_life_days: float | None = None    # None = 用默认（半天）
    sadness_half_life_days: float | None = None

    # ---- 关系：掉得多快、修得多快
    trust_damage: float = 1.0           # 信任受损的乘数
    trust_repair: float = 1.0           # 修复时信任回升的乘数

    # ---- 行为倾向（不是台词）
    speak_first: bool = False           # 有隔阂时会不会主动开口
    hold_words: bool = False            # 气消了，嘴上还放不放过
    demand_explanation: bool = False    # 要不要对方给个解释

    # ---- 表达强度。给主 AI 的一个软提示，不参与判定。
    intensity: str = "normal"           # soft / normal / sharp

    def __post_init__(self) -> None:
        if self.anger_sensitivity <= 0 or self.trust_damage <= 0:
            raise ValueError(f"{self.name}: 乘数必须为正")
        if self.trust_repair <= 0:
            raise ValueError(f"{self.name}: 修复乘数必须为正")


@dataclass
class Stance:
    """此刻的行为倾向。**这不是台词，是台词该怎么写。**

    分开的理由：7.4 那三个角色说的三句话，背后是**同一份状态**。
    状态是算出来的，说法是写出来的。这一层只负责算。
    """

    tone: str = "normal"                # cold / normal / warm / sharp
    speak_first: bool = False
    will_mention_it: bool = False
    needs_apology: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "tone": self.tone,
            "speak_first": self.speak_first,
            "will_mention_it": self.will_mention_it,
            "needs_apology": self.needs_apology,
            "reasons": list(self.reasons),
        }


class CharacterRegistry:
    """从 `specs/characters/*.json` 读角色卡。"""

    def __init__(self, specs: dict[str, CharacterSpec]):
        self._specs = specs

    @classmethod
    def from_dir(cls, path: str | Path) -> "CharacterRegistry":
        specs: dict[str, CharacterSpec] = {}
        for f in sorted(Path(path).glob("*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            spec = CharacterSpec(
                name=data["name"],
                persona=data.get("persona", ""),
                anger_sensitivity=float(data.get("anger_sensitivity", 1.0)),
                anger_half_life_days=data.get("anger_half_life_days"),
                sadness_half_life_days=data.get("sadness_half_life_days"),
                trust_damage=float(data.get("trust_damage", 1.0)),
                trust_repair=float(data.get("trust_repair", 1.0)),
                speak_first=bool(data.get("speak_first", False)),
                hold_words=bool(data.get("hold_words", False)),
                demand_explanation=bool(data.get("demand_explanation", False)),
                intensity=data.get("intensity", "normal"),
            )
            specs[spec.name] = spec
        return cls(specs)

    def get(self, name: str) -> CharacterSpec:
        if name not in self._specs:
            raise KeyError(f"没有这个角色: {name}")
        return self._specs[name]

    def all(self) -> list[CharacterSpec]:
        return list(self._specs.values())


def stance(
    character: CharacterSpec,
    *,
    anger: float,
    trust: float,
    closeness: float,
    open_grievances: int,
) -> Stance:
    """此刻这个角色大概会怎么做。

    只看四样东西：还有多气、信任多少、多亲近、有没有事没说开。
    这四样都是**别的层算出来的**，角色卡不重新定义它们。

    判断顺序本身就是立场：

        1. 还在气头上 → 谁都不主动，嘴上也不客气
        2. 气消了但事没关 → 这才是 7.4 那三个角色真正分岔的地方
        3. 事关了 → 正常
    """
    st = Stance()

    # (1) 气还没消。这时候性格差异会被压平——
    # 再温柔的人，正在气头上也不会主动递台阶。
    if anger >= 40:
        st.tone = "sharp" if character.hold_words or character.intensity == "sharp" else "cold"
        st.speak_first = False
        st.will_mention_it = True
        st.needs_apology = True
        st.reasons.append(f"还在生气（{anger:.0f}）")
        return st

    # (2) 气消了，但还有事没说开。**7.4 就是这一段。**
    if open_grievances > 0:
        st.will_mention_it = True
        st.needs_apology = character.demand_explanation or closeness < 40

        if character.speak_first:
            # 温柔：愿意主动把话说开
            st.tone = "warm"
            st.speak_first = True
            st.reasons.append("气消了，还有事没说开；这个角色愿意先开口")
        elif character.hold_words:
            # 傲娇：心里不怎么生气了，嘴上不放过
            st.tone = "sharp"
            st.speak_first = False
            st.reasons.append("气消了，还有事没说开；嘴上不肯先软")
        else:
            # 高冷：气消了，隔阂还在，等对方开口
            st.tone = "cold"
            st.speak_first = False
            st.reasons.append("气消了，还有事没说开；不会主动提")

        # 气虽然消了，但如果信任已经很低，傲娇的"嘴硬"会变成真的疏远
        if trust < 30 and character.hold_words:
            st.reasons.append("信任已经很低，嘴硬不再只是嘴硬")
        return st

    # (3) 没事了。
    st.tone = "warm" if closeness >= 50 else "normal"
    st.speak_first = character.speak_first
    st.reasons.append("没有未了结的事")
    return st
