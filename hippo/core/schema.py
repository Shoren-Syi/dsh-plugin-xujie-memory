"""抽取结果 schema 与模型输出校验（M5.1）。

注意区分两个 models：
  - `core/models.py`  —— 数据库行。字段与列一一对应，不做任何"聪明"的修正。
  - `core/schema.py`  —— **LLM 的输出**。模型会给你 0.85 写成 8.5、给 null、
                         给 markdown 围栏、给你三句寒暄再给 JSON。
                         所以这里每一层都要能兜住。

三件事：
  1. `ExtractResult` —— 把模型的自由发挥收敛成确定的形状。
  2. `parse_result()` —— 从"好的，以下是结果：\n```json\n{...}\n```\n希望有帮助"里挖出 JSON。
  3. `anchor_raw()` —— **raw 逐字校验**。模型最爱"顺手润色"，
     而原文是唯一真相源。锚不上的条目宁可丢弃，不可入库。

第 3 条是这一层最重要的东西。没有它，三个月后你会看到 AI 笃定地引用
一句用户从未说过的话，而且查不出来是哪一步编的。
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .keywords import normalize_keywords

__all__ = [
    "EntityRef", "EventOut", "FactOut", "TraitOut", "PlanOut", "DoubtOut",
    "AffectDelta", "ExtractResult", "parse_result", "anchor_raw",
    "clean_keywords", "render_prompt", "PROMPT_TEMPLATE",
]

# 空的 keywords、无效摘要等都会让下游检索失效，但不该让整条事件失败
_MAX_SUMMARY = 100
_MAX_KEYWORDS = 8
_MIN_KEYWORD_LEN = 2
_MAX_KEYWORD_LEN = 12


class _Out(BaseModel):
    """模型输出的基类：忽略多余字段，不做严格类型检查。

    模型多给一个字段就 422 —— 那是拿自己的稳定性换洁癖。
    多给的字段直接丢掉，缺的用默认值，这是唯一务实的策略。
    """

    model_config = ConfigDict(extra="ignore")


def _clamp01(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    # 模型常把 0~1 写成 0~10（"情绪很强烈，8 分"）。1 以上按十分制换算。
    if f > 1.0:
        f = f / 10.0 if f <= 10.0 else 1.0
    return max(0.0, min(1.0, f))


class EntityRef(_Out):
    name: str
    type: Literal["person", "place", "thing", "org"] = "person"
    aliases: list[str] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("aliases", mode="before")
    @classmethod
    def _aliases(cls, v: Any) -> list[str]:
        if not v:
            return []
        if isinstance(v, str):
            v = [v]
        return [str(x).strip() for x in v if str(x).strip()]


class EventOut(_Out):
    raw: str
    summary: str = ""
    keywords: list[str] = Field(default_factory=list)
    entities: list[EntityRef] = Field(default_factory=list)
    event_time_hint: str | None = None
    arousal: float = 0.0
    # 模型自己打的重要性（1~5）。插件只负责摘要/关键词/落库，
    # "这件事重不重要"交给模型判断——它比任何启发式都准，且不额外花钱。
    importance: int = 3
    # 关键值：数字/日期/金额/比例/专名，逐字照抄。单独存一列用于检索与渲染，
    # 不塞进摘要（摘要 40~60 字，塞满了就成流水账）。
    values: list[str] = Field(default_factory=list)
    source: Literal["user", "ai", "third"] = "user"
    # 摘要被截断的标记。由 encode 的修复回路写入，模型不该给这个字段——
    # 但它必须能穿过 parse，所以声明在这里而不是藏在别处。
    summary_truncated: int = 0
    # 修了两轮还不合格。和 truncated 分开：截断是"我们砍了"，flag 是"模型没写好"。
    summary_flagged: int = 0

    @field_validator("raw", mode="before")
    @classmethod
    def _raw(cls, v: Any) -> str:
        return str(v or "")

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v: Any) -> str:
        return str(v or "").strip()[:_MAX_SUMMARY]

    @field_validator("keywords", mode="before")
    @classmethod
    def _kw(cls, v: Any) -> list[str]:
        return clean_keywords(v)

    @field_validator("arousal", mode="before")
    @classmethod
    def _arousal(cls, v: Any) -> float:
        return _clamp01(v)


class FactOut(_Out):
    key: str
    value: str
    entity: str = "用户"
    confidence: float = 0.6
    # 哪一轮说了这件事。没有证据的事实照样会写（facts 的价值在值本身），
    # 但 source_event 留空——宁可"查不到出处"，也不要指一条无关的原文。
    evidence: list[int] = Field(default_factory=list)

    @field_validator("evidence", mode="before")
    @classmethod
    def _ev(cls, v: Any) -> list[int]:
        if v is None:
            return []
        if isinstance(v, (int, str)):
            v = [v]
        out = []
        for x in v:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out

    @field_validator("key", "value", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("confidence", mode="before")
    @classmethod
    def _c(cls, v: Any) -> float:
        return _clamp01(v, 0.6)


# 槽位别名。开放槽位等于没有槽位：模型这次写 employer、下次写 job，
# 两条"在哪工作"就会同时 active，矛盾检测形同虚设。
# 未知槽位**保留原文**而不是丢弃——丢了它会退化成"不占槽"，
# 悄悄失去冲突检测能力，比多一个长尾槽位更糟。
_SLOT_ALIASES = {
    "job": "employer", "work": "employer", "company": "employer",
    "occupation": "employer", "workplace": "employer", "career": "employer",
    "location": "city", "live": "city", "residence": "city", "home": "city",
    "address": "city", "living": "city",
    "pets": "pet", "animal": "pet",
    "food": "diet", "eat": "diet", "taste": "diet",
    "character": "personality", "trait": "personality", "temperament": "personality",
    "speech": "style", "habit": "style", "language": "style",
    "body": "health", "illness": "health", "medical": "health",
    "hobbies": "hobby", "interest": "hobby",
    "relatives": "family", "relationship": "family",
}


class TraitOut(_Out):
    entity: str
    trait: str
    status: Literal["observed", "inferred"] = "observed"
    evidence: list[int] = Field(default_factory=list)
    # 槽位：这条特征在回答哪个问题（employer / city / pet / diet …）。
    # 同一实体同一槽位只留一条 active，"在字节" 才能被 "在创业公司" 顶掉。
    # 给不出槽位就留空——空槽位不参与冲突，不会被误伤。
    slot: str = ""

    @field_validator("slot", mode="before")
    @classmethod
    def _slot(cls, v: Any) -> str:
        s = re.sub(r"[^a-z0-9_]", "", str(v or "").strip().lower())
        return _SLOT_ALIASES.get(s, s)[:20]

    @field_validator("evidence", mode="before")
    @classmethod
    def _ev(cls, v: Any) -> list[int]:
        if not v:
            return []
        if isinstance(v, (int, str)):
            v = [v]
        out = []
        for x in v:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if i >= 0:
                out.append(i)
        return out


class PlanOut(_Out):
    content: str
    due_hint: str | None = None
    recur: Literal["daily", "weekly"] | None = None

    @field_validator("recur", mode="before")
    @classmethod
    def _recur(cls, v: Any) -> str | None:
        if v in ("daily", "weekly"):
            return v
        return None


class DoubtOut(_Out):
    question: str
    confidence: float = 0.4
    impact: float = 0.5
    cost: float = 0.3

    @field_validator("confidence", "impact", "cost", mode="before")
    @classmethod
    def _c(cls, v: Any) -> float:
        return _clamp01(v, 0.5)


class AffectDelta(_Out):
    joy: float = 0
    sadness: float = 0
    anger: float = 0
    fear: float = 0
    disgust: float = 0
    surprise: float = 0

    @field_validator("joy", "sadness", "anger", "fear", "disgust", "surprise",
                     mode="before")
    @classmethod
    def _d(cls, v: Any) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return max(-20.0, min(20.0, f))

    def is_zero(self) -> bool:
        return all(abs(getattr(self, k)) < 0.5 for k in
                   ("joy", "sadness", "anger", "fear", "disgust", "surprise"))

    def as_dict(self) -> dict[str, float]:
        return {k: getattr(self, k) for k in
                ("joy", "sadness", "anger", "fear", "disgust", "surprise")}


class ExtractResult(_Out):
    events: list[EventOut] = Field(default_factory=list)
    facts: list[FactOut] = Field(default_factory=list)
    traits: list[TraitOut] = Field(default_factory=list)
    plans: list[PlanOut] = Field(default_factory=list)
    doubts: list[DoubtOut] = Field(default_factory=list)
    affect_delta: AffectDelta = Field(default_factory=AffectDelta)

    # M23.2：用户对 AI 上一轮**自主动作**的反馈。空串 = 没表态。
    # 只收"针对 AI 做过的事"的评价——"今天真热"不是反馈，
    # "你昨天改我配置这事很烦"才是。判据写在提示词里，模型来判。
    neg_feedback: str = ""
    pos_feedback: str = ""

    @field_validator("neg_feedback", "pos_feedback", mode="before")
    @classmethod
    def _fb(cls, v: Any) -> str:
        """超长截断，不报错。反馈只是"信号"不是记忆正文，截短不影响结算，
        留着长文本反而会让后续相关度计算被无关内容稀释。"""
        if v is None:
            return ""
        return str(v).strip()[:120]

    @field_validator("events", "facts", "traits", "plans", "doubts", mode="before")
    @classmethod
    def _list(cls, v: Any) -> list:
        """模型很喜欢给 null。null 和 [] 在这里是同一件事。"""
        return v if isinstance(v, list) else []

    @field_validator("affect_delta", mode="before")
    @classmethod
    def _aff(cls, v: Any) -> Any:
        return v if isinstance(v, dict) else {}

    def is_empty(self) -> bool:
        # M23.2 起，只有一句反馈也算"有内容"：它不改记忆，但要结算账簿。
        return not (self.events or self.facts or self.traits or self.plans
                    or self.doubts or self.neg_feedback or self.pos_feedback)

    def counts(self) -> dict[str, int]:
        return {
            "n_events": len(self.events), "n_facts": len(self.facts),
            "n_traits": len(self.traits), "n_plans": len(self.plans),
            "n_doubts": len(self.doubts),
        }


# ---------------------------------------------------------------- 关键词清洗
# 这些词出现在 keywords 里等于噪声：检索时会命中一切，等于什么都没命中
_EMPTY_WORDS = {
    "事情", "问题", "那个", "这个", "一些", "什么", "东西", "情况", "方面",
    "时候", "地方", "感觉", "觉得", "可能", "应该", "然后", "现在", "今天",
    "用户", "对话", "内容", "信息", "相关", "进行", "一个", "一下", "有点",
}


def clean_keywords(value: Any) -> list[str]:
    """解析期的关键词清洗：只做机械归一化。

    策略清洗（停用词/空词/上限 8/实体名保护）在 persist 里做——
    那时才知道这一段对话里有哪些实体名，才能保住"猫"这种单字实体。
    """
    return normalize_keywords(value)


# ---------------------------------------------------------------- 解析
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class ParseError(ValueError):
    """模型输出不是可用的 JSON。带上原始片段，方便日志里直接看。"""

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw
        self.head = (raw or "")[:200]


def _extract_json_text(text: str) -> str:
    """从任意包裹里挖出 JSON 主体。

    模型的实际输出长这样（三种都见过）：
        1. {"events": [...]}
        2. ```json\\n{"events": [...]}\\n```
        3. 好的，以下是抽取结果：\\n{"events": [...]}\\n希望对你有帮助。
    """
    t = (text or "").strip()
    if not t:
        raise ParseError("空输出", t)

    m = _FENCE.search(t)
    if m:
        t = m.group(1).strip()

    if t.startswith("{") and t.endswith("}"):
        return t

    # 找最外层的 { ... }：用括号计数，不靠 rfind，避免尾部还有别的对象
    start = t.find("{")
    if start < 0:
        raise ParseError("输出里没有 JSON 对象", t)
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return t[start:i + 1]
    raise ParseError("JSON 对象没有闭合（多半是被 max_tokens 截断）", t)


def parse_result(text: str) -> ExtractResult:
    """模型输出 → ExtractResult。失败抛 ParseError（调用方据此重试）。"""
    body = _extract_json_text(text)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ParseError(f"JSON 解析失败: {exc}", text) from exc
    if not isinstance(data, dict):
        raise ParseError(f"顶层不是对象，是 {type(data).__name__}", text)
    try:
        return ExtractResult.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - pydantic 的报错类型太杂
        raise ParseError(f"schema 校验失败: {exc}", text) from exc


# ---------------------------------------------------------------- raw 锚定
def _norm_map(s: str) -> tuple[str, list[int]]:
    """归一化并记录"每个归一化字符来自原文第几个字符"。

    NFKC 把全角转半角（"８点"→"8点"），空白全部丢弃。
    丢弃空白是有意的：模型经常把"我 昨天 去了"重排成"我昨天去了"，
    这在语义上没变，在字节上变了。我们要接受这种，但要能映射回原文。
    """
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(s):
        for c in unicodedata.normalize("NFKC", ch):
            if c.isspace():
                continue
            chars.append(c)
            idx.append(i)
    return "".join(chars), idx


def anchor_raw(raw: str, source: str) -> str | None:
    """把模型给的 raw 对回原文，返回**原文中的片段**；对不上返回 None。

    只做两级，不做模糊匹配（相似度 0.9 也不做）：
      1. 原样是子串 → 直接用
      2. 归一化后是子串 → 映射回原文切片

    为什么不做相似度：一旦允许"差不多就算"，模型润色过的句子就会入库，
    而"原文是唯一真相源"这条原则就废了——你无法再区分哪句是用户说的、
    哪句是模型替用户说的。**能锚定才入库，锚不上就丢弃，丢的是内容不是真相。**
    """
    raw = (raw or "").strip()
    if not raw or not source:
        return None

    if raw in source:
        return raw

    n_raw, _ = _norm_map(raw)
    if len(n_raw) < 2:
        return None
    n_src, pos = _norm_map(source)
    i = n_src.find(n_raw)
    if i < 0:
        return None
    return source[pos[i]: pos[i + len(n_raw) - 1] + 1]


# ---------------------------------------------------------------- prompt
PROMPT_TEMPLATE = """你是记忆编码器，不是对话参与者。
（模板文件：hippo/data/prompts/extract.md，这里是加载失败时的兜底）

已知人物：<<KNOWN_ENTITIES>>
当前时间：<<NOW_HINT>>

对话：
<<CONVERSATION>>

只输出一个 json 对象，字段：events / facts / traits / plans / doubts /
affect_delta / neg_feedback / pos_feedback。
raw 必须逐字复制用户原话。没有内容就给空数组，反馈留空字符串。
"""

_TEMPLATE_PATH_NAME = "prompts/extract.md"


def load_template() -> str:
    from ..config import DATA_DIR

    p = DATA_DIR / _TEMPLATE_PATH_NAME
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return PROMPT_TEMPLATE


def render_prompt(
    conversation: str,
    *,
    known_entities: str = "（暂无）",
    now_hint: str = "",
    template: str | None = None,
) -> str:
    """渲染 prompt。

    **用 replace 不用 str.format**：对话内容里出现一个 `{` 就会让 format 崩，
    而用户完全可能说"我写了个 {x} 的模板"。这种崩溃发生在生产环境，
    表现是"抽取随机失败"，最难查。
    """
    tpl = load_template() if template is None else template
    if "<<FACT_KEYS>>" in tpl:
        # 词表来自 data/fact_keys.yaml，不在 prompt 里写死：
        # 扩词表是改数据，不是改 prompt，两条路分开才不会互相打架。
        from .facts import load_keys

        tpl = tpl.replace("<<FACT_KEYS>>", load_keys().prompt_block())
    return (tpl
            .replace("<<KNOWN_ENTITIES>>", known_entities or "（暂无）")
            .replace("<<NOW_HINT>>", now_hint or "（未知）")
            .replace("<<CONVERSATION>>", conversation or "（空）"))
