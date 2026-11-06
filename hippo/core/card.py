"""角色卡渲染（M7.2 / M7.3）。

角色卡是唯一一处**把推测直接喂给模型**的地方。事件至少有原文可查，
事实有来源事件，而一张"老王｜可能对现状不满"的卡，模型会当成既定前提用，
然后主动关心老王——用户一脸问号，因为那句话是 AI 自己猜的。

所以这里只有三条规则：

1. inferred 永远带前缀。不是"大概""也许"，是明确的「（我的推测）」。
   前缀的作用不是礼貌，是让模型知道这句话**可以被推翻**。
2. confidence < 0.5 默认不注入。低置信度的推测在上下文里就是噪声，
   它会挤掉真正有用的事实，还增加被当成事实的概率。
3. 每一行都能追到事件 id。没有证据的 trait 在 Store 层就被拒了，
   这里只负责把 evidence 显示出来——用户问"你凭什么这么说"时能答。

反过来说，**能注入不等于该注入**：一张 40 条特征的卡会吃掉 800 token，
而其中 30 条这个月都没用上。选择哪些进 prompt 是 M9 检索的事，
这里只提供 `render_traits(..., max_n=)` 让调用方限制条数。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .models import CardTrait, Entity

__all__ = [
    "INFERRED_PREFIX", "MIN_INJECT_CONF", "format_trait", "injectable",
    "render_traits", "render_card",
]

INFERRED_PREFIX = "（我的推测）"
MIN_INJECT_CONF = 0.5


def format_trait(t: CardTrait) -> str:
    """一行特征。inferred 必须带前缀，observed 绝不能带。"""
    text = (t.trait or "").strip()
    if t.status == "inferred":
        return f"{INFERRED_PREFIX}{text}"
    return text


def injectable(t: CardTrait, *, min_conf: float = MIN_INJECT_CONF) -> bool:
    return t.valid_to is None and t.confidence >= min_conf and bool(t.trait.strip())


def _sort_key(t: CardTrait) -> tuple[int, float, int]:
    # observed 在前；同状态下置信度高的在前；同分按 id 稳定排序
    return (0 if t.status == "observed" else 1, -t.confidence, t.id)


def render_traits(
    traits: Iterable[CardTrait],
    *,
    min_conf: float = MIN_INJECT_CONF,
    max_n: int | None = None,
    include_low: bool = False,
) -> list[str]:
    """给 prompt 用的行列表。默认只保留可注入的。"""
    pool = [t for t in traits if t.valid_to is None]
    if not include_low:
        pool = [t for t in pool if injectable(t, min_conf=min_conf)]
    pool.sort(key=_sort_key)
    if max_n is not None:
        pool = pool[:max_n]
    return [format_trait(t) for t in pool]


def render_card(
    e: Entity,
    traits: Sequence[CardTrait],
    *,
    min_conf: float = MIN_INJECT_CONF,
    with_evidence: bool = True,
) -> str:
    """人读的完整视图（`hippo traits` 用）。低置信度也会列出来，但标注未注入。"""
    head = f"{e.canonical} ({e.type}, conf {e.confidence:.2f})"
    if not traits:
        return head + "\n  （没有特征）"
    lines = [head]
    for t in sorted(traits, key=_sort_key):
        mark = ""
        if t.valid_to is not None:
            mark = "  (已失效)"
        elif not injectable(t, min_conf=min_conf):
            mark = "  (未注入)"
        ev = ""
        if with_evidence and t.evidence:
            ev = "  evidence: " + " ".join(f"#{i}" for i in t.evidence)
        conf = "" if t.status == "observed" else f"  conf {t.confidence:.2f}"
        lines.append(f"  [{t.status}] {format_trait(t)}{ev}{conf}{mark}")
    return "\n".join(lines)
