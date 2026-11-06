"""夜间巩固（M15）：把"同一件事说了三遍"合成一条。

## 这个模块为什么要写这么长的注释

因为它的判据是**用量出来的**，不是拍出来的，而量出来的结论和手册不一样。
不写清楚，下一个人会以为阈值是随手定的，然后随手改掉。

## 判据怎么定的（`tools/probe_dup.py` 的结论）

手册给的是「关键词 Jaccard × 0.6 + 摘要 trigram Jaccard × 0.4，阈值 0.75」。
在真机库和 30 天模拟数据上跑，这套东西**归零**：

    应合并的对里，手册公式的最高分是 0.62，中位数 0.12 —— 0.75 一条都够不着
    原因：重复陈述往往第二遍更简短，Jaccard 的分母被第一遍的丰富度撑大
         （这就是 M12 的 D75 已经量过一次的同一个坑）

换成**覆盖率**（分母取较小一方）之后，分数才和"是不是同一件事"对上。
但覆盖率单独用会连锁：`住朝阳`—`更正为海淀`—`搬家到海淀` 会被串成
一个 5 条的簇，把"改过"这件事抹平。

最后定下来的三层：

    1. 候选对   同 session、30 天内、至少 1 个关键词重合（倒排配对，不 O(n²)）
    2. 强判据   覆盖率 + 小样本时要求完全包含（cov_strict）→ 自动合并
    3. 弱判据   只有覆盖率过线（kw_cov）→ 合并但记 strong=0，可 unmerge

## 一个必须承认的天花板

词面方法分不开两种形状完全一样的对：

    "北大人民医院复查" vs "医院复查"      同一件事，该合
    "微信群里吵起来"   vs "搬工位吵一下"   两件事，不该合

两条都只有 2 个关键词、共享 1 个、覆盖率都是 0.5。**没有任何词面信号
能区分它们**（字符余弦、原文 bigram、IDF 加权都试过，都分不开）。

所以策略是：**宁可漏合，不可错合**。
漏合只是效率问题（多占一个 prompt 名额），错合是不可逆的信息损坏
（原文还在，但检索层再也看不见那条独立的记忆）。
强判据选"小样本要求完全包含"，就是这个取舍的直接体现。
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..log import get_logger
from . import doubt
from .telemetry import Span

log = get_logger("consolidate")

# 时间窗。手册写 7 天，实测不够：30 天模拟数据里 53% 的"同一件事"
# 两次提到之间隔了 7 天以上（"我不吃辣"这种事会跨整月反复讲）。
# 放宽到 30 天的代价是候选对变多，靠倒排配对和不要求词覆盖来控。
TIME_GAP_DAYS = 30

# 单簇上限。超过说明在链式蔓延（single-linkage 的经典病症），截断。
CLUSTER_MAX = 6

# 强判据阈值：覆盖率。0.5 = "较小一方的词有一半出现在对方里"。
STRONG_THR = 0.5
# 弱判据阈值：同样的分数，但样本小时不要求完全包含。
WEAK_THR = 0.5

# 更正 / 否认标记。命中的事件**不参与合并**。
# 它的价值就在"改过"这个动作本身，合并掉等于把变化抹平：
# 库里只剩"住海淀"，再没人知道用户说过"住朝阳"然后改口。
# 实测这条刹车在真机库里挡住了 (31,32)「住朝阳 → 说错了是海淀」，
# 在模拟数据里把误合并从 4 条压到 2 条。
# 「算了」也收进来：它在改变主意的语境里几乎总意味着"推翻前面说的"
# （"我现在就想辞职…算了，先忍着吧"）。加它的风险是把"算了，就这样"
# 这类无争议的话也冻结——而冻结的代价是**漏合**，不是错合，可以宽容。
CORRECTION = re.compile(r"不对|说错|其实是|更正|记错|不是这样|搞错|改主意|反悔|算了")


# ---------------------------------------------------------------- 数据结构

@dataclass
class DupRow:
    """巩固流程里的一个事件。字段是 `consolidate_rows()` 的返回子集。"""

    id: int
    event_time: int
    keywords: set[str]
    summary: str
    raw: str
    weight: float
    recall_count: int = 0
    tier: int = 2
    pinned: int = 0

    @classmethod
    def of(cls, d: dict[str, Any]) -> "DupRow":
        return cls(
            id=int(d["id"]),
            event_time=int(d["event_time"]),
            keywords=set((d.get("keywords") or "").split()),
            summary=d.get("summary") or "",
            raw=d.get("raw") or "",
            weight=float(d.get("weight") or 0.0),
            recall_count=int(d.get("recall_count") or 0),
            tier=int(d.get("tier") or 2),
            pinned=int(d.get("pinned") or 0),
        )


@dataclass
class Cluster:
    """一组被判为"同一件事"的事件。`members[0]` 是主记录。"""

    members: list[DupRow]
    strong: bool
    score: float

    @property
    def master(self) -> DupRow:
        return self.members[0]

    @property
    def size(self) -> int:
        return len(self.members)

    def merged_keywords(self) -> str:
        """簇内关键词并集。

        **必须并集**：被合并的事件不再出现在检索结果里，它们的说法如果
        不同步到主记录，那些词就再也搜不到了。原文还在库里，但检索不到——
        这是合并最隐蔽的损失。
        """
        seen: list[str] = []
        for m in self.members:
            for t in sorted(m.keywords):
                if t not in seen:
                    seen.append(t)
        return " ".join(seen[:12])   # 上限 12：再多倒排就退化成"全命中"


@dataclass
class Report:
    """一轮巩固的结果。字段对齐 `hippo consolidate` 的输出。"""

    scanned: int = 0
    pairs: int = 0
    clusters: int = 0
    merged: int = 0
    strong: int = 0
    weak: int = 0
    summary_ok: int = 0
    summary_fail: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    elapsed_ms: float = 0.0
    # 合并后还剩多少重复（按簇内同实体估算不了，只报结构指标）
    before: int = 0
    after: int = 0
    doubts_converted: int = 0        # M18.3：过期疑问转成 cold 事件的条数

    def line(self) -> str:
        s = (f"{self.scanned} 条扫出 {self.clusters} 簇，合并 {self.merged} 条"
             f"（强 {self.strong} / 弱 {self.weak}）"
             f" | {self.pairs} 候选对  {self.elapsed_ms:.0f}ms")
        if self.prompt_tokens or self.completion_tokens:
            s += (f"  | 模型 {self.prompt_tokens}+{self.completion_tokens} tok"
                  f" ¥{self.cost:.4f}")
        return s


# ---------------------------------------------------------------- 相似度

def _coverage(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def kw_cov(a: DupRow, b: DupRow) -> float:
    """关键词覆盖率。分母取较小一方。

    为什么不是 Jaccard：重复的典型形状是"A 说得多、B 说得少"。
    用并集当分母等于惩罚"第一次说得详细"，方向正好反了。
    """
    return _coverage(a.keywords, b.keywords)


def cov_strict(a: DupRow, b: DupRow) -> float:
    """小样本时要求完全包含的覆盖率（强判据）。

        原文逐字相同            → 1.0
        词数少的一方 ≥ 3        → 覆盖率
        词数少的一方 = 2        → 必须完全包含

    卡在 m=2 的原因：抽取器只给 2 个关键词时，"共享 1 个"就是 0.5，
    而 0.5 落在任何合理阈值上都会过线——此时**共享 1 个词根本不是证据**。
    实测的 5 个误合并全是这个形状，共享的那个词往往是"吵""加班""忌口"
    这类泛词。

    代价：真重复里也有 2 词共享 1 个的（"北大人民医院复查" vs "医院复查"），
    会被一起挡掉，落进弱判据那一档。
    """
    if a.raw.strip() == b.raw.strip():
        return 1.0
    m = min(len(a.keywords), len(b.keywords))
    if m < 2:
        return 0.0
    s = len(a.keywords & b.keywords)
    if m == 2:
        return 1.0 if s == 2 else 0.0
    return s / m


# 弱判据在"2 词共享 1 个"时额外要求的字符余弦门槛。**默认关闭（0.0）**。
#
# 为什么留着这个旋钮而不是删掉：它是一条真实存在的取舍曲线，实测数据如下
# （30 天模拟数据，142 条事件，初始重复率 18.3%，M15 收工时的最终口径）：
#
#     CHAR_THR   合并后重复率   跨话题误合
#     0（关闭）      4.9%          1.4%
#     0.10           5.6%          1.4%
#     0.25           8.5%          0.7%
#     0.35           9.2%          0.7%
#     0.50           9.2%          0.0%
#
# **没有任何一档能同时满足"重复率 <5%"和"误合并 <2%"**：0 那一档误合达标
# （1.4% < 2%）但重复率只压到 4.9%，再想压重复就得往上调，而往上调重复率
# 立刻超标。这不是调参没调好，是词面方法的天花板：两条各 2 个词、撞上 1 个
# 泛词时，"同一件事的两种说法"和"两件不相干的事"在词面上完全同形，
# 只能靠原文余弦去猜。
#
# 默认选 0（信任判据、多合并、剩下靠 unmerge 兜），因为：
#   - 重复率是**每轮都在付的成本**（多占 prompt），误合并是一次性的
#   - 误合并可以 `hippo unmerge` 撤销，重复率不行
#   - 摘要重生成会把差异如实写进摘要（实测"先说住朝阳，后更正为海淀"），
#     所以误合并的实际信息损失远小于"两条记忆被抹成一条"的字面意思
#
# 想更保守就把这个值调到 0.25（重复率 8.5%，误合 0.7%）。
CHAR_THR = 0.0


def _char_cos(a: str, b: str) -> float:
    """去虚词字集的余弦相似度。

    用字不用 bigram：中文一换语序 bigram 就掉光（"我吃辣" vs "辣我吃"共享 0），
    而字是最稳的单位。去虚词是因为"我/的/了/不/一/点/都"在任何两句中文里
    都会撞上，留着等于把"文本长度"当相似度。
    """
    from .keywords import STOPWORDS

    def sift(t: str) -> set[str]:
        return {ch for ch in t if ch.strip() and ch not in STOPWORDS
                and not ch.isascii()}

    ca, cb = sift(a), sift(b)
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / (len(ca) * len(cb)) ** 0.5


# ---------------------------------------------------------------- 候选对

def candidate_pairs(rows: Sequence[DupRow], gap_days: int = TIME_GAP_DAYS
                    ) -> list[tuple[DupRow, DupRow]]:
    """候选对：时间窗内、至少共享 1 个关键词。

    用倒排配对而不是全量 `combinations`：1 万条事件全量是 5000 万对，
    倒排配对的开销是 Σ df(token)²，实测低两个数量级。
    """
    gap = gap_days * 86400
    bucket: dict[str, list[DupRow]] = {}
    for r in rows:
        for t in r.keywords:
            bucket.setdefault(t, []).append(r)

    seen: set[tuple[int, int]] = set()
    out: list[tuple[DupRow, DupRow]] = []
    for lst in bucket.values():
        if len(lst) < 2:
            continue
        for a, b in itertools.combinations(lst, 2):
            key = (a.id, b.id) if a.id < b.id else (b.id, a.id)
            if key in seen:
                continue
            seen.add(key)
            if abs(a.event_time - b.event_time) <= gap:
                out.append((a, b))
    return out


# ---------------------------------------------------------------- 聚类

class _DSU:
    def __init__(self, ids: Iterable[int]):
        self.p = {i: i for i in ids}

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> int:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra
        return self.find(a)


def find_clusters(rows: Sequence[DupRow], *,
                  gap_days: int = TIME_GAP_DAYS,
                  strong_thr: float = STRONG_THR,
                  weak_thr: float = WEAK_THR,
                  max_size: int = CLUSTER_MAX,
                  limit_pairs: int = 0) -> list[Cluster]:
    """跑聚类。返回**按簇大小降序**的簇列表（只含 ≥2 条的）。

    两步走，不是一个阈值：
      1. 先用强判据 union 一遍（高置信，纯度高）
      2. 再对剩下的单点，用弱判据尝试挂到已有簇上

    为什么分两步而不是"取低阈值一次跑完"：一次跑的话，弱判据的边会
    在强判据的边之间穿针引线，把两个本该分开的簇缝起来。
    分两步时，强簇的骨架先定下来，弱边只能"往簇上挂"，不能"把簇连起来"。
    """
    by_id = {r.id: r for r in rows}
    pairs = candidate_pairs(rows, gap_days)
    if limit_pairs and len(pairs) > limit_pairs:
        log.warning("consolidate.pairs_capped",
                    extra={"fields": {"got": len(pairs), "cap": limit_pairs}})
        pairs = pairs[:limit_pairs]

    score_cache: dict[tuple[int, int], tuple[float, float]] = {}

    def scores(a: DupRow, b: DupRow) -> tuple[float, float]:
        k = (a.id, b.id) if a.id < b.id else (b.id, a.id)
        v = score_cache.get(k)
        if v is None:
            v = (cov_strict(by_id[k[0]], by_id[k[1]]),
                 kw_cov(by_id[k[0]], by_id[k[1]]))
            score_cache[k] = v
        return v

    frozen = {r.id for r in rows if CORRECTION.search(r.raw)}

    dsu = _DSU([r.id for r in rows])
    members: dict[int, list[int]] = {r.id: [r.id] for r in rows}
    strong_of: dict[int, float] = {}      # 根 → 这一簇靠的强分

    # ---- 第 1 步：强判据
    strong_edges = sorted(
        ((scores(a, b)[0], a, b) for a, b in pairs), key=lambda e: -e[0])
    for s, a, b in strong_edges:
        if s < strong_thr:
            break
        if a.id in frozen or b.id in frozen:
            continue
        ra, rb = dsu.find(a.id), dsu.find(b.id)
        if ra == rb or len(members[ra]) + len(members[rb]) > max_size:
            continue
        root = dsu.union(a.id, b.id)
        merged_ids = members.pop(ra, []) + members.pop(rb, [])
        members[root] = merged_ids
        strong_of[root] = max(strong_of.get(ra, 0.0), strong_of.get(rb, 0.0), s)

    # ---- 第 2 步：弱判据，只允许"单点挂到簇上"
    weak_edges = sorted(
        ((scores(a, b)[1], a, b) for a, b in pairs), key=lambda e: -e[0])
    for s, a, b in weak_edges:
        if s < weak_thr:
            break
        # **frozen 在第二步也必须检查**。第一版只写在强判据里，结果
        # 真机库的「二十八 → 哦不对，我二十九了」照样被合了：强判据没合它，
        # 弱判据把它捡回去了。更正刹车是"这条不参与任何合并"，
        # 不是"这条不给强证据"——两者差很远。
        if a.id in frozen or b.id in frozen:
            continue
        # 小样本时再要一道原文证据。两条各 2 个词、撞上 1 个泛词的话，
        # 覆盖率给的是 0.5（虚高），而这 0.5 在词面上和"同一件事的两种说法"
        # 完全同形。原文余弦能分开：同话题的实词几乎重合，泛词撞上的不重合。
        if min(len(a.keywords), len(b.keywords)) == 2 and _char_cos(a.raw, b.raw) < CHAR_THR:
            continue
        ra, rb = dsu.find(a.id), dsu.find(b.id)
        if ra == rb:
            continue
        la, lb = len(members[ra]), len(members[rb])
        # 只允许"1 + n"的合并：两边都有 ≥2 条说明是两个已成形的簇，
        # 拿弱判据把它们缝在一起风险太大。
        if la > 1 and lb > 1:
            continue
        if la + lb > max_size:
            continue
        root = dsu.union(a.id, b.id)
        members[root] = members.pop(ra, []) + members.pop(rb, [])
        strong_of.setdefault(root, 0.0)

    # ---- 收口
    groups: dict[int, list[DupRow]] = {}
    for r in rows:
        groups.setdefault(dsu.find(r.id), []).append(r)

    out: list[Cluster] = []
    for root, g in groups.items():
        if len(g) < 2:
            continue
        # 主记录：weight 最高，平手取 event_time 最早（第一次提到信息最全）
        g.sort(key=lambda r: (-r.weight, r.event_time, r.id))
        s = strong_of.get(root, 0.0)
        out.append(Cluster(members=g, strong=s >= strong_thr, score=s))
    out.sort(key=lambda c: (-c.size, c.master.event_time))
    return out


# ---------------------------------------------------------------- 合并

def cluster_cost(cluster: Cluster) -> int:
    """估算重生成摘要要花多少 token（不实际调用）。"""
    n = sum(len(m.raw) for m in cluster.members)
    return int(n * 1.5) + 120


def build_summary_prompt(cluster: Cluster, *, max_len: int = 60) -> list[dict[str, str]]:
    """组"重生成摘要"的 messages。

    **把簇内所有 raw 一起喂**，不是"拿旧摘要再摘要"。手册 §M15.2 明令禁止
    后者，理由是所有摘要系统都逃不掉的那条：每摘要一次掉一层信息，
    三轮之后摘要和原文没关系了，而且**不会有任何报错**。
    """
    lines = [f"{i}. {m.raw}" for i, m in enumerate(cluster.members, 1)]
    body = "\n".join(lines)
    prompt = (
        "下面是同一个人在不同时间说的几句话，说的是同一件事。\n"
        "请把它们**合并成一句中文摘要**，要求：\n"
        f"- 不超过 {max_len} 字\n"
        "- 第三人称，主语用「用户」\n"
        "- 保留所有具体细节（人名、数字、地点、原因、感受）\n"
        "- 不要添加原文没有的信息，不要推测\n"
        "- 如果几句话之间有矛盾或更正，如实写明（例如「先说不吃辣，后说微辣可以」）\n"
        "\n只输出 json：{\"summary\": \"...\"}\n\n"
        f"原话：\n{body}\n"
    )
    return [{"role": "user", "content": prompt}]


def parse_summary(text: str, *, max_len: int = 80) -> str:
    """从模型输出里抠摘要。解析失败返回空串（调用方保留原摘要）。"""
    import json as _json

    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    try:
        d = _json.loads(t)
        s = str(d.get("summary") or "").strip()
    except (ValueError, AttributeError):
        # 模型没按 json 输出时，退回"取第一行去掉引号"
        s = t.splitlines()[0].strip().strip('"').strip() if t else ""
    if not s:
        return ""
    # 太长就截断——截断比"超长摘要挤占 prompt"好，但必须留痕
    if len(s) > max_len:
        s = s[:max_len - 1] + "…"
    return s


# ---------------------------------------------------------------- 主流程

def run(store: Any, session: str, *, now: int | None = None,
        dry_run: bool = False, summarizer: Any = None,
        gap_days: int = TIME_GAP_DAYS,
        pricing: dict[str, float] | None = None) -> Report:
    """跑一轮巩固。

    `summarizer` 是一个**同步**可调用：`(messages) -> (text, usage_dict)`。
    为 None 时**不重生成摘要**，只做结构合并（supersede + 关键词并集）。
    这不是降级凑数——关键词并集已经能保证检索不丢，摘要是"更好读"，
    不是"能不能找到"。

    为什么要求同步：巩固是逐簇串行的（摘要要基于合并后的结果，不能并发），
    一个事件循环就够。让每个簇各自 `asyncio.run()` 会重复建 loop，
    上千个簇时会慢得莫名其妙。CLI 那边持有一个 loop 复用（见 `_Summary`）。
    """
    import time as _time

    t0 = _time.perf_counter()
    rep = Report()

    # M18.3：过期疑问 → cold 事件。放在 clusters 判空之前——巩固不是
    # 只服务"有重复记忆的会话"，疑问过期的会话一条重复都没有，
    # 提前返回会让它们的疑问永远转不出池子。
    try:
        rep.doubts_converted = len(doubt.expire_to_memory(
            store, session=session,
            now_ts=now if now is not None else int(_time.time())))
    except Exception as exc:                # 疑问转记忆失败不阻断合并
        rep.doubts_converted = 0
        log.warning("consolidate.doubt_expire_failed",
                    extra={"fields": {"session": session, "err": str(exc)}})

    rows = [DupRow.of(d) for d in store.consolidate_rows(session, gap_days=gap_days)]
    rep.scanned = len(rows)
    rep.before = len(rows)

    clusters = find_clusters(rows, gap_days=gap_days)
    rep.clusters = len(clusters)
    rep.pairs = len(candidate_pairs(rows, gap_days))

    if not clusters:
        rep.after = len(rows)
        rep.elapsed_ms = (_time.perf_counter() - t0) * 1000
        return rep

    ts = now if now is not None else int(_time.time())

    for cl in clusters:
        master = cl.master
        slaves = [m.id for m in cl.members[1:]]
        if dry_run:
            rep.merged += len(slaves)
            rep.strong += len(slaves) if cl.strong else 0
            rep.weak += 0 if cl.strong else len(slaves)
            continue

        # ---- 摘要在写库之前算好。算完再写，避免"改了摘要但没合并"
        new_summary = ""
        tokens = 0
        cost = 0.0
        if summarizer is not None:
            try:
                text, usage = summarizer(build_summary_prompt(cl))
                new_summary = parse_summary(text)
                # 用 Span 吃 usage 再算钱，不自己写一遍公式：OpenAI 和 DeepSeek
                # 的 usage 形状不同（cached_tokens 一个在顶层、一个在
                # prompt_tokens_details 里），这个坑 telemetry 已经踩过，
                # 抄一遍必然抄漏一处，而漏掉的表现是"成本永远是 0"。
                sp = Span()
                sp.absorb_usage(usage)
                tokens = sp.prompt_tokens + sp.completion_tokens
                cost = sp.cost(pricing)
                rep.prompt_tokens += sp.prompt_tokens
                rep.completion_tokens += sp.completion_tokens
                if new_summary:
                    rep.summary_ok += 1
                else:
                    rep.summary_fail += 1
            except Exception as exc:            # 摘要失败不阻断合并
                rep.summary_fail += 1
                log.warning("consolidate.summary_failed",
                            extra={"fields": {"master": master.id, "err": str(exc)}})

        # recall_count 取 **max 不取 sum**（R-14-Gate 的交代）：
        # 求和会让一条合并了 50 次旧事的记忆直接顶到 cap，半衰期 ×2.25 白送。
        rc = max((m.recall_count for m in cl.members), default=0)

        n = store.merge_into(
            master.id, slaves,
            keywords=cl.merged_keywords(),
            summary=new_summary or None,
            recall_count=rc,
        )
        store.log_consolidation(
            session=session, master_id=master.id, member_ids=slaves,
            strong=cl.strong, score=cl.score,
            summary_old=master.summary, summary_new=new_summary or "",
            tokens=tokens, cost=cost, ts=ts,
        )
        rep.merged += n
        if cl.strong:
            rep.strong += n
        else:
            rep.weak += n
        rep.cost += cost

    rep.after = len(rows) - rep.merged
    rep.elapsed_ms = (_time.perf_counter() - t0) * 1000
    log.info("consolidate.done", extra={"fields": {
        "session": session, "merged": rep.merged, "clusters": rep.clusters}})
    return rep


# ---------------------------------------------------------------- 指标

def dup_rate(store: Any, session: str, *, gap_days: int = TIME_GAP_DAYS) -> float:
    """当前库里的"重复率"：还能被合并的事件 / 全部事件。

    定义和 `tools/probe_dup.py` 的 `residual` 一致：数"还剩多少该合没合"。
    合并做到位时这个数会降到一个稳定的小值（剩下的都是词面分不开的那种）。
    """
    rows = [DupRow.of(d) for d in store.consolidate_rows(session, gap_days=gap_days)]
    if not rows:
        return 0.0
    clusters = find_clusters(rows, gap_days=gap_days)
    extra = sum(len(c.members) - 1 for c in clusters)
    return extra / len(rows)
