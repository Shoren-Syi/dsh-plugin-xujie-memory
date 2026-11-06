"""话题切换检测（M12）。

这一层决定"这一轮要不要检索"。它是全场唯一的**成本闸门**：
每轮都检索，20 轮对话要多花 20 次 L1+L2 的 CPU 和 600 token 的注入；
不检索，用户换个话题问"那老王呢"时 AI 就会答非所问。

## 为什么不调模型判断"换没换话题"

一句话 3 个字，调模型的固定开销（网络 + 首 token）就有 300ms。
用户等这 300ms 换来的准确率提升，不如把 300ms 花在回复本身。
规则的准确率上限在 M4 已经量过（留出集 R 卡在 0.72），接受它，
用"漏触发代价 < 误触发代价"来选阈值——这是 M12.3 标定要做的事。

## 关键词集为什么不用 n-gram

n-gram 的 Jaccard 在中文上会**系统性偏低**：

    "老王跳槽了"      片段：老王 王跳 跳槽 槽了 老王跳 王跳槽 老王跳槽
    "老王新公司怎么样" 片段：老王 王新 新公 公司 司怎 怎么 么样 …

交集 1~2 个，并集 15 个，J ≈ 0.1。手册预期 0.5。**同一件事被判成换话题。**

真正的问题不是分词，是"两边用什么单位做交集"。n-gram 的碎片只在
"同一条字符串"里才有意义，跨句子比较时它们是噪声。

所以改用**库里的关键词表当词典**：把这句话切成 n-gram，只保留在
`kw_index` 里真的出现过的那些。等价于"用这个系统自己的语言做分词"，
而且零依赖、零模型。

代价：库里没有的新词（"量子纠缠"）永远进不了关键词集。但这不是缺陷——
新词恰恰是**新话题**的信号，判成"切换"是对的。

## 为什么"上一轮的关键词"存库而不是从对话里现算

手册说的是"上一轮 pack 的实际内容"。这和"上一句用户消息"是两回事：

    pack 的记忆块 = 8 条事件的 keywords 并集（可能 30 个词）
    上一句用户消息 = 5 个词

用 5 个词做分母，J 偏高，该切换的不切换。用 30 个词做分母，
J 偏低，没切换的判成交互。**分母是这套判据的主要参数**，
所以必须存 pack 的真实输出，不能回头猜。
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from ..log import get_logger
from .lexicon import GENERIC, MAX_QUERY_TOKENS, STOPWORDS, query_tokens
from .retrieve import extract_entities_in_query

log = get_logger("topic")

__all__ = [
    "RECALL_MARKERS", "FACT_MARKERS", "Trigger", "user_keywords", "last_keywords",
    "save_keywords", "jaccard", "relate", "new_entity_hint", "should_retrieve",
    "STATE_KEY", "TURN_KEY", "LAST_KEY", "MIN_INTERVAL_TURNS",
]

# 会话状态里的键。M13 写完 pack 后回填，M12 每轮开头读。
STATE_KEY = "last_pack_keywords"
ENT_KEY = "last_pack_entities"
TURN_KEY = "turn_no"              # 本会话进行到第几轮
LAST_KEY = "last_retrieve_turn"   # 上次真的检索是第几轮

# 两次检索之间的最小间隔（轮）。
#
# 为什么需要它：词面判据在真实对话上会把 80% 的轮次判成"换话题"（相邻两句
# 几乎不共享关键词，用户全用代词）。80% 意味着这套闸门形同虚设——每轮都检索，
# 那不如不做判断。
#
# 但"刚检索过 2 轮"时，上一轮的 600 token 记忆块还在上下文里，
# 用户问的是同一件事也答得上来。所以节流不是偷懒，是**利用上下文窗口**。
#
# 3 轮是"启用时"的建议值：一轮对话通常 3~6 句，3 轮差不多是"刚才说的还热乎"
# 的边界。但默认是 **0（关闭）**——实测它挡掉的大部分是真换了话题，
# 详见 config.RetrieveCfg.min_interval_turns 的注释和 R-12.3。
MIN_INTERVAL_TURNS = 0

# 回忆词：命中就**强制深检索**（连 L3）。
#
# 和 M11 的 detail_markers 是两套，不要合并：
#   回忆词  = "我要想起某件事"      → 触发检索，原文可选
#   细节词  = "我要那件事的原话"    → 触发下钻
# "你还记得我上次说的那个事吗" 是回忆但不是要原话；"当时怎么说的" 是细节。
RECALL_MARKERS = (
    "还记得", "记得", "上次", "上回", "那次", "你说过", "我跟你说过",
    "之前说", "以前说", "当时说", "当年", "曾经", "提过", "聊过",
)

# 具体事实问句（M26 评测暴露）。这些词本身不指向"过去"，但问的是
# **一个存在库里的具体值**："我约的复查是哪天""我住哪个区"。
#
# 不加这一组的话，只要上一轮聊的是同一件事（jaccard=1.0），
# should_retrieve 就判 continue、不检索，转而复用上一轮的 D 段。
# 而上一轮的 D 段是按**上一句**检索出来的，未必含这个值。
# 实测"我约的胃镜复查是什么时候"：l2 明明排第一（0.603），
# 但因为"复查"两个字上一轮刚聊过，pack 里一条记忆都没有。
FACT_MARKERS = (
    "什么时候", "哪天", "哪一天", "几点", "几号", "多少", "几个",
    "哪个", "哪家", "哪位", "叫什么", "是谁", "在哪",
)

# 称谓 + 名字。"我朋友老张" / "我老板叫李总"。
_TITLES = (
    "朋友", "同事", "老板", "领导", "室友", "房东", "医生", "老师",
    "同学", "表哥", "表姐", "堂哥", "堂妹", "对象", "男朋友", "女朋友",
    "老婆", "老公", "孩子", "儿子", "女儿", "邻居", "客户", "学长", "学姐",
)
_NAME = r"[\u4e00-\u9fa5]{2,3}"

# 称谓后面直接跟名字（"我朋友老张"）或"叫 X"（"我老板叫李总"）
_TITLE_RE = re.compile(r"(?:%s)(?:叫|名叫)?(%s)" % ("|".join(_TITLES), _NAME))
# "叫 X" 单独出现时的兜底（"有个人叫周深"）
_CALL_RE = re.compile(r"(?:叫|名叫|名字是)(%s)" % _NAME)

# 候选词不能以这些字开头——它们是动词/副词/助词，
# "我朋友说好" 会被 `_TITLE_RE` 切成 "说好"。
_BAD_HEAD = set("说讲是的了在要会能就都也还又很不没别让给对把被从向和与或但如果因为所以")

# 正则贪婪吃 3 个字，"我朋友老张下周结婚" 会切出 "老张下"。
# 尾部这些字几乎不会是名字的最后一个字，见到就往回退。
_BAD_TAIL = set("上下说来了去要会能的了在是有不没就都也还又很把被给让对和与或但如因所时后前里中外吗呢吧啊哦呀嘛"
                "今明昨早晚上午下周年月日号点分秒个位次条件事人")

# 库里已有的常见词也排除（"我朋友都这样" → "都这样"）
_KNOWN_NOISE = STOPWORDS | GENERIC | {
    "什么", "怎么", "这样", "那样", "这个", "那个", "一下", "有点", "真的",
    "知道", "觉得", "感觉", "应该", "可能", "现在", "时候", "地方", "事情",
}


@dataclass
class Trigger:
    """一次触发判定的结果。`reason` 会进 metrics，用来回答"到底谁在花钱"。"""

    fire: bool
    reason: str                        # recall / new_entity / cold_start / switch / continue
    jaccard: float
    deep: bool = False                 # 是否连 L3
    keywords: set[str] = field(default_factory=set)
    signals: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    suppressed: str = ""               # 非空 = 本来要检索，被节流挡下，值是原因


# ------------------------------------------------------------------ 关键词集
def user_keywords(
    store: Any,
    session: str,
    text: str,
    *,
    limit: int = MAX_QUERY_TOKENS,
) -> set[str]:
    """这句话的关键词集 = 库内词表命中 + 实体正名。

    见模块开头"为什么不用 n-gram"。一次 SQL（走 kw_index 主键 IN）
    + 一次实体匹配，实测 0.2~0.4ms。
    """
    plain = unicodedata.normalize("NFKC", text or "").lower().strip()
    if not plain:
        return set()

    words: set[str] = set()
    toks = query_tokens(plain, limit=limit)
    if toks:
        words = store.kw_lookup(toks)

    ids, names = extract_entities_in_query(text, session, store)
    if ids:
        # 用 canonical 而不是命中的别名：pack 的记忆块里写的是事件的 keywords，
        # 而那些词是抽取器按 canonical 生成的。用别名做交集会永远对不上。
        want = set(ids)
        for ent in store.list_entities(session):
            if ent.id in want:
                words.add(ent.canonical.lower())
        words |= {n.lower() for n in names}
    return words


def jaccard(a: set[str], b: set[str]) -> float:
    """|A∩B| / |A∪B|。空集返回 0（不是 1，也不是抛 ZeroDivision）。

    手册指定的就是它。**但实测它判不了真实对话**，见 `relate` 的注释：
    两份对话集相邻两句的 J 几乎恒为 0，五档阈值给出完全一样的触发率。
    这个函数保留有两个用途：报告里的对照指标，以及测试里的边界用例。
    """
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


# 指代标记。真实对话里第二句几乎不会重复上一句的实体，全用代词：
#
#   "老王不是让你帮他喂猫" → "对，他下周去深圳出差"
#                            ↑ 这句的关键词是 {深圳, 出差}，和老王没有词面交集
#
# 没有这一条，J 恒为 0，每轮都判"换话题"——等于没做判据。
_ANAPHORA = (
    "他", "她", "它", "他们", "她们", "它们", "对方", "这人", "那人",
    "这家伙", "那家伙", "那边", "这边", "这事", "那事", "这个事", "那个事",
    "刚才说", "你说的", "上面说",
)
# "他"会命中"其他"。"其他"不是代词，是限定词。
_NOT_ANAPHORA = ("其他", "其它")

# 疑问标记。用来区分"没信息的话"和"在问新东西的话"：
#   "嗯"       没信息 → 延续
#   "你养猫吗"  在问，但一个已知词都没有 → 问的是新东西
_QUESTION = ("？", "?", "吗", "呢", "什么", "哪", "几", "多少", "怎么",
             "为什么", "是不是", "有没有", "能不能", "要不要")

# 省略主语的追问。"什么品种""去几天""什么时候交"——中文里省略主语
# 就是在说"接着上面那句"。这类句子几乎必然是话题延续，判成切换纯属浪费。
#
# 反例："你养猫了吗" 有主语、有疑问，是完整的新问题。所以判据不是长短，
# 是"有没有主语"。
_SUBJECTS = ("你", "我", "他", "她", "它", "咱", "咱家")


def relate(
    cur: set[str], prev: set[str], text: str = "", *, store: Any = None,
) -> tuple[float, str]:
    """这句话和上一轮像不像。返回 (相似度 0~1, 依据)。

    用**覆盖率**而不是 Jaccard：|本轮词 ∩ 上轮词| / |本轮词|。

    为什么换：Jaccard 的分母含上一轮话题集的规模。话题集越大（8 条记忆
    的关键词并集，30 个词），J 越接近 0——**话题越丰富，越容易被判成换话题**，
    这个方向是反的。覆盖率只看"这一句有多少落在刚才的话题里"，
    不受上一轮装了几条记忆影响。

    三档依据（按可信度排）：
      anaphora  本轮没有内容词但有指代   → 1.0（"他下周去深圳"就是接着说的）
      coverage  按词面重合算
      empty     两边都空 → 0
    """
    if not cur and not prev:
        return 0.5, "empty"
    if not cur:
        t = text or ""
        probe = t
        for bad in _NOT_ANAPHORA:
            probe = probe.replace(bad, "")
        if any(a in probe for a in _ANAPHORA):
            return 1.0, "anaphora"
        if any(q in probe for q in _QUESTION):
            if len(probe) <= 5 and not probe.startswith(_SUBJECTS):
                return 0.8, "elliptical"
            # 有主语的完整问句，且一个已知词都没命中——问的是新内容。
            # "你养猫吗" 在聊搬家的上下文里就是换话题。
            return 0.0, "ask"
        # 没有内容词、没有指代、也不是提问：多半是"嗯""哈哈"。
        # 判成换话题会白发一次检索，判延续会漏。选延续——
        # 用户下一句通常会自己带出话题。
        return 0.5, "empty"
    if not prev:
        # 本轮有词、上轮一个都没命中——没有可复用的话题，不是"接着聊"。
        return 0.0, "idle"
    cov = len(cur & prev) / len(cur)
    if cov < 1.0 and store is not None and store.co_occur(cur, prev):
        # 没有共同词，但两组词在同一条事件里出现过 → 同一件事的两个侧面。
        # "我搬家了" → "房租涨了多少"，这条记忆的关键词是"搬家 朝阳 海淀 房租"。
        # 传 store 才跑这一步：它是唯一需要查库的分支，纯集合测试用不上。
        return 0.9, "cooccur"
    return cov, "coverage"


def last_keywords(store: Any, session: str) -> set[str]:
    raw = store.get_state(session, STATE_KEY, "")
    return {w for w in raw.split() if w}


def save_keywords(
    store: Any, session: str, words: set[str] | list[str],
    entities: set[int] | list[int] = (),
) -> None:
    """存上一轮 pack 的关键词与实体。**M13 装配完必须调**，否则下一轮
    永远判 cold_start（每轮都全量检索，缓存全废）。

    entities 一起存是为了"同实体必延续"这条判据——它比词面可靠得多。
    """
    store.set_state(session, STATE_KEY, " ".join(sorted(words)))
    store.set_state(session, ENT_KEY, " ".join(str(e) for e in sorted(entities)))


# ------------------------------------------------------------------ 新实体
def new_entity_hint(store: Any, session: str, text: str) -> list[str]:
    """这句话里像新人名的词。**是启发式，不是 NER。**

    只认两种句法位置：称谓后面、`叫` 后面。别的位置一律不猜——
    猜错代价是"多检索一次"（便宜），但猜多了会把每句话都判成新实体，
    整套闸门就失效了。
    """
    src = unicodedata.normalize("NFKC", text or "")
    if not src:
        return []

    known = set()
    for ent in store.list_entities(session):
        known.add(ent.canonical.lower())
        known.update(a.lower() for a in ent.aliases)

    cands: list[str] = []
    for rx in (_TITLE_RE, _CALL_RE):
        for m in rx.finditer(src):
            w = _trim_name(m.group(1))
            if (w and len(w) >= 2 and w[0] not in _BAD_HEAD
                    and w not in _KNOWN_NOISE and w.lower() not in known):
                cands.append(w)
    # 去重但保序：同一句话里"我朋友老张"和"老张说"不该算两个新人
    return list(dict.fromkeys(cands))


def _trim_name(w: str) -> str:
    """把正则贪婪多吃的尾字退掉。"老张下" → "老张"。"""
    while len(w) > 2 and w[-1] in _BAD_TAIL:
        w = w[:-1]
    return w if len(w) >= 2 and w[-1] not in _BAD_TAIL else ""


# ------------------------------------------------------------------ 判定
def should_retrieve(
    store: Any,
    session: str,
    text: str,
    *,
    threshold: float | None = None,
    interval: int | None = None,
    turn: int | None = None,
) -> Trigger:
    """要不要检索、为什么。三条件任一满足即触发（手册 M12.2）。

    判定顺序是**成本顺序**，不是概率顺序：
      recall     命中回忆词 → 一定要，而且连原文
      new_entity 出现新名字 → 一定要（不认识的人，不查就是装不认识）
      cold_start 没有上一轮 → 一定要（第一次开口，什么都得查）
      switch     J 低于阈值 → 可能换话题了
    前三个的判断代价都是微秒级，J 要查库，所以放最后。
    """
    t0 = time.perf_counter()
    cfg = getattr(store, "retrieve_cfg", None)
    th = float(threshold if threshold is not None
               else getattr(cfg, "trigger_jaccard", 0.25))
    gap = int(interval if interval is not None
              else getattr(cfg, "min_interval_turns", MIN_INTERVAL_TURNS))

    turn = _turn_no(store, session) if turn is None else int(turn)

    words = user_keywords(store, session, text)
    prev = last_keywords(store, session)
    j, basis = relate(words, prev, text or "", store=store)

    # ---- 强信号：不看间隔，必须检索
    hits = [m for m in RECALL_MARKERS if m in (text or "")]
    if hits:
        return _fire(store, session, turn, j, "recall", words, hits, True, t0)

    new = new_entity_hint(store, session, text)
    if new:
        return _fire(store, session, turn, j, "new_entity", words, new, False, t0)

    # 上一轮和这一轮提到同一个人 → 铁定在聊同一件事，不看词面。
    # 这是最可靠的信号：词会换（"搬家"→"通勤"），人不会。
    cur_ids = _entity_ids(store, session, text or "")
    prev_ids = {int(x) for x in store.get_state(session, ENT_KEY, "").split() if x}
    if cur_ids & prev_ids:
        return Trigger(False, "continue", 1.0, keywords=words,
                       signals=["同实体"], elapsed_ms=_ms(t0))

    # 具体事实问句：问的是库里的一个值，不是"接着刚才那句说"。
    # 放在"同实体"之后是必须的——"老王什么时候回来"问的是未来，
    # 只是因为句子里有"什么时候"就去翻库，等于每句问句都检索一次。
    # 而"我约的复查是哪天"里没有同实体信号（实体是"复查"，
    # 上一轮记的是"搬家"），会落到这里。
    facts = [m for m in FACT_MARKERS if m in (text or "")]
    if facts:
        return _fire(store, session, turn, j, "fact_query", words, facts, False, t0)

    if store.get_state(session, STATE_KEY, None) is None:
        # 存都没存过 = 这个会话第一次开口。和"存过但是空的"是两件事，
        # 见 relate 里 "idle" 那段。
        return _fire(store, session, turn, j, "cold_start", words, [basis], False, t0)

    # ---- 弱信号：话题可能换了，但要过节流
    if j >= th:
        return Trigger(False, "continue", j, keywords=words, signals=[basis],
                       elapsed_ms=_ms(t0))

    last = int(store.get_state(session, LAST_KEY, "0") or 0)
    if gap > 0 and last and turn - last <= gap:
        t = Trigger(True, "switch", j, keywords=words, signals=[basis],
                    elapsed_ms=_ms(t0))
        t.suppressed = f"距上次检索 {turn - last} 轮 < {gap}，上下文还在"
        return Trigger(False, "throttled", j, keywords=words, signals=[basis],
                       elapsed_ms=_ms(t0), suppressed=t.suppressed)

    return _fire(store, session, turn, j, "switch", words, [basis], False, t0)


def _entity_ids(store: Any, session: str, text: str) -> set[int]:
    if not text:
        return set()
    ids, _ = extract_entities_in_query(text, session, store)
    return set(ids)


def _turn_no(store: Any, session: str) -> int:
    """本会话进行到第几轮。自增并落库。

    **这个函数有副作用**，这是刻意的：轮次是对话的属性，不是调用的参数。
    让调用方传 turn 反而更容易错（重试一次就多算一轮）。
    """
    turn = int(store.get_state(session, TURN_KEY, "0") or 0) + 1
    store.set_state(session, TURN_KEY, str(turn))
    return turn


def _fire(store: Any, session: str, turn: int, j: float, reason: str,
          words: set[str], signals: list[str], deep: bool, t0: float) -> Trigger:
    store.set_state(session, LAST_KEY, str(turn))
    return Trigger(True, reason, j, deep=deep, keywords=words,
                   signals=signals, elapsed_ms=_ms(t0))


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def explain(t: Trigger) -> str:
    """一行解释这次判定。CLI `hippo why-retrieve` 用。"""
    head = f"{'检索' if t.fire else '跳过'}（{t.reason}） J={t.jaccard:.2f}"
    if t.deep:
        head += " 深检索"
    if t.signals:
        head += f"  命中：{'/'.join(t.signals[:4])}"
    head += f"  {t.elapsed_ms:.2f}ms"
    if t.keywords:
        head += f"\n关键词：{' '.join(sorted(t.keywords)[:12])}"
    return head
