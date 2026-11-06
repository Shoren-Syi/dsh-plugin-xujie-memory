"""奖励账簿（M23）—— AI 给自己的自主动作记账。

## 这个模块只做四件事

    1. 登记   `register()`          打算做自主动作前，写下"我觉得这事好不好"
    2. 结算   `settle()`            用户后来表态了，翻出是哪件事，算 RPE
    3. 过期   `expire()`            三天没等到反馈 → 作废，不猜
    4. 懊悔   `make_regret()`       RPE ≤ -0.5 的事故写成一条能想起来的记忆

## 三条纪律

**一、数值绝不进 prompt。**
predicted / rpe / 权重都是内部信号。日记本上的数字给 AI 看，它会开始
"解释"这些数字（"我的 RPE 是 -0.8，说明我很失败"）。这不是人格，是账本。
`test_m23_1_ledger.py::test_numbers_never_reach_prompt` 守这条。

**二、一次反馈只结算一条。**
用户说了句"你昨天那个改动挺烦的"，可能有三条未结算动作。全结算是
批量误伤——另外两条根本没被提到。按"反馈文本与动作文本的相关度"选
最像的那一条，其余留着等下一条反馈。

**三、过期不猜。**
三天前的动作，今天用户随便一句抱怨就结算成 -1，等于凭空造一条教训。
宁可留空账。空账的代价是"少学一次"，假账的代价是"学错一辈子"。

## 为什么不用强化学习那一套
没有环境、没有成千上万次试验、没有稳定的 reward。这里能拿到的信号
是"用户骂了一句"，稀疏、有噪声、且延迟。所以只做最保守的事：
把"预期"和"结果"的差记下来，差值够大才写成一条自然语言记忆，
让下次的自己读到。**不做梯度，不做策略更新。**

## 动作分类为什么只有四类
手册 M23.1 定义的：好奇探索、主动改文件、主动提议、主动发消息。
分类的用途只有一个：懊悔记忆要能被"同类动作"召回。分得再细，
召回时也没有足够样本支撑，反而每条 regret 都变成孤例。
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

from ..log import get_logger
from .doubt import answer_overlap
from .models import LedgerEntry

log = get_logger(__name__)

__all__ = [
    "KIND_EXPLORE", "KIND_EDIT", "KIND_PROPOSE", "KIND_MESSAGE", "ACTION_KINDS",
    "KIND_WORDS", "REGRET_RPE", "SETTLE_DAYS",
    "detect_kind", "looks_requested", "clamp_predicted",
    "register", "Settlement", "settle", "expire", "recall", "make_regret",
    "regret_summary", "format_stats",
]

KIND_EXPLORE = "explore"
KIND_EDIT = "edit_file"
KIND_PROPOSE = "propose"
KIND_MESSAGE = "message"

ACTION_KINDS = (KIND_EXPLORE, KIND_EDIT, KIND_PROPOSE, KIND_MESSAGE)

# 每类动作的"标配词"。懊悔记忆的 keywords 里必含它（手册 M23.3 第 3 条），
# 定向召回也靠它匹配——events 表没有 action_kind 列，而为了一个字段
# 给 events 加列不值当：关键词本来就是给人看的，这三个词恰好也是
# 三个月后读这条记忆时最先扫到的词。
KIND_WORDS: dict[str, tuple[str, ...]] = {
    KIND_EDIT: ("擅自", "改文件", "未询问"),
    KIND_MESSAGE: ("擅自", "发消息", "未确认"),
    KIND_PROPOSE: ("擅自", "提议", "没问清"),
    KIND_EXPLORE: ("擅自", "翻看", "没打招呼"),
}

# 结算窗口。手册写"最近 3 天内未结算"。
SETTLE_DAYS = 3
SETTLE_SECONDS = SETTLE_DAYS * 86400

# 低于这条线才写懊悔记忆（手册 M23.3）。
REGRET_RPE = -0.5

# ------------------------------------------------------------------ 分类
# 顺序 = 优先级。"我改完文件顺手发了条消息"应该算 edit_file：
# 改文件是主行为，消息是附带的。搞反了会让懊悔挂到错的类型上，
# 下次要改文件时召回不到。
_KIND_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (KIND_EDIT, ("改文件", "改配置", "编辑", "写入", "覆盖", "删了", "动了",
                 "改了", "替换", "重写", "import", "patch", "commit")),
    (KIND_MESSAGE, ("发消息", "发了条", "通知他", "提醒他", "主动说", "私信",
                    "发邮件", "告诉他", "问他")),
    (KIND_PROPOSE, ("建议", "提议", "推荐", "要不要", "不如", "方案")),
    (KIND_EXPLORE, ("翻看", "搜了", "查了", "看了一眼", "浏览", "读了",
                    "探索", "顺手看", "扫了")),
)

# 关键词匹配不到时的兜底。"改一下配置"不含"改配置"这个子串——
# 中文的动作短语中间随时会插进量词和语气词，纯子串匹配会在最
# 该命中地方漏掉（"我要改一下配置"是标准的改文件意图）。
_KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (KIND_EDIT, re.compile(r"(改|动|删|覆盖|写).{0,8}(配置|文件|代码|设置|脚本)")),
    (KIND_MESSAGE, re.compile(r"(发|通知|提醒|私信).{0,3}(消息|邮件|他|她)")),
    (KIND_EXPLORE, re.compile(r"(翻|看|查|读|搜).{0,2}(一下|一眼|了)")),
    (KIND_PROPOSE, re.compile(r"(建议|提议|要不|不如).{0,4}")),
)

# 用户明确要求做事的信号。判定成"被动"的代价是多一条空账（无害），
# 判定成"主动"的代价是给一次正常服务记上奖惩（有害）——所以宁可宽松。
_REQUEST_WORDS = (
    "帮我", "请你", "请帮", "麻烦", "给我", "替我", "能不能", "可不可以",
    "你去", "你去把", "把文件", "改一下", "改改", "写一个", "写个",
    "查一下", "看一下", "发一下", "发个", "整理一下", "生成", "翻译",
    "总结一下", "跑一下", "运行", "执行", "按照我说的", "照做",
)


def detect_kind(action: str) -> str:
    """动作文本 → 四类之一；判不出来返回空串（不是自主动作）。"""
    t = (action or "").strip().lower()
    if not t:
        return ""
    for kind, words in _KIND_RULES:
        if any(w in t for w in words):
            return kind
    for kind, pat in _KIND_PATTERNS:
        if pat.search(t):
            return kind
    return ""


def looks_requested(user_text: str) -> bool:
    """用户这句话是不是在**要求**做某事。

    "帮我改一下配置" → True（被动响应，不登记）
    "最近有点烦"     → False（AI 主动接话，可登记）

    只看显式的请求词。不做意图推断：推断错的代价不对称——把"我自己
    随口一说"判成"用户在要求"，会让 AI 之后不敢主动做事；反过来只是
    多记一条空账。所以门槛设得低（宁可多判被动）。
    """
    t = (user_text or "").strip()
    if not t:
        return False
    return any(w in t for w in _REQUEST_WORDS)


def clamp_predicted(value: object) -> float:
    """把模型给的预期收敛到 [-1, 1]。

    模型有两种常见写法：
      - 十分制："我觉得这事有 7 分把握" → 0.7
      - 直接越界：1.5 / -3 / 12
    十分制的判定用"绝对值 ≥2 且 ≤10"：1.5 更可能是"比 1 稍高"的越界，
    不会是"1.5 分"——十分制里没人打 1.5 分，那等于说"基本确定是灾难"
    却写在正数栏里。这个下界不是拍脑袋：`schema._clamp01` 用 >1 就换算，
    结果是 1.5 分变成 0.15，方向对但量级错得离谱。
    """
    try:
        v = float(value)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(v) or math.isinf(v):
        return 0.0
    if abs(v) > 1.0:
        v = v / 10.0 if 2.0 <= abs(v) <= 10.0 else math.copysign(1.0, v)
    return max(-1.0, min(1.0, v))


# ------------------------------------------------------------------ 登记
def register(store, session: str, action: str, predicted: object = 0.0, *,
             kind: str = "", user_text: str = "",
             now: int | None = None) -> str | None:
    """登记一条自主动作的预期。返回 action_id；不该登记的返回 None。

    不该登记的三种情况：
      1. 用户明确要求做的（被动响应）—— 这不是 AI 的自主判断，
         给它记奖惩等于替用户的行为打分。
      2. 动作文本为空 —— 空账没有结算价值。
      3. 归不到四类动作里 —— "回复用户的问题"是本职，不是自主行为。
    """
    text = (action or "").strip()
    if not text:
        return None
    if user_text and looks_requested(user_text):
        return None
    k = (kind or "").strip()
    if k and k not in ACTION_KINDS:
        log.warning("ledger.bad_kind", extra={"fields": {"kind": k}})
        k = ""
    k = k or detect_kind(text)
    if not k:
        return None
    return store.add_ledger(session=session, action=text,
                            predicted=clamp_predicted(predicted), kind=k,
                            ts=now)


# ------------------------------------------------------------------ 结算
@dataclass
class Settlement:
    """一次结算的结果。给调用方看，**不给模型看**。"""

    action_id: str
    action: str
    kind: str
    predicted: float
    actual: float
    rpe: float
    regret_id: int | None = None
    expired: int = 0          # 本次顺带作废了几条

    @property
    def is_regret(self) -> bool:
        return self.rpe <= REGRET_RPE


def expire(store, session: str, *, now: int | None = None,
           days: int = SETTLE_DAYS) -> int:
    return len(store.expire_ledger(session, within_seconds=days * 86400, now=now))


def _relevance(feedback: str, action: str) -> float:
    """反馈文本与动作文本的相关度。

    用 M18.3 的实义块覆盖率，取两个方向的最大值：
      - 反馈的块有多少出现在动作里（"别擅自改配置" vs "改了配置文件"）
      - 动作的块有多少出现在反馈里（反向）
    单方向会被长度差吃掉：反馈常常是长句，动作是短语，反过来也一样。
    """
    a = answer_overlap(feedback, action)
    b = answer_overlap(action, feedback)
    return max(a, b)


def _pick(cands: list[LedgerEntry], feedback: str) -> LedgerEntry:
    """多条未结算里选一条：相关度优先，同分取最近。

    相关度全为 0 时 max 自然退化成"取 ts 最大"，正好是手册的兜底规则。
    """
    return max(cands, key=lambda e: (_relevance(feedback, e.action), e.ts))


def settle(store, session: str, feedback: str, actual: float, *,
           now: int | None = None, lesson: str = "",
           days: int = SETTLE_DAYS) -> Settlement | None:
    """用户表态了 → 找到对应的动作 → 结算 → rpe 够低就写懊悔记忆。

    `actual` 由调用方给（+1 表扬 / -1 批评），这里不猜：负反馈也可能
    是"这次做得不错，但是……"，让抽取模型去判，比在这里数感叹号强。
    """
    now = int(now if now is not None else time.time())
    expired = expire(store, session, now=now, days=days)
    cands = store.open_ledger(session, within_seconds=days * 86400, now=now)
    if not cands:
        return None

    entry = _pick(cands, feedback or "")
    rpe = store.settle_ledger(entry.action_id, actual, now=now)
    out = Settlement(action_id=entry.action_id, action=entry.action,
                     kind=entry.action_kind, predicted=entry.predicted,
                     actual=actual, rpe=rpe, expired=expired)
    if rpe <= REGRET_RPE:
        out.regret_id = make_regret(store, session, entry, feedback,
                                    lesson=lesson, now=now)
    log.info("ledger.settled", extra={"fields": {
        "action_id": entry.action_id, "kind": entry.action_kind,
        "rpe": round(rpe, 2), "regret": out.regret_id, "expired": expired}})
    return out


# ------------------------------------------------------------------ 懊悔
_LEAD = re.compile(r"^[我咱]\s*")
_SENT = re.compile(r"[。！？!?；;\n]")
# "下次 / 以后 / 别再……" 之后到句末，就是用户要求的那句
_LESSON = re.compile(
    r"(?:下次|以后|下回|往后|别再|不要再|不许再|应该先|要先|记得先)"
    r"([^。！？!?；;\n]{0,24})")

_DEFAULT_LESSON = {
    KIND_EDIT: "先问再做",
    KIND_MESSAGE: "先确认再发",
    KIND_PROPOSE: "先问清楚再提议",
    KIND_EXPLORE: "先打招呼再翻",
}


def _clip(text: str, n: int) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else t[:n] + "…"


def _reaction(feedback: str) -> str:
    """用户的反应，写成一句第三人称的话。

    保留原话的片段而不是概括——三个月后读到"用户说「你怎么又擅自改」"
    比"用户表达了不满"有用得多：前者带着语气，后者只是一条标签。
    """
    t = _SENT.split((feedback or "").strip())[0].strip()
    if not t:
        return "用户明确表达了不满"
    return f"用户说「{_clip(t, 30)}」"


def _lesson(feedback: str, kind: str) -> str:
    m = _LESSON.search(feedback or "")
    if m:
        tail = m.group(1).strip(" ，,、")
        if len(tail) >= 2:
            return _clip(tail, 20)
    return _DEFAULT_LESSON.get(kind, "先问再做")


def regret_summary(action: str, feedback: str, kind: str,
                   lesson: str = "") -> str:
    """懊悔记忆的摘要模板（手册 M23.3）：

        我{动作}，{用户的反应}，要求下次{该怎么做}。

    "我"只在动作不以第一人称开头时补——动作文本常常已经是"我改了配置"，
    直接套模板会变成"我我改了配置"。
    """
    act = _LEAD.sub("", (action or "").strip()) or "做了件事"
    return f"我{act}，{_reaction(feedback)}，要求下次{lesson or _lesson(feedback, kind)}。"


def _kw_blocks(text: str, n: int) -> list[str]:
    """从一句话里劈出可当关键词的实义块（走 M18.3 的同一把刀）。

    为什么复用疑问句的切块器：它已经解决了"去掉自我前缀、劈掉虚词、
    代词踢除"这三件事，换个场景再写一套只会得到两套不一致的切法——
    同一句话在存疑池里切成 3 块、在账簿里切成 2 块，检索行为就开始玄学。
    """
    from .doubt import _question_blocks      # 同包内部件，避免复制一份切法

    out: list[str] = []
    for b in _question_blocks(text or ""):
        if 2 <= len(b) <= 6 and not b.startswith(("我", "你", "他", "她", "它")):
            if b not in out:
                out.append(b)
    return out[:n]


def _keywords(action: str, feedback: str, kind: str) -> list[str]:
    """keywords = 动作类型的标配词（必含）+ 动作/反馈里的实义块。

    标配词保证"同类动作定向召回"能命中；动作块说明"到底动了什么"；
    反馈块说明"用户当时介意的是什么"（"改配置"和"改配置没备份"
    是两种教训，只有后者能被"备份"搜到）。
    """
    out: list[str] = list(KIND_WORDS.get(kind, ("擅自",)))
    for w in _kw_blocks(action, 2) + _kw_blocks(feedback, 2):
        if w not in out:
            out.append(w)
    return out[:6]


def make_regret(store, session: str, entry: LedgerEntry, feedback: str, *,
                lesson: str = "", now: int | None = None) -> int:
    """把一次 RPE 够低的结算写成 kind=regret 的记忆。

    w0=0.85 是手册定的：懊悔必须比日常事件活得久（w0≥0.7 半衰期 180 天），
    否则三个月后同样的坑会再踩一次，而"我明明记过"这句话就成了谎。
    tier 给 2 而不是 1：hot 应该由权重衰减自己算出来，不该手写。
    写死 hot 的话，一条不相关的懊悔会赖在 hot 池里挤占别人的位置。
    """
    kind = entry.action_kind or detect_kind(entry.action)
    summary = regret_summary(entry.action, feedback, kind, lesson)
    stamp = int(now if now is not None else time.time())
    return store.add_event(
        session=session,
        raw=summary,                 # AI 自己的记忆，没有用户原话可锚
        summary=summary,
        keywords=_keywords(entry.action, feedback, kind),
        event_time=stamp,
        ingest_time=stamp,
        tier=2,
        w0=0.85,
        weight=0.85,
        source="ai",
        kind="regret",
        time_precision="minute",
    )


def recall(store, session: str, upcoming_action: str, *,
           limit: int = 1) -> list:
    """打算做某个动作之前，先把同类的懊悔翻出来（定向检索）。

    只在**真的要动手**时调。常规对话里调用它，等于让 AI 每轮都
    念一遍自己的黑历史——那不是长记性，那是创伤后应激。
    """
    k = detect_kind(upcoming_action)
    if not k:
        return []
    word = KIND_WORDS[k][1]
    return store.regrets(session, keyword=word, limit=limit)


def format_stats(stats: dict, regrets: list) -> str:
    """`hippo stats --ledger` 的人读格式。数字只在这里给人看。"""
    lines = [f"待结算 {stats['open']} 条 / 已结算 {stats['settled']} 条"]
    if stats["settled"]:
        lines.append(f"平均 RPE {stats['avg_rpe']:+.2f}（负 = 结果比预期差）")
    lines.append(f"懊悔记忆 {stats['regrets']} 条")
    if not regrets and stats["regrets"]:
        lines.append("  （多条会话合并计数，加 --session 看明细）")
    for e in regrets[:5]:
        lines.append(f"  · {e.summary}")
    if not regrets and not stats["regrets"]:
        lines.append("  （还没有。没闯过祸，或者闯了但没被说。）")
    return "\n".join(lines)
