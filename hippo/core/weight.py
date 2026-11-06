"""M14.1 权重公式 —— 记忆会不会老死，全看这个文件。

## 为什么没有照抄手册

手册 §M14.1 给的公式是：

    w = clamp(w0 × decay(hl[tier]) × (1+0.2·rc) × (1+0.3·arousal) × pin)

抄下来十分钟。但 `tools/probe_weight.py` 在真机库上跑 90 天，四条指标
全部爆红（R-14.1 有完整输出）：

| 指标 | 手册公式 | 本文件 |
|---|---|---|
| 权重自己在涨（单调违例） | 1051 次 | 0 |
| 被 clamp 削平 | 16.3% | 0 |
| hot 占比（day90） | 45.5% | 6.8% |
| 30 天前的重要事件还在 top-8 | 1/3 | **3/3** |

三个病根，逐个说：

### 一、自指：hl 按 tier 取，tier 又由 weight 算出来

hot 半衰期 3 天。一条 w0=0.75 的事件：

    day0  hot  → w=0.75
    day1  hot  → decay 0.79 → w=0.72  还是 hot
    day2  hot  → decay 0.63 → w=0.57  掉成 warm
    day3  warm → 半衰期变 30 天 → 用**绝对年龄**重算 → decay 0.93 → w=0.85  又回 hot
    day4  hot  → decay 0.40 → w=0.36  又掉

于是 hot/warm 每两天翻一次，90 天翻 69 次。tier 一抖，M16 的注入深度
就跟着抖——用户会看到"同一条记忆今天给全文、明天给一行、后天又给全文"。

修法不是调参，是**断掉这条边**：tier 只做输出，不做输入。半衰期改由
`w0` 决定，而 `w0` 是写入时定死的常量，永远不会自我引用。

### 二、半衰期的方向反了

手册 `hot=3 / warm=30 / cold=180`，读起来是"工作记忆忘得快、长期记忆
忘得慢"。但 hot 是由"重要"判出来的。**重要的事忘得最快**——这不是人脑，
这是内存回收。

人的记忆是反过来的：吵架、告白、坏消息，二十年都记得；中午吃了什么，
晚上就忘了。所以本文件按 w0 分档，**越重要忘得越慢**：

    w0 ≥ 0.70（重）  → 180 天
    w0 ≥ 0.40（中）  → 30 天
    w0 <  0.40（轻） → 3 天

`w0` 的真实分布是 0.40–0.90（`persist._arousal_tier` 给的），所以第三档
现在几乎用不到。留着是为了以后 w0 的算法改了不至于悄悄退化。

### 三、arousal 收了两遍钱

写入时 `w0 = 0.40 + 0.50 × arousal`，公式里再乘 `(1 + 0.3 × arousal)`。
同一份情绪被计两次，结果是高唤醒事件一开始就顶到 1.0，然后被 clamp
削平几十天——这期间"时间越久越低"在**输出上**不成立（曲线是平顶），
tier 也跟着冻结。

### 四、用进废退：加权重改成延长半衰期

手册 `(1 + 0.2·rc)` 是"每次召回直接给权重加成"。两个问题：
加成会顶棚；而且 `last_access` 刷新**已经**把 decay 重置回 1 了，
再加一次就是双重奖励，马太效应拉满（探针里 top-8 有 7 条并列 1.000，
排序彻底失效）。

改成延长半衰期：

    hl_eff = hl × (1 + 0.25 × min(rc, 5))

"用得多 → 忘得慢"，是持久的性质改变，不是一次性的分数加成。而且
`w = w0 × 0.5^(age/hl_eff) ≤ w0 ≤ 1` 恒成立，**永远不需要 clamp**。

### 五、pinned 的语义是"不忘记"，不是"权重翻倍"

`pin = 2.0` 会让 0.5 的事件跳成 1.0，然后照样衰减——两个月后又冷掉，
只是过程好看一点。钉住应该是"永不衰减"，所以给一个极大的半衰期
（默认 36500 天 = 100 年）。要撤销钉住，把 `pinned` 置 0 就行，
权重会从那一刻按正常曲线重新算。

## 单调性

`compute_weight` 对 `now` 严格不增（同一个 last_access 下）。这条是
测试 `test_monotonic_decay` 的前提，也是"记忆会老"这句话的数学形式。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

__all__ = [
    "DEFAULT_W0_BANDS", "compute_weight", "half_life_of", "tier_of",
    "explain", "now_ts",
]

DAY = 86400.0

# (w0 下界, 半衰期天数)。从高到低匹配，第一个命中的生效。
DEFAULT_W0_BANDS: tuple[tuple[float, float], ...] = (
    (0.70, 180.0),
    (0.40, 30.0),
    (0.00, 3.0),
)

DEFAULT_PINNED_HL = 36500.0     # 100 年，等于"不衰减"
DEFAULT_RECALL_GAIN = 0.25      # 召回 5 次，半衰期 ×2.25
DEFAULT_RECALL_CAP = 5

DEFAULT_HOT = 0.70
DEFAULT_WARM = 0.30
DEFAULT_HYST = 0.05


def now_ts(now: datetime | int | float | None) -> float:
    """把 datetime / int / float 统一成秒级时间戳。"""
    if now is None:
        return datetime.now().timestamp()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def _cfg(decay_cfg: Any) -> dict[str, Any]:
    """从 Config 的 weight 段取参数，缺什么用什么默认。

    全部走 getattr 而不是 `cfg.weight.xxx`：测试里经常只传一个
    `SimpleNamespace()`，写死属性名会让这些测试全炸，而且炸在
    "跟被测逻辑无关的地方"。
    """
    c = decay_cfg
    get = (lambda k, d: getattr(c, k, d) if c is not None else d)

    bands = get("half_life_by_w0", None)
    if bands:
        # yaml 里是 [[0.7, 180], [0.4, 30], [0, 3]]，元素可能是 list
        bands = tuple((float(b[0]), float(b[1])) for b in bands)
    else:
        bands = DEFAULT_W0_BANDS
    return {
        "bands": bands,
        "pinned_hl": float(get("pinned_half_life", DEFAULT_PINNED_HL)),
        "recall_gain": float(get("recall_hl_gain", DEFAULT_RECALL_GAIN)),
        "recall_cap": int(get("recall_cap", DEFAULT_RECALL_CAP)),
        "hot": float(get("hot", DEFAULT_HOT)),
        "warm": float(get("warm", DEFAULT_WARM)),
        "hyst": float(get("hysteresis", DEFAULT_HYST)),
    }


def half_life_of(w0: float, *, pinned: bool = False, recall: int = 0,
                 cfg: Any = None) -> float:
    """这条记忆的半衰期（天）。

    分档看 `w0`，不看 `tier`——tier 是 weight 算出来的，拿它当输入就是
    自指（见模块 docstring 第一节）。`w0` 从写入那天起就不再变。
    """
    p = _cfg(cfg)
    if pinned:
        return p["pinned_hl"]
    base = p["bands"][-1][1]
    for lo, hl in p["bands"]:
        if w0 >= lo:
            base = hl
            break
    used = min(max(int(recall), 0), p["recall_cap"])
    return base * (1.0 + p["recall_gain"] * used)


def compute_weight(event: Any, now: datetime | int | float | None,
                   *, cfg: Any = None) -> float:
    """`event` 在 `now` 这一刻的权重。纯函数：同样的输入永远同样的输出。

    时间基准是 `max(last_access, ingest_time)`，**不是 `event_time`**。

    这条很重要：真机库里 id=20 的事件 `event_time` 是 2023-12-31，而它是
    今天才知道的（"前年买的房"）。按 event_time 算，它一进来就该老死；
    但我们今天才听说，它对我们来说是新的。衰减量的是**"多久没想起来"**，
    不是"事情多久以前发生的"。后者是 `rank._recency` 的活，两件事分开算。

    `now` 必须由调用方传入。函数里不许出现 `datetime.now()`——否则
    "90 天后会怎样"只能靠改系统时钟来测。
    """
    p = _cfg(cfg)
    t = now_ts(now)

    last = getattr(event, "last_access", None) or 0
    ingest = float(getattr(event, "ingest_time", 0) or 0)
    base = max(float(last), ingest)

    # 未来时间（抽取器把"下周三"记成了事件时间，而 last_access 又可能被
    # 一个未来的时钟写成）→ 当作"刚刚"，不产生负衰减。负数会让权重超过
    # w0，而 w0 已经是上限语义。
    age_days = max(0.0, (t - base) / DAY)

    w0 = _clamp01(float(getattr(event, "w0", 0.5) or 0.0))
    hl = half_life_of(w0, pinned=bool(getattr(event, "pinned", 0)),
                      recall=int(getattr(event, "recall_count", 0) or 0), cfg=cfg)
    return _clamp01(w0 * (0.5 ** (age_days / hl)))


def tier_of(weight: float, *, pinned: bool = False, prev: int | None = None,
            cfg: Any = None) -> int:
    """由 weight 派生 tier。**tier 只做输出。**

    带滞回：升 hot 要 ≥0.70，掉出 hot 要 <0.65；掉 cold 要 <0.30，
    出 cold 要 ≥0.35。

    没有滞回的话，权重正好落在 0.70 上的事件会每天翻一次层——探针里
    那条 `w0=0.70` 的事件 90 天翻了 30 次。滞回带的代价是"层级切换
    滞后 0.05"，换来的是层级稳定，划算。
    """
    p = _cfg(cfg)
    if pinned:
        return 1
    # 两个边界的滞回方向是**相反的**，这一点很容易写反：
    #   hot 边界：已经在 hot 里 → 要跌破 0.65 才出去（门往下挪）
    #   cold 边界：已经在 cold 里 → 要涨过 0.35 才出来（门往上挪）
    # 写反的后果是"cold 更容易出、hot 更容易掉"，层级会变得比不用滞回还飘。
    hi = p["hot"] - (p["hyst"] if prev == 1 else 0.0)
    lo = p["warm"] + (p["hyst"] if prev == 3 else 0.0)
    if weight >= hi:
        return 1
    return 2 if weight >= lo else 3


def explain(event: Any, now: datetime | int | float | None,
            *, cfg: Any = None) -> dict[str, Any]:
    """`hippo why` 用：把公式拆开，让人看见每个因子各占多少。"""
    p = _cfg(cfg)
    t = now_ts(now)
    last = getattr(event, "last_access", None) or 0
    ingest = float(getattr(event, "ingest_time", 0) or 0)
    base = max(float(last), ingest)
    age_days = max(0.0, (t - base) / DAY)
    w0 = float(getattr(event, "w0", 0.5) or 0.0)
    pinned = bool(getattr(event, "pinned", 0))
    recall = int(getattr(event, "recall_count", 0) or 0)
    base_hl = half_life_of(w0, cfg=cfg)
    hl = half_life_of(w0, pinned=pinned, recall=recall, cfg=cfg)
    return {
        "w0": round(w0, 4),
        "age_days": round(age_days, 1),
        "base_half_life": base_hl,
        "half_life": round(hl, 1),
        "recall": recall,
        "pinned": pinned,
        "decay": round(0.5 ** (age_days / hl), 4),
        "weight": round(compute_weight(event, t, cfg=cfg), 4),
        # 带上 prev：滞回是"从当前层出发"的判定，不带 prev 会算出跟
        # recompute 不一样的 tier——CLI 显示 hot、库里还是 warm，这种
        # 不一致排查起来非常费劲。
        "tier": tier_of(compute_weight(event, t, cfg=cfg), pinned=pinned,
                        prev=int(getattr(event, "tier", 0) or 0) or None, cfg=cfg),
    }


def _clamp01(x: float) -> float:
    if x != x:          # NaN：w0 是 NULL 时见过一次
        return 0.0
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)
