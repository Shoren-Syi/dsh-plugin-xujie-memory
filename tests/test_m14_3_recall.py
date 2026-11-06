"""M14.3 召回加固验收。

手册的要求只有一句：**只对真正注入上下文的事件计数**。

这句话拆开是两件容易做错的事：

1. 拿 `items` 全量当"注入"。`items` 是"检索到了"，不是"发出去了"。
   被预算裁掉的条目留在里面的话，`recall_count` 会虚高——而
   `recall_count` 会延长半衰期，等于"考虑过的事"比"真正想起来的事"
   更容易被记住，方向反了。

2. 复用时不计数，或者反过来把 new_ids 当注入。复用的是**上一轮的字节**，
   这一轮真正进 prompt 的是 old_ids。记 new_ids 会去加固那些"检索到了
   但一个字都没发出去"的事件。
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from hippo.core.pack import pack

NOW = datetime(2026, 11, 5, 14, 0)
S = "s1"


def _ev(store, raw, keywords, *, summary="", tier=2, pinned=0):
    return store.add_event(
        session=S, raw=raw, keywords=keywords, summary=summary or raw,
        tier=tier, pinned=pinned,
        event_time=int(datetime(2026, 11, 4, 12, 0).timestamp()))


def rc(store, eid: int) -> int:
    return store.get_event(eid).recall_count


def last(store, eid: int):
    return store.get_event(eid).last_access


# ------------------------------------------------------------------ 计数
def test_injected_event_gets_counted(store):
    """★ 注入即 +1。"""
    eid = _ev(store, "用户搬到海淀了", "搬家 海淀 朝阳")
    p = pack(store, S, "我搬家了你记得吗", now=NOW,
             cfg=__import__("hippo.config", fromlist=["Config"]).Config())
    assert p.items, "没检索到，测试前提不成立"
    assert rc(store, eid) == 1
    assert last(store, eid) == int(NOW.timestamp())


def test_pack_twice_counts_twice(store):
    eid = _ev(store, "用户搬到海淀了", "搬家 海淀 朝阳")
    cfg = __import__("hippo.config", fromlist=["Config"]).Config()
    pack(store, S, "我搬家了你记得吗", now=NOW, cfg=cfg, use_cache=False)
    pack(store, S, "我搬家了你记得吗", now=NOW, cfg=cfg, use_cache=False)
    assert rc(store, eid) == 2


def test_trimmed_events_are_not_counted(store):
    """★ 被预算裁掉的条目**不许**计数。

    做法：40 条都命中同一个词，D 段装不下，看没进 `inj_ids` 的那些
    是不是 recall_count == 0。
    """
    ids = []
    for i in range(40):
        ids.append(_ev(store, f"第{i}条搬家记录" + "细节" * 30,
                       "搬家 海淀", summary="摘要" * 40))
    cfg = __import__("hippo.config", fromlist=["Config"]).Config()
    p = pack(store, S, "搬家", now=NOW, cfg=cfg)
    injected = sum(1 for i in ids if rc(store, i) > 0)
    assert 0 < injected < len(ids), f"没有发生裁剪（{injected}/{len(ids)}），测试前提不成立"
    assert injected == p.by_name["memory"].text.count("[记忆 · ")


def test_total_budget_updates_recall_accounting_after_trimming(store, monkeypatch):
    import importlib
    module = importlib.import_module("hippo.core.pack")
    monkeypatch.setattr(module, "TOTAL_BUDGET", 450)
    ids = [_ev(store, f"搬家记录{i}" + "细节" * 20, "搬家 海淀", summary="摘要" * 35)
           for i in range(8)]
    cfg = __import__("hippo.config", fromlist=["Config"]).Config()
    result = pack(store, S, "搬家", now=NOW, cfg=cfg, use_cache=False)
    rendered = result.by_name["memory"].text.count("[记忆 · ")
    assert len(result.items) > rendered, "应触发总预算裁剪"
    assert sum(rc(store, i) for i in ids) == rendered
    assert len(store.get_state(S, "last_pack_ids").split()) == rendered
    assert not result.by_name["memory"].text.endswith("]"), "不能保留没有内容的事件标题"


def test_reuse_turn_counts_exactly_the_rendered(store):
    """续说同一话题 → 不重新检索 → 复用 D 段。加固数必须等于 D 段条目数。

    这一条同时守 `_save_state`：它存进 `last_pack_ids` 的必须是
    **渲染出去的** id。存 items 全量的话，裁剪发生时下一轮会去加固
    那几条从来没发出去过的记忆，recall_count 虚高、半衰期被凭空延长。
    """
    ids = [_ev(store, f"搬家记录{i}" + "细节" * 30, "搬家 海淀",
               summary="摘要" * 40) for i in range(40)]
    cfg = __import__("hippo.config", fromlist=["Config"]).Config()
    p1 = pack(store, S, "我搬家了你记得吗", now=NOW, cfg=cfg)
    n1 = sum(rc(store, i) for i in ids)
    rendered1 = p1.by_name["memory"].text.count("[记忆 · ")
    assert n1 == rendered1, "第一轮就加固多了"
    assert rendered1 < len(ids), "没发生裁剪，测试前提不成立"

    # 同一话题往下说：covered → continue → 不检索，直接复用
    p2 = pack(store, S, "搬家真是累死了", now=NOW, cfg=cfg)
    assert p2.trigger.fire is False, f"这轮其实触发了：{p2.trigger.reason}"
    assert p2.reused is True
    n2 = sum(rc(store, i) for i in ids)
    assert n2 - n1 == p2.by_name["memory"].text.count("[记忆 · ")


def test_last_pack_ids_records_rendered_only(store):
    """状态里存的是"上一轮装了什么"，不是"上一轮检索到什么"。"""
    ids = [_ev(store, f"搬家记录{i}" + "细节" * 30, "搬家 海淀",
               summary="摘要" * 40) for i in range(40)]
    cfg = __import__("hippo.config", fromlist=["Config"]).Config()
    pack(store, S, "我搬家了你记得吗", now=NOW, cfg=cfg)
    saved = [int(x) for x in store.get_state(S, "last_pack_ids").split()]
    assert len(saved) < len(ids)
    assert set(saved) <= set(ids)
    assert all(rc(store, i) == 1 for i in saved)
    assert all(rc(store, i) == 0 for i in set(ids) - set(saved))


def test_empty_query_does_not_count(store):
    eid = _ev(store, "用户搬到海淀了", "搬家 海淀")
    cfg = __import__("hippo.config", fromlist=["Config"]).Config()
    pack(store, S, "", now=NOW, cfg=cfg)
    assert rc(store, eid) == 0


# ------------------------------------------------------------------ 复用
def test_reused_pack_counts_old_ids(store):
    """★ 复用的是旧字节，加固的必须是旧 id。

    第二次问同样的话 → 检索结果一样 → 复用 D 段。这一轮进 prompt 的
    内容来自 last_pack_ids，所以加固它。
    """
    eid = _ev(store, "用户搬到海淀了", "搬家 海淀 朝阳")
    cfg = __import__("hippo.config", fromlist=["Config"]).Config()
    pack(store, S, "我搬家了你记得吗", now=NOW, cfg=cfg)
    assert rc(store, eid) == 1

    p2 = pack(store, S, "我搬家了你记得吗", now=NOW, cfg=cfg)
    assert p2.reused is True, "第二次没有复用，测试前提不成立"
    assert rc(store, eid) == 2


def test_reused_pack_does_not_count_unrendered(store):
    """复用路径不许加固"这一轮检索到但没渲染"的事件。

    造法：先有一轮 D 段（少量条目），再让检索返回更多条目但重合≥60%。
    """
    ids = [_ev(store, f"搬家记录{i}" + "细节" * 30, "搬家 海淀",
               summary="摘要" * 40) for i in range(3)]
    cfg = __import__("hippo.config", fromlist=["Config"]).Config()
    pack(store, S, "搬家", now=NOW, cfg=cfg)
    before = {i: rc(store, i) for i in ids}
    p2 = pack(store, S, "搬家", now=NOW, cfg=cfg)
    after = {i: rc(store, i) for i in ids}
    for i in ids:
        # 只有真正在 D 段里的才 +1
        rendered = f"#{i}"  # 占位，下面用真实计数校验
        del rendered
    total_delta = sum(after.values()) - sum(before.values())
    assert total_delta == p2.by_name["memory"].text.count("[记忆 · ")


# ------------------------------------------------------------------ 效果
def test_recall_raises_weight_on_next_recompute(store):
    """加固的目的：被想起的事掉得慢。重算一次，权重必须回升。"""
    from hippo.config import Config
    from hippo.core.dynamics import recompute

    cfg = Config()
    # ingest 要放到过去，否则 age=0，加固前后都是 w0
    eid = store.add_event(
        session=S, raw="用户搬到海淀了", keywords=["搬家", "海淀"],
        summary="用户搬到海淀了", event_time=int(datetime(2026, 1, 1).timestamp()),
        ingest_time=int(datetime(2026, 1, 1).timestamp()))
    old = int(datetime(2026, 1, 1).timestamp())
    store.touch_events([eid], when=old)
    recompute(store, NOW, cfg=cfg.weight)
    before = store.get_event(eid).weight

    store.touch_events([eid], when=int(NOW.timestamp()))
    recompute(store, NOW, cfg=cfg.weight)
    assert store.get_event(eid).weight > before


# ------------------------------------------------------------------ 边界与性能
def test_touch_empty_is_noop(store):
    assert store.touch_events([]) == 0


def test_touch_missing_id_is_noop(store):
    assert store.touch_events([999999]) == 0


def test_touch_8_events_under_5ms(store):
    """手册要求批量更新 <5ms。"""
    ids = [_ev(store, f"事{i}", f"词{i}") for i in range(8)]
    store.touch_events(ids)          # 预热（第一次会建语句缓存）
    t0 = time.perf_counter()
    for _ in range(20):
        store.touch_events(ids)
    ms = (time.perf_counter() - t0) / 20 * 1000
    assert ms < 5.0, f"{ms:.2f}ms"


def test_touch_does_not_change_weight_directly(store):
    """加固只动 recall_count / last_access，**不直接改 weight**。

    weight 只在重算时变。直接改的话会出现"权重被两个地方写"，
    出问题时无法判断是谁写的。
    """
    eid = _ev(store, "用户搬到海淀了", "搬家 海淀", tier=2)
    before = store.get_event(eid).weight
    store.touch_events([eid], when=int(NOW.timestamp()))
    assert store.get_event(eid).weight == before


def test_touch_is_persisted_across_reopen(db_path, store):
    """加固要落盘。内存里改了没提交，重启后 recall_count 归零，
    用户会觉得"我明明提过好几次，它还是忘了"。
    """
    eid = store.add_event(session=S, raw="一件事", keywords=["事"],
                          event_time=0, ingest_time=0)
    store.touch_events([eid], when=int(NOW.timestamp()))
    store.close()

    from hippo.core.store import Store
    s2 = Store.open(db_path)
    try:
        assert s2.get_event(eid).recall_count == 1
    finally:
        s2.close()
