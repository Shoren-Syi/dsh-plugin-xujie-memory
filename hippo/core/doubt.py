"""M18 存疑池：产生 → 限流 → 过期转记忆，全生命周期一个模块。

为什么全用规则不用模型：疑问的产生端在抽取器里（模型已经判过一次
"这值不值得记"），这里做的是入库前的质检和注入端的节流——每条消息、
每一轮 pack 都要走，一次模型调用都嫌贵（约束⑤）。

confidence 的语义（最容易读反的地方，注释钉死）：

    confidence = "我已经理解了多少"。**越低越该问。**

所以 should_ask 里 `confidence > 0.75 → 不问`：基本懂了，再问像抬杠；
`impact < 0.4 → 不问`：就算问了也不影响理解；`cost > 0.7 → 不问`：
这个问题问出口的社交代价太高（涉及隐私、需要对方想很久）。
三个数都不是精度测量，是档位——别拿它们做加权求和之类的"精细"运算，
六个 if 就是手册 §8 的原则逐条翻译成代码，多一行都是聪明过头。

三段职责：
- filter_doubts   入库前质检（M18.1）：鸡毛蒜皮滤掉、重复合并、超限淘汰
- should_ask/pick_doubt   注入端节流（M18.2）：一轮最多 1 条，问完不再纠结
- expire_to_memory / resolve_matching_doubts   生命周期收尾（M18.3）：
  问不出来的变成"我忘了"的体感，答案来了的体面关闭
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..log import get_logger
from .schema import DoubtOut

log = get_logger("doubt")

# ---------------------------------------------------------------- 手册阈值
# 集中放这里，config 里 DoubtsCfg 只放"部署可调"的（冷却轮数/上限）。
# 这三个是判据本体的定义，改了等于改语义，不开放配置。
MIN_IMPACT = 0.4           # impact 低于它：不影响理解，不记
MIN_QUESTION_LEN = 8       # 少于 8 字：太含糊，问了也没用
MERGE_SIMILARITY = 0.8     # trigram 相似度超过它：同一个疑问

# should_ask 的硬档位（手册 §8 六条件里的三个数）
CONFIDENT_ENOUGH = 0.75    # confidence 高于它：基本懂了，别问
TOO_COSTLY = 0.7           # cost 高于它：问出口的代价太大
DEFAULT_COOLDOWN = 8       # 轮：两次追问的最小间隔
DEFAULT_MAX_ASK = 2        # 次：一条疑问最多问几次

# 注入格式（M18.2.4）。固定模板，不玩花样：模型看到这句话只有一个
# 正确反应——要么顺着问，要么忽略。措辞一改，行为就不可预测。
ASK_TEMPLATE = "[存疑] 你不确定「{q}」。如果想问，问一次就好，问完不再纠结。"


# ================================================================ 相似度

def trigrams(text: str) -> set[str]:
    """字符级 trigram。中文没有空格分词，按字切是最便宜且够用的粒度。

    长度 <3 的串返回它自身（单字/双字也要能比，否则"老王"和
    "老王是谁"比较时前者直接得 0 分）。
    """
    s = (text or "").strip().lower()
    if not s:
        return set()
    if len(s) < 3:
        return {s}
    return {s[i:i + 3] for i in range(len(s) - 2)}


def trigram_similarity(a: str, b: str) -> float:
    """Jaccard：交集 / 并集。0=毫不相关，1=逐字相同。"""
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ================================================================ M18.1 过滤

@dataclass
class MergedInto:
    """一条候选被并进了库里已有的某条 doubt。"""
    doubt_id: int            # 已有条目的 id
    question: str            # 已有条目的原文
    impact: float            # 合并后两者取较高值（传给 bump_doubt_impact）


@dataclass
class DoubtFilterStats:
    """过滤账目。丢了几条、为什么丢，验收表（丢弃率 40%~60%）靠它算。"""
    total: int = 0
    kept: int = 0
    dropped_low_impact: int = 0
    dropped_vague: int = 0
    dropped_duplicate: int = 0   # 池内互相合并
    dropped_answered: int = 0    # 与已回答的疑问同题：答案已在 facts 里
    merged_into_existing: list[MergedInto] = field(default_factory=list)

    @property
    def dropped(self) -> int:
        return (self.dropped_low_impact + self.dropped_vague
                + self.dropped_duplicate + self.dropped_answered
                + len(self.merged_into_existing))

    @property
    def drop_rate(self) -> float:
        return self.dropped / self.total if self.total else 0.0


def filter_doubts(
    candidates: Sequence[DoubtOut],
    existing: Iterable[Any] = (),
    *,
    closed_recent: Iterable[Any] = (),
    min_impact: float = MIN_IMPACT,
    min_len: int = MIN_QUESTION_LEN,
    merge_sim: float = MERGE_SIMILARITY,
) -> tuple[list[DoubtOut], DoubtFilterStats]:
    """入库前质检。返回 (要新增的, 账目)。

    顺序有讲究：impact/长度先判（O(1)，一眼丢），相似度最后算
    （每条候选要和 existing + 已保留的逐个比）。丢一半的候选根本
    不需要走到相似度那步——顺序就是成本。

    `closed_recent`：最近被用户回答过的疑问。候选与它们相似时直接
    丢弃（不是合并）——答案已经在 facts 里，新开一条疑问等于装傻。
    """
    st = DoubtFilterStats(total=len(candidates))
    existing = list(existing)
    closed = list(closed_recent)
    kept: list[DoubtOut] = []
    kept_questions: list[str] = []

    for c in candidates:
        if not c.question or not c.question.strip():
            st.dropped_vague += 1          # 模型给了空问题，和含糊同罪
            continue
        if c.impact < min_impact:
            st.dropped_low_impact += 1
            continue
        if len(c.question.strip()) < min_len:
            st.dropped_vague += 1
            continue

        # 与已回答的比：同题再问 = 装傻，直接丢。
        answered = False
        for e in closed:
            if trigram_similarity(c.question, e.question) > merge_sim:
                st.dropped_answered += 1
                answered = True
                break
        if answered:
            continue

        # 与库里活跃的比：相似 → 合并，不新增。疑问再次被触发表明它
        # 更值得问，impact 取两者较高（只升不降，见 bump_doubt_impact）。
        hit = False
        for e in existing:
            if trigram_similarity(c.question, e.question) > merge_sim:
                st.merged_into_existing.append(MergedInto(
                    doubt_id=e.id, question=e.question,
                    impact=max(c.impact, e.impact)))
                hit = True
                break
        if hit:
            continue

        # 与本批已保留的比：一次抽取里模型经常就同一件事写出两种问法
        # （"用户住哪？""用户的居住地是？"），只留 impact 高的那条。
        dup = False
        for i, q in enumerate(kept_questions):
            if trigram_similarity(c.question, q) > merge_sim:
                st.dropped_duplicate += 1
                if c.impact > kept[i].impact:
                    kept[i] = c
                    kept_questions[i] = c.question
                dup = True
                break
        if dup:
            continue

        kept.append(c)
        kept_questions.append(c.question)

    st.kept = len(kept)
    return kept, st


# ================================================================ M18.2 限流

def should_ask(
    d: Any,
    turn: int,
    asked_this_turn: bool,
    *,
    now_ts: int | None = None,
    cooldown_turns: int = DEFAULT_COOLDOWN,
    max_ask_count: int = DEFAULT_MAX_ASK,
) -> bool:
    """这条疑问这一轮能不能问。手册 §8 六条件逐条照抄，顺序也不动。

    每个条件拦下来的理由（自上而下，从最硬到最软）：
    - asked_this_turn        一轮最多 1 条，这是 DoD 的硬约束
    - confidence > 0.75      基本懂了，问了像抬杠
    - impact < 0.4           问了也不影响理解
    - cost > 0.7             问出口的社交代价太高
    - expires < now          已过期：注入端不该再问（M18.3 会转记忆）
    - 距上次问 < 8 轮        刚问过还问 = 复读机
    - asked_count >= 2       问了两次还没答案，第三次是骚扰
    """
    if asked_this_turn:
        return False
    if d.confidence > CONFIDENT_ENOUGH:
        return False
    if d.impact < MIN_IMPACT:
        return False
    if d.cost > TOO_COSTLY:
        return False
    if now_ts is not None and d.expires < now_ts:
        return False
    if d.last_asked_turn is not None and \
            turn - d.last_asked_turn < cooldown_turns:
        return False
    if d.asked_count >= max_ask_count:
        return False
    return True


def pick_doubt(
    store: Any,
    session: str,
    turn: int,
    *,
    asked_this_turn: bool = False,
    now_ts: int | None = None,
    cfg: Any = None,
) -> Any | None:
    """这一轮要注入的至多一条疑问。选中即记账（asked_count+1）。

    选择标准：impact / cost 最高——同样的提问代价，先问影响理解
    最深的。并列时保持库序（active_doubts 已按 impact DESC 排），
    不引入第二把尺子。
    """
    dcfg = getattr(cfg, "doubts", None)
    cooldown = int(getattr(dcfg, "cooldown_turns", DEFAULT_COOLDOWN))
    max_ask = int(getattr(dcfg, "max_ask_count", DEFAULT_MAX_ASK))

    best = None
    best_score = -1.0
    for d in store.active_doubts(session, limit=50):
        if not should_ask(d, turn, asked_this_turn,
                          now_ts=now_ts, cooldown_turns=cooldown,
                          max_ask_count=max_ask):
            continue
        score = d.impact / max(d.cost, 0.01)   # cost 下限防除零
        if score > best_score:
            best, best_score = d, score

    if best is None:
        return None
    store.mark_doubt_asked(best.id, turn)
    log.info("doubt.pick", extra={"fields": {
        "id": best.id, "turn": turn, "impact": best.impact, "cost": best.cost}})
    return best


def format_doubt(question: str) -> str:
    return ASK_TEMPLATE.format(q=question)


# ================================================================ M18.3 生命周期

def expire_to_memory(store: Any, *, session: str = "",
                     now_ts: int | None = None,
                     source: str = "ai") -> list[int]:
    """过期疑问 → kind=doubt 的 cold 事件。返回转出的事件 id。

    "不能凭空消失"是手册的原话。问不出来的疑问要变成"我忘了"的
    体感来源：若干天后相关话题出现，这条 cold 事件被检索到，模型
    自然说出"这个我当时好像没问清楚"——比一句硬编码的"我记性不好"
    有说服力得多。

    converted 标记在写入成功后才置位：转记忆失败时 doubt 还在池子里，
    下轮巩固重试——宁可重复也不丢（和 persist_degraded 同一个取向）。
    """
    rows = store.expired_doubts(session=session, now_ts=now_ts)
    out: list[int] = []
    for d in rows:
        summary = f"某个问题当时没搞清楚：{d.question}"
        eid = store.add_event(
            session=d.session,
            raw=d.question,                      # 原文就是疑问句本身
            summary=summary,
            keywords=_keywords_of_question(d.question),
            kind="doubt",
            tier=3,                              # cold：只做"体感"，不占注入预算
            w0=0.15,
            source=source,
        )
        store.mark_doubt_converted(d.id)
        out.append(eid)
        log.info("doubt.expired_to_memory", extra={"fields": {
            "doubt": d.id, "event": eid, "question": d.question[:30]}})
    return out


# 疑问句的功能词。去掉它们剩下的才是"问的是什么"。
_QUESTION_FILLERS = (
    "什么", "怎么", "为什么", "哪里", "哪儿", "多少", "几个", "哪些",
    "是不是", "有没有", "能不能", "会不会", "是谁", "谁", "吗", "呢",
    "请问", "告诉我", "还记得", "记得", "想知道", "想问", "请问",
    "哪天", "啥", "具体",
)

# 功能虚词，切关键词/块时顺手劈开（"叫""用""做"是黏在实体前的动词，
# 不劈开的话块变成"报表模块用"这种连动串，回答里换个措辞就匹配不上）。
_QUESTION_GLUE = r"[的了在是有啊吧呀要会能叫用做，,。？?！!、]"

# 自我指称前缀。几乎所有疑问都以"用户的……"开头，留着它任何两句
# 问话互相都是 0.3+ 的相似度，问答匹配则会被"用户"这个 bigram 误连。
_SELF_PREFIX = r"^(用户的?|我的?)"


def _question_core(question: str) -> str:
    """把疑问句砍到"实体 + 动作"，供问答匹配用。

    「她的猫叫什么名字」→「她的猫叫名字」。粗糙，但方向保守：
    砍多了匹配不上（漏），砍错了也只会漏不会错关。
    """
    import re as _re
    s = question
    for w in _QUESTION_FILLERS:
        s = s.replace(w, "")
    return _re.sub(_SELF_PREFIX, "", s)


# 单字代词块直接踢掉：留着一个"她"，任何含她的句子都能白捡 0.17+。
_PRONOUN_BLOCKS = frozenset("她他它你我")


def _question_blocks(question: str) -> list[str]:
    """疑问句 → 实义块列表，问答匹配的最小单元。

    「用户的老家是哪个城市」→ [老家, 城市]。
    「用户下周要独立接什么需求」→ [下周, 独立, 需求]。
    块比 bigram 稳：bigram 会被虚词切碎（"老家"变"老家/家是"），
    块是完整的实体词，出现与否语义明确。单字块保留——"猫""班"
    这类单字名词在中文里是正经实体，但在打分时降权（见 answer_overlap）。
    """
    import re as _re
    core = _question_core(question)
    return [b for b in _re.split(_QUESTION_GLUE, core)
            if b and b not in _PRONOUN_BLOCKS]


def answer_overlap(question: str, answer: str) -> float:
    """问答匹配度：疑问句实义块在回答文本中的命中比例（单字块计半权）。

    为什么不用 trigram Jaccard：实测「用户的老家是哪个城市」vs
    「用户老家是四川成都的」Jaccard = 0.0——问答的表述差异太大。
    为什么不用 bigram 覆盖率：实测「用户的房贷月供是多少」vs
    「带用户的导师姓什么」= 0.29——"用户/户的"这两个自我指称
    bigram 把任何两句问话连在一起，会误关。块是去掉自我前缀、
    劈掉虚词之后的实义词；单字块（"猫""班"）保留但只算半权，
    否则一个"她"字就能把任何句子连上 0.33。

    校准记录（实算值，见 R-18.3）：
      「用户的老家是哪个城市」/「用户老家是四川成都的」→ 0.50
      「她的猫叫什么名字」/「她的猫叫煤球」→ 0.50
      「她的猫叫什么名字」/「她说她下周忙」→ 0.00（代词块被踢，不误关）
      「用户的房贷月供是多少」/「带用户的导师姓什么」→ 0.00
      「用户下周要独立接什么需求」/「说是报表优化，下周就开始」→ 0.50
    """
    blocks = _question_blocks(question)
    if not blocks:
        return 0.0
    total = sum(1.0 if len(b) >= 2 else 0.5 for b in blocks)
    hit = sum(1.0 if len(b) >= 2 else 0.5 for b in blocks if b in answer)
    return hit / total


def _keywords_of_question(question: str) -> list[str]:
    """疑问句 → 检索关键词。

    不能直接 clean_keywords(question)：question 是没有分词的整句，
    会原样变成一个 10 字长词——倒排里它是孤岛，什么查询都命不中它，
    这条记忆就成了死数据。按虚词和功能词劈开：「用户的老家在哪个
    城市」→ [用户, 老家, 城市]。
    """
    import re as _re
    core = _question_core(question)
    pieces = _re.split(_QUESTION_GLUE, core)
    out, seen = [], set()
    for p in pieces:
        p = p.strip()
        if len(p) >= 2 and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


# 覆盖率阈值。校准记录（都是实算值，不是拍脑袋）：
#   匹配对：「用户的老家是哪个城市」/「用户老家是四川成都的」→ 0.50
#   匹配对：「她的猫叫什么名字」/「她的猫叫煤球」→ 1.00（块[名字]? 实测见测试）
#   不匹配：「用户的房贷月供是多少」/「带用户的导师姓什么」→ 0.00
# 阈值取 0.25：正例离得远，反例也离得远。真实对话里这个判据偏保守，
# 漏 match 的代价由限流器的"最多问 2 次"兜底——不会无限骚扰。
RESOLVE_OVERLAP = 0.25


def resolve_matching_doubts(
    store: Any,
    session: str,
    texts: Sequence[str],
    *,
    overlap: float = RESOLVE_OVERLAP,
) -> int:
    """用户自己把答案说出来了 → 对应 doubt 关闭。返回关闭条数。

    判据：answer_overlap ≥ 0.25（校准数据见 RESOLVE_OVERLAP 注释）。
    偏低的代价是偶尔多关（把沾边的疑问也关了），偏高的代价是永远
    关不上、AI 反复追问用户已经回答过的事——后者对体感的破坏大
    得多，两害取轻。

    匹配即关闭：答案已经在 facts 里了（persist 的正常流程），doubt
    的使命完成，留着只会被限流器反复挑出来问已答过的问题。
    """
    if not texts:
        return 0
    closed = 0
    for d in store.active_doubts(session, limit=50):
        if any(answer_overlap(d.question, t) >= overlap for t in texts):
            store.resolve_doubt(d.id)
            closed += 1
            log.info("doubt.resolved_by_user", extra={"fields": {
                "id": d.id, "question": d.question[:30]}})
    return closed
