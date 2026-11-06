"""重复判定探针（M15.1 开工前）。

## 为什么先写这个

手册给的公式是「关键词 Jaccard × 0.6 + 摘要 trigram Jaccard × 0.4，阈值 0.75」。
在真实措辞差异下这个阈值几乎不可能达到，所以要先量，再决定实现。

## 指标为什么不是 pairwise recall（一个自己踩过的坑）

第一版探针算"应合并的对里命中了多少对"，跑出来召回只有 33%，
看着像判据不行。其实**指标本身是错的**：

    同一件事的 6 条事件，只要有一条链路把它们连起来就行，
    不需要两两之间都有边。

    A—B—C 三跳连通，pairwise 只看 A-C 那一对，会判成"漏了"，
    而实际聚类结果是对的。

所以评估必须**先跑完整个聚类，再看簇的质量**：

    dup_rate_after    (事件数 - 簇数) / 事件数        ← 就是 DoD 那个数
    purity            簇内最多同一话题的占比（加权）  ← 误合并的度量
    completeness      每个话题被并进同一个簇的比例    ← 漏合并的度量
    cross_merge       真的有跨话题事件被合进同簇      ← 不可逆的错误，单独数

## 两套数据

  `--db`     真机库 44 条 + 内嵌人工标注（验"更正型误合并"，样本小）
  `--jsonl`  30 天模拟数据（`tools/gen_dialog30.py`，带 truth 字段，验召回）

两套都要跑：真机库验不出召回（重复形状太规整），模拟数据验不出"更正型误合并"
（真机库才有真实的更正对）。

用法：
    python3 -m tools.probe_dup --db /tmp/h2/mem.db
    python3 -m tools.probe_dup --jsonl tests/fixtures/dialog_30d.jsonl --pairs
"""

from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------- 真机库标注
#
# 写死在代码里而不是外置 json：这份标注是**针对真机库那 44 条**的，
# 换个库就失效。写死至少会在换库时让人看见"这个 id 不存在"的报错，
# 而不是悄悄拿一份对不上的标注去算指标。

REDUNDANT: list[tuple[list[int], str]] = [
    ([6, 26, 27, 28, 29, 30], "同一件事：用户不吃辣"),
    ([11, 12, 13], "同一件事：被老板当众骂"),
]

COMPLEMENTARY: list[tuple[list[int], str]] = [
    ([16, 17], "自我介绍：公司职级 / 技术栈"),
    ([36, 37], "老王小李来北京：这事本身 / 怎么来的"),
]

MUST_NOT: list[tuple[list[int], str]] = [
    ([31, 32], "更正：朝阳 → 海淀"),
    ([33, 34], "更正：二十八 → 二十九"),
    ([14, 15], "状态变化：想辞职 → 先忍着"),
    ([6, 7], "矛盾：一点辣都受不了 / 微辣还行"),
    ([26, 28], "同一事实的不同侧重点，措辞差异大"),
]


@dataclass
class Row:
    id: int
    kw: set[str]
    summary: str
    raw: str
    event_time: int
    weight: float = 0.5
    truth: str | None = None


def load_db(db: str) -> list[Row]:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    out = []
    for r in c.execute(
        "SELECT id, keywords, summary, raw, event_time, weight FROM events "
        "WHERE kind = 'event' AND superseded_by IS NULL AND archived = 0"
    ):
        out.append(Row(
            id=r["id"], kw=set((r["keywords"] or "").split()),
            summary=r["summary"] or "", raw=r["raw"] or "",
            event_time=r["event_time"], weight=r["weight"],
        ))
    c.close()
    return out


def load_jsonl(path: str) -> list[Row]:
    out = []
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        d = json.loads(line)
        out.append(Row(
            id=i + 1, kw=set(d.get("keywords") or []),
            summary=d.get("summary") or "", raw=d.get("raw") or "",
            event_time=int(d["ts"]), truth=d.get("truth"),
        ))
    return out


# ---------------------------------------------------------------- 相似度

def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def coverage(a: set, b: set) -> float:
    """分母取小的那个。**这是 M12 的 D75 结论，不是新发明**。

    「A 说得多、B 说得少」是重复的典型形状：第二次提到往往更简短。
    用并集当分母等于惩罚"第一次说得详细"，方向正好反了。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def trigrams(s: str) -> set[str]:
    s = "".join(ch for ch in s if ch.strip())
    if len(s) < 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def bigrams(s: str) -> set[str]:
    s = "".join(ch for ch in s if ch.strip())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


# ---------------------------------------------------------------- 判据

def manual(a: Row, b: Row) -> float:
    """手册公式。"""
    return jaccard(a.kw, b.kw) * 0.6 + jaccard(trigrams(a.summary), trigrams(b.summary)) * 0.4


def kw_jac(a: Row, b: Row) -> float:
    return jaccard(a.kw, b.kw)


def kw_cov(a: Row, b: Row) -> float:
    return coverage(a.kw, b.kw)


def raw_tri(a: Row, b: Row) -> float:
    return jaccard(trigrams(a.raw), trigrams(b.raw))


def raw_cov(a: Row, b: Row) -> float:
    return coverage(bigrams(a.raw), bigrams(b.raw))


def _chars(text: str) -> set[str]:
    """去虚词后的字集合。

    用「字」不用「词」也不用「bigram」：bigram 一换语序就掉（"我吃辣"
    vs "辣我吃"共享 0 个 bigram），而字是最稳的单位；去虚词是因为
    "我/的/了/不/一/点/都" 这类字在任何两句中文里都会撞上，
    留着它们等于把"文本长度"当成了相似度。
    """
    from hippo.core.keywords import STOPWORDS
    return {ch for ch in text if ch.strip() and ch not in STOPWORDS
            and not ch.isascii()}


def char_cos(a: Row, b: Row) -> float:
    """去虚词字集的余弦相似度。

    为什么是余弦不是覆盖率：覆盖率的 min 分母会被「一句长一句短」骗到。
    实测 `我吃辣会胃疼，所以基本不碰`(7 实词) 与 `我不喝冰的，胃不好`(4 实词)
    共享一个"胃"，覆盖率给出 1/4 = 0.25（勉强），余弦给出 1/√28 = 0.19（明确低）。
    而同话题的 `真的，我一点辣都吃不了` vs `我吃不了辣，一点都受不了`
    覆盖率 3/3、余弦 3/√12 = 0.87，两边都高。

    这个判据是在量完 kw_cov 的连锁问题之后加的：kw_cov 会把
    「住朝阳 → 更正为海淀 → 搬家到海淀」串成 5 条（共享"居住"和"海淀"
    各 1 个词就够 0.5），而 char_cos 在这两对上分别是 0.00 和 0.13。
    """
    ca, cb = _chars(a.raw), _chars(b.raw)
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / (len(ca) * len(cb)) ** 0.5


def cov_strict(a: Row, b: Row) -> float:
    """小样本时要求完全包含的覆盖率。

    这是量出"误合并全是 2 词共享 1 个"之后加的。规则：

        raw 完全一样            → 1.0（同一句话写了三遍，没有争议）
        词数少的一方 ≥3         → 覆盖率（和 kw_cov 一样）
        词数少的一方 =2         → 必须完全包含（1.0），否则 0

    为什么卡在 m=2：抽取器给 2 个关键词时，"共享 1 个"就是 0.5，
    而 0.5 落在任何合理阈值上都会过线。此时**共享 1 个词根本不是证据**——
    实测的 5 个误合并（住朝阳/改海淀、不喝冰的/不吃辣、年终奖/先忍着、
    微信群吵/搬工位吵、加班九点/开会加班）全是这个形状：
    两条各只有 2 个词，撞上 1 个，而那个词往往是"吵""加班""忌口"这种泛词。

    代价：真重复里也有 2 词共享 1 个的（"北大人民医院复查" vs "医院复查"），
    会被一起挡掉。**这是明知会漏、选择不漏错的取舍**——漏合并只是效率问题，
    错合并是不可逆的信息损坏。
    """
    if a.raw.strip() == b.raw.strip():
        return 1.0
    m = min(len(a.kw), len(b.kw))
    if m < 2:
        return 0.0
    s = len(a.kw & b.kw)
    c = s / m
    if m == 2:
        return c if s == 2 else 0.0
    return c


def blend(a: Row, b: Row) -> float:
    """关键词覆盖率与字余弦的几何平均。

    不用加权和：两个判据各有各的失效方式（kw_cov 被单个共享泛词抬高，
    char_cos 被"都是短句"抬高），加权和会让任一方单独就把分拉过线。
    几何平均要求**两边都过**，正是想要的。
    """
    k = kw_cov(a, b)
    c = char_cos(a, b)
    if k <= 0 or c <= 0:
        return 0.0
    return (k * c) ** 0.5


def mix(a: Row, b: Row) -> float:
    """关键词覆盖率为主，原文 bigram 覆盖率为辅。

    权重 0.7/0.3 而不是 0.5/0.5：关键词是**抽取器给的标签**，比原文更稳
    （原文里"我""那个"这类字占掉大量 bigram，且措辞一换就掉）；
    但只看关键词会在"关键词只交 1 个"时给出 0.5 的虚高分，
    原文重叠能压住这种误判。
    """
    return kw_cov(a, b) * 0.7 + raw_cov(a, b) * 0.3


CRITERIA = {
    "manual": manual,
    "kw_jac": kw_jac,
    "kw_cov": kw_cov,
    "raw_tri": raw_tri,
    "raw_cov": raw_cov,
    "mix": mix,
    "char_cos": char_cos,
    "blend": blend,
    "cov_strict": cov_strict,
}


# ---------------------------------------------------------------- 聚类

class DSU:
    def __init__(self, ids):
        self.p = {i: i for i in ids}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def candidates(rows: list[Row], gap_days: int, inv: bool = True):
    """候选对：时间窗 + 至少共享 1 词。

    用倒排而不是 itertools.combinations 全量扫：1 万条事件全量是 5000 万对，
    倒排配对的开销是 Σ df(token)²，实测比全量低两个数量级。
    """
    gap = gap_days * 86400
    if not inv:
        return [(a, b) for a, b in itertools.combinations(rows, 2)
                if abs(a.event_time - b.event_time) <= gap and (a.kw & b.kw)]
    by_token: dict[str, list[Row]] = {}
    for r in rows:
        for t in r.kw:
            by_token.setdefault(t, []).append(r)
    seen: set[tuple[int, int]] = set()
    out = []
    for lst in by_token.values():
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


# 更正 / 否认标记。命中的事件**不参与合并**——它的价值就在于"改过"这个动作，
# 合并掉就把变化抹平了：库里只剩"住海淀"，再没人知道用户说过"住朝阳"然后改口。
# 这不只是挡误合并，本身就是语义上该做的事。
CORRECTION = re.compile(r"不对|说错|其实是|更正|记错|不是这样|搞错|改主意|反悔")

CLUSTER_MAX = 6      # 单簇上限。超过说明在链式蔓延，截断


def cluster(rows: list[Row], fn, thr: float, gap_days: int,
            complete: bool = True) -> list[list[Row]]:
    """并查集聚类。

    `complete=True` 走 **complete-linkage**：一条边能合并两个簇，要求两个簇
    **所有跨簇对**的分数都过阈值；`False` 走 single-linkage（任一对过就算）。

    为什么要 complete：single-linkage 会链式蔓延。实测真机库风格的数据上，
    `住朝阳`—`更正为海淀`（共享"居住"，cov 0.5）连上之后，
    `搬家到海淀` 又从 `更正为海淀` 那侧接进来（共享"海淀"，cov 0.33 不够，
    但和 `住朝阳` 那一侧的"朝阳"够），最后 addr_old / addr_fix / move
    三个话题被串成一个 5 条的簇——**这正是我们最不能犯的错**（抹掉"改过"这件事）。

    complete-linkage 的代价是慢一些（每次合并要检查全部跨簇对），
    但夜间巩固跑得起，而错误合并是不可逆的。
    """
    by_id = {r.id: r for r in rows}
    cache: dict[tuple[int, int], float] = {}

    def score(a: Row, b: Row) -> float:
        k = (a.id, b.id) if a.id < b.id else (b.id, a.id)
        v = cache.get(k)
        if v is None:
            v = fn(by_id[k[0]], by_id[k[1]])
            cache[k] = v
        return v

    edges = [(score(a, b), a, b) for a, b in candidates(rows, gap_days)]
    edges = [e for e in edges if e[0] >= thr]
    # 分数高的边先合：先合最像的，再考虑要不要把次像的并进来
    edges.sort(key=lambda e: -e[0])

    dsu = DSU([r.id for r in rows])
    members: dict[int, list[int]] = {r.id: [r.id] for r in rows}
    frozen = {r.id for r in rows if CORRECTION.search(r.raw)}
    for _s, a, b in edges:
        if a.id in frozen or b.id in frozen:
            continue
        ra, rb = dsu.find(a.id), dsu.find(b.id)
        if ra == rb:
            continue
        if len(members[ra]) + len(members[rb]) > CLUSTER_MAX:
            continue
        if complete:
            if not all(score(by_id[x], by_id[y]) >= thr
                       for x in members[ra] for y in members[rb]):
                continue
        dsu.union(a.id, b.id)
        root = dsu.find(a.id)
        members[root] = members.get(root, []) + members.pop(ra, []) + members.pop(rb, [])

    groups: dict[int, list[Row]] = {}
    for r in rows:
        groups.setdefault(dsu.find(r.id), []).append(r)
    return list(groups.values())


def _dup_before(rows: list[Row]) -> int:
    """合并前有多少条是"多余的重复"：Σ(话题条数 - 1)。"""
    from collections import Counter
    c = Counter(r.truth for r in rows if r.truth)
    return sum(v - 1 for v in c.values() if v > 1)


def must_not_hits(groups: list[list[Row]], must_not) -> list[tuple[int, int]]:
    """真机库专用的误合并计数。

    `evaluate()` 的 purity 依赖 `truth` 字段，而真机库没有 truth——
    它只有人工标注的「这几对绝不能合并」。不单独算这一项的话，
    真机库跑出来永远是"纯度 100%"，一个骗人的绿灯。
    """
    where = {}
    for i, g in enumerate(groups):
        for r in g:
            where[r.id] = i
    return [(a, b) for a, b in must_not
            if a in where and b in where and where[a] == where[b]]


def evaluate(rows: list[Row], groups: list[list[Row]]) -> dict:
    """簇质量。

    `dup_after` **不能**用 `(n - 簇数) / n`——那个式子算的是"合并掉了多少"，
    不是"还剩多少重复"。两者在正确合并和错误合并并存时会给出同一个数，
    而这两种情况的后果完全相反。这里直接把"还没合并掉的重复"数出来：

        对每个话题 t：residual += (含 t 的簇数 - 1)
        （理想是 1 簇 = 0 剩余；6 条散在 3 个簇里 = 还剩 2 处该合没合的）

    **这里我写反过一次**：写成 `条数 - 簇数`，于是"6 条各占一簇"算出
    剩余 0（实际还剩 5），"6 条并成 1 簇"算出剩余 5（实际是 0）。
    更糟的是它让指标看起来"阈值越高越好"，与常识相反——
    当时差一点就顺着这个假象去调高阈值了。

    跨话题混进同簇的那部分不计入 residual（那是 purity 的活），
    但会以 `cross` 单独报出来——它是**不可逆**的错误，必须能单独看见。
    """
    from collections import Counter
    n = len(rows)
    n_clusters = len(groups)
    by_truth: dict[str, list[Row]] = {}
    for r in rows:
        if r.truth:
            by_truth.setdefault(r.truth, []).append(r)
    residual = 0
    for t, rs in by_truth.items():
        n_cl = sum(1 for g in groups if any(r.truth == t for r in g))
        residual += max(0, n_cl - 1)
    dup_after = residual / n if n else 0.0

    # purity：每个簇里最多的话题占比，按簇大小加权
    tot = 0
    pure = 0.0
    cross = 0
    for g in groups:
        tr = [r.truth for r in g]
        if len(g) == 1:
            tot += 1
            pure += 1.0
            continue
        from collections import Counter
        c = Counter(tr)
        top = max(c.values())
        pure += top
        tot += len(g)
        # 跨话题混进同簇的条数。None（独立事件）也算跨——
        # 把两件不同的日常琐事合起来，和把两个话题合起来一样是错的。
        if c.get(None, 0) and len(c) > 1:
            cross += len(g) - c[None]
        elif len([k for k in c if k is not None]) > 1:
            cross += len(g) - top
    purity = pure / tot if tot else 1.0

    # completeness：每个多事件话题，被并进同一个簇的比例
    comps = []
    for t, rs in by_truth.items():
        if len(rs) < 2:
            continue
        # 每个簇里有多少条属于这个话题
        best = max(sum(1 for r in g if r.truth == t) for g in groups)
        comps.append(best / len(rs))
    comp = sum(comps) / len(comps) if comps else 1.0

    return {"clusters": n_clusters, "dup_after": dup_after,
            "purity": purity, "comp": comp, "cross": cross,
            "residual": residual}


# ---------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="")
    ap.add_argument("--jsonl", default="")
    ap.add_argument("--pairs", action="store_true", help="打印最大几个簇")
    ap.add_argument("--gap", type=int, default=30, help="时间窗（天）")
    args = ap.parse_args()

    if args.jsonl:
        rows = load_jsonl(args.jsonl)
        source = "模拟数据"
    elif args.db:
        rows = load_db(args.db)
        source = "真机库"
    else:
        print("要 --db 或 --jsonl")
        return 2

    by_id = {r.id: r for r in rows}
    n = len(rows)
    print(f"{source}　事件 {n} 条　时间窗 {args.gap} 天　"
          f"候选对 {len(candidates(rows, args.gap))} / {n * (n - 1) // 2}")

    db = _dup_before(rows)
    print(f"合并前：{n} 条事件，{n - db} 个话题/独立事件，"
          f"多余重复 {db} 条 → 重复率 {db / n:.1%}")

    thr_list = [0.34, 0.40, 0.50, 0.60, 0.67]
    print(f"\n{'判据':<8}{'链':<4}{'阈值':>6}{'簇数':>7}{'剩重复':>8}{'重复率':>8}"
          f"{'纯度':>8}{'完整度':>9}{'跨合':>6}")
    best = []
    for name, fn in CRITERIA.items():
        for complete in (True, False):
            if name in ("raw_tri", "raw_cov") and complete:
                continue      # 单独一个判据的 linkage 差异不值两行表
            for t in thr_list:
                g = cluster(rows, fn, t, args.gap, complete)
                m = evaluate(rows, g)
                tag = "全" if complete else "单"
                print(f"{name:<8}{tag:<4}{t:>6.2f}{m['clusters']:>7}{m['residual']:>8}"
                      f"{m['dup_after']:>8.1%}{m['purity']:>8.1%}"
                      f"{m['comp']:>9.1%}{m['cross']:>6}")
                best.append((m["dup_after"], m["purity"], name, t, m, tag))

    # 排序：先看重复率够不够低，再看纯度。
    ok = [b for b in best if b[0] <= 0.05 and b[1] >= 0.99]
    print(f"\n达成『重复率 ≤5% 且纯度 ≥99%』的组合：{len(ok)} 个")
    for dup, pur, name, t, m, tag in sorted(ok, key=lambda x: (-x[1], x[0]))[:6]:
        print(f"  {name}({tag}) 阈值 {t:.2f}  重复率 {dup:.1%}  纯度 {pur:.1%}  "
              f"完整度 {m['comp']:.0%}  跨合 {m['cross']}")

    # 真机库：单独报"禁止合并"的那几对有没有被合上
    if args.db:
        print("\n===== 真机库：禁止合并的对是否被合进同簇 =====")
        mn = [(ids[0], ids[1]) for ids, _w in MUST_NOT]
        for name, fn in (("kw_cov", kw_cov), ("cov_strict", cov_strict)):
            for t in (0.34, 0.5, 0.6):
                g = cluster(rows, fn, t, args.gap, False)
                hits = must_not_hits(g, mn)
                rn = [x for x in REDUNDANT]
                print(f"  {name:<11}@{t:.2f}  簇 {len(g):>3}  "
                      f"合并 {n - len(g):>3} 条  误合 {len(hits)} "
                      f"{hits if hits else ''}")
        print("\n===== 真机库：禁止合并的对 =====")
        for ids, why in MUST_NOT:
            a, b = by_id.get(ids[0]), by_id.get(ids[1])
            if not a or not b:
                print(f"  {ids} 缺数据")
                continue
            vals = "  ".join(f"{k}={fn(a, b):.2f}" for k, fn in CRITERIA.items())
            print(f"  {ids[0]}-{ids[1]} {why}")
            print(f"      {vals}")
        print("\n===== 真机库：应合并的簇 =====")
        for ids, why in REDUNDANT:
            print(f"  {ids} {why}")
            for x, y in itertools.combinations(ids, 2):
                a, b = by_id.get(x), by_id.get(y)
                if not a or not b:
                    continue
                print(f"      {x}-{y}  mix={mix(a, b):.2f}")

    if args.pairs:
        print("\n===== 最大的几个簇（kw_cov, 阈值 0.5, complete-linkage）=====")
        for g in sorted(cluster(rows, kw_cov, 0.5, args.gap, True), key=len, reverse=True)[:6]:
            if len(g) < 2:
                continue
            print(f"\n  [{len(g)} 条]")
            for r in g:
                print(f"    {r.id:>3} [{r.truth}] {r.raw[:30]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
