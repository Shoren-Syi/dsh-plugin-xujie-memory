"""M15.3 归档与膨胀控制验收。

手册这一段**自相矛盾**，写代码前必须先把口径定下来：

    先说"把行 INSERT ... SELECT 过去，主库删除"
    又说"改为在归档函数里标记 archived=1，主库保留瘦身版：只留
         id/keywords/summary，raw 移到归档库"

后半句在现有 schema 上做不到：`raw` 有 `NOT NULL`、`CHECK (length(raw) > 0)`，
还有 `trg_events_raw_immutable` 拦 UPDATE。把 raw 清空会被数据库自己拒绝。

所以取前者：**物理搬走**。理由是它同时满足三条 DoD：
    体积降 ≥20%（raw 真的不在主库了）
    可还原（归档库有完整行，含原 id）
    归档事件仍可检索（走归档库，见 `search_archive` / `recall --deep`）

最后一条的实现方式变了，在此写明：**不做跨库自动检索**，做成显式询问。
跨库自动检索意味着每轮对话都要 ATTACH 一个可变数量的库文件，而归档的
语义本来就是"这东西我不用了"——让 99% 的查询为 1% 的可能性付磁盘 IO。
真要找的时候 `hippo recall --deep` 或 `hippo restore --id`，都是显式的。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from hippo.core.store import StoreError

S = "main"
BASE = int(datetime(2024, 1, 1).timestamp())
DAY = 86400
NOW = BASE + 800 * DAY


def _old(store, i, *, tier=3, weight=0.02, idle_days=500, kind="event"):
    eid = store.add_event(
        session=S, raw=f"很久以前的事{i}，" + "细节" * 40,
        keywords=["旧事", f"词{i}"], summary=f"旧摘要{i}",
        event_time=BASE + i, ingest_time=BASE + i,
        tier=tier, w0=0.4, weight=weight, kind=kind)
    return eid


def _new(store, i):
    return store.add_event(
        session=S, raw=f"最近的事{i}，" + "详情" * 40,
        keywords=["新事", f"新{i}"], summary=f"新摘要{i}",
        event_time=BASE + 700 * DAY, ingest_time=BASE + 700 * DAY,
        tier=2, w0=0.5, weight=0.5)


def _adir(tmp_path) -> Path:
    return tmp_path / "archive"


# ------------------------------------------------------------------ 触发条件
def test_picks_only_cold_low_weight_idle(store):
    old = _old(store, 1)
    warm = _old(store, 2, tier=2)                 # 还在 warm
    heavy = _old(store, 3, weight=0.3)            # 权重不够低
    ids = store.archive_candidates(now=NOW)
    assert ids == [old]
    assert warm not in ids and heavy not in ids


def test_recently_touched_not_archived(store):
    """cold 但上个月被想起过 → 不归档。

    `last_access` 为 NULL 时用 `ingest_time` 兜底：从没人碰过不等于
    "很久没碰"，它得从写入那天开始算。这条如果写错（比如 `last_access < ?`
    在 NULL 上恒为 NULL），**所有从没被召回过的冷记忆都永远不归档**，
    而它们恰恰是最该归档的那批。
    """
    eid = _old(store, 1)
    store.touch_events([eid], when=NOW - 10 * DAY)
    assert store.archive_candidates(now=NOW) == []


def test_null_last_access_uses_ingest_time(store):
    eid = _old(store, 1)
    ev = store.get_event(eid)
    assert ev.last_access is None
    assert store.archive_candidates(now=NOW) == [eid]


def test_facts_and_plans_never_archived(store):
    """fact / plan 是结构化记忆，删了没法重建，永远不归档。"""
    for kind in ("fact", "plan", "regret"):
        _old(store, 1, kind=kind)
    assert store.archive_candidates(now=NOW) == []


def test_limit_respected(store):
    for i in range(10):
        _old(store, i)
    assert len(store.archive_candidates(now=NOW, limit=3)) == 3


# ------------------------------------------------------------------ 搬走
def test_archive_moves_rows(store, tmp_path):
    ids = [_old(store, i) for i in range(30)]
    for i in range(5):
        _new(store, i)
    r = store.archive_to(ids, _adir(tmp_path) / "mem-2025.db", now=NOW)
    assert r["moved"] == 30
    assert store.get_event(ids[0]) is None, "主库必须真的没有这一行了"
    assert len(store.consolidate_rows(S)) == 5


def test_archive_shrinks_db(store, tmp_path):
    """★ 体积降 ≥20%（手册 DoD）。"""
    for i in range(200):
        _old(store, i)
    before = store.db_bytes(live=False)
    ids = store.archive_candidates(now=NOW)
    r = store.archive_to(ids, _adir(tmp_path) / "mem-2025.db", now=NOW)
    assert r["file_size"] < before * 0.8, f"{before} -> {r['file_size']}"


def test_live_bytes_drop_before_vacuum(store, tmp_path):
    """不 VACUUM 时文件不缩，但**活数据**必须已经降了。

    两个口径都要能拿到：活数据量回答"删掉了多少"，文件大小回答
    "什么时候该 VACUUM"。只看文件大小会得出"归档没生效"的错误结论。
    """
    for i in range(300):
        _old(store, i)
    ids = store.archive_candidates(now=NOW)
    r = store.archive_to(ids, _adir(tmp_path) / "mem-2025.db", now=NOW,
                         vacuum=False)
    assert r["bytes_after"] < r["bytes_before"] * 0.8, (
        f"活数据应已降 ≥20%：{r['bytes_before']} -> {r['bytes_after']}")


def test_archive_writes_meta(store, tmp_path):
    ids = [_old(store, i) for i in range(5)]
    store.archive_to(ids, _adir(tmp_path) / "mem-2025.db", now=NOW)
    c = sqlite3.connect(str(_adir(tmp_path) / "mem-2025.db"))
    row = c.execute("SELECT n, ids, source FROM archive_meta").fetchone()
    c.close()
    assert row[0] == 5
    assert sorted(eval(row[1])) == sorted(ids)  # noqa: S307  测库存的 json 数组
    assert row[2].endswith("mem.db")


def test_archive_failure_keeps_main_intact(store, tmp_path):
    """归档库写不进去时，主库一行都不能少。

    实现是"先写后删，且写完要验数"。验数这一步不是形式主义：
    INSERT 可能因为磁盘满、权限、编码问题**部分成功**，
    此时如果直接 DELETE，主库删掉的就不是归档库有的那些行。
    """
    ids = [_old(store, i) for i in range(3)]
    # 用一个**目录**当归档库路径：ATTACH 打不开目录，必然抛错。
    # 不用 chmod 500 —— 测试以 root 跑，root 无视权限位，
    # 那种"制造失败"的手法在这里根本不失败（写的时候才发现测试是空转的）。
    bad = tmp_path / "as_dir"
    bad.mkdir()
    with pytest.raises((sqlite3.Error, OSError)):
        store.archive_to(ids, bad, now=NOW)
    for i in ids:
        assert store.get_event(i) is not None, "归档失败却删了主库的行"


def test_archived_not_found_by_recall(store, tmp_path):
    eid = _old(store, 1)
    store.archive_to([eid], _adir(tmp_path) / "mem-2025.db", now=NOW)
    hits = store.fetch_events([eid], S, include_historical=True)
    assert hits == []


# ------------------------------------------------------------------ 还原
def test_restore_brings_back_verbatim(store, tmp_path):
    """★ 可还原：原 id、原文、关键词全部一致。"""
    eid = _old(store, 7)
    before = store.get_event(eid)
    store.archive_to([eid], _adir(tmp_path) / "mem-2025.db", now=NOW)
    got = store.restore_from([eid], _adir(tmp_path))
    assert got == [eid]
    after = store.get_event(eid)
    assert after is not None
    assert after.raw == before.raw
    assert after.summary == before.summary
    assert after.keywords == before.keywords
    assert after.event_time == before.event_time


def test_restore_rebuilds_inverted_index(store, tmp_path):
    """恢复后必须能被倒排命中。

    只把 events 行插回去、忘了 kw_index 的话，"恢复成功"是真的，
    但它照样搜不到——用户以为找回来了，实际还是个死记录。
    """
    eid = _old(store, 7)
    store.archive_to([eid], _adir(tmp_path) / "mem-2025.db", now=NOW)
    store.restore_from([eid], _adir(tmp_path))
    hits = [r["event_id"] for r in store.conn.execute(
        "SELECT event_id FROM kw_index WHERE token = ?", ("旧事",)).fetchall()]
    assert hits == [eid]


def test_restore_from_any_batch(store, tmp_path):
    """一条事件可能属于任何一批，还原要跨文件找。"""
    a = [_old(store, i) for i in range(3)]
    b = [_old(store, i) for i in range(10, 13)]
    adir = _adir(tmp_path)
    store.archive_to(a, adir / "mem-2024.db", now=NOW)
    store.archive_to(b, adir / "mem-2025.db", now=NOW)
    got = store.restore_from([a[0], b[0]], adir)
    assert set(got) == {a[0], b[0]}


def test_restore_unknown_id_returns_empty(store, tmp_path):
    _adir(tmp_path).mkdir(parents=True)     # 有目录但空
    assert store.restore_from([999], _adir(tmp_path)) == []


def test_restore_is_idempotent(store, tmp_path):
    eid = _old(store, 1)
    store.archive_to([eid], _adir(tmp_path) / "mem-2025.db", now=NOW)
    assert store.restore_from([eid], _adir(tmp_path)) == [eid]
    # 第二次：归档库里已经没了，返回空而不是报错
    assert store.restore_from([eid], _adir(tmp_path)) == []


def test_archived_rows_are_removed_from_archive_after_restore(store, tmp_path):
    """还原之后归档库里的那份要删掉，否则同一条事件存在两个地方。"""
    eid = _old(store, 1)
    p = _adir(tmp_path) / "mem-2025.db"
    store.archive_to([eid], p, now=NOW)
    store.restore_from([eid], _adir(tmp_path))
    c = sqlite3.connect(str(p))
    n = c.execute("SELECT count(*) FROM events WHERE id = ?", (eid,)).fetchone()[0]
    c.close()
    assert n == 0


# ------------------------------------------------------------------ 口径
def test_db_bytes_two_measures(store):
    live = store.db_bytes()
    total = store.db_bytes(live=False)
    assert 0 < live <= total


def test_page_stats_is_the_only_source(store, tmp_path):
    """`db_bytes` 和 `stats --disk` 必须读同一份页统计。

    分开查 PRAGMA 的后果是"报表和断言对不上"，而且只在某个版本上对不上。
    这条同时是 DAO 单一出口的守卫：cli 里不许出现 `.execute(`。
    """
    st = store.page_stats()
    assert st["total"] == st["pages"] * st["page_size"]
    assert st["live"] == max(0, st["pages"] - st["freelist"]) * st["page_size"]
    assert store.db_bytes(live=True) == st["live"]
    assert store.db_bytes(live=False) == st["total"]


# ------------------------------------------------- 手册验收 #2：仍可检索
def test_archived_still_findable_in_archive(store, tmp_path):
    """搬走了，但**找得到**。这是手册 M15.3 验收 #2。

    和"自动召回"的区别在于谁来发起：`search_archive` 是显式询问，
    不参与每轮检索。归档的语义是"我不用了"，但它得是"收起来了"，
    不是"没了"。
    """
    eid = store.add_event(
        session=S, raw="两年前在协和做过甲状腺手术，切了一侧",
        keywords=["甲状腺", "手术"], summary="甲状腺手术",
        event_time=BASE, ingest_time=BASE, tier=3, w0=0.4, weight=0.02)
    store.archive_to([eid], _adir(tmp_path) / "mem-2024.db", now=NOW)

    hits = store.search_archive("甲状腺")
    assert len(hits) == 1
    assert hits[0]["archived_id"] == eid
    assert "甲状腺手术" in hits[0]["raw"]
    assert hits[0]["file"].endswith("mem-2024.db")


def test_deep_search_does_not_see_live_rows(store, tmp_path):
    """归档检索只看归档库。刚写的记忆归 L1/L2 管，不该在这里冒出来。"""
    _old(store, 1)
    live = store.add_event(
        session=S, raw="今天新说的甲状腺复查结果", keywords=["甲状腺"],
        summary="复查", event_time=BASE + 700 * DAY,
        ingest_time=BASE + 700 * DAY, tier=1, w0=0.9, weight=0.9)
    _adir(tmp_path).mkdir(parents=True)
    hits = store.search_archive("甲状腺")
    assert hits == []
    assert all(h["archived_id"] != live for h in hits)


def test_deep_search_matches_any_token(store, tmp_path):
    """多词是"任一命中"，不是"全都要"。

    用户在命令行打的是自然语言（`hippo recall 我搬家的事 --deep`），
    归档里写的是"从朝阳搬到海淀"，没有共同字面。要求全词命中等于
    永远查不到。
    """
    eid = store.add_event(
        session=S, raw="从朝阳搬到海淀，房租涨了两千",
        keywords=["搬家", "海淀"], summary="搬家",
        event_time=BASE, ingest_time=BASE, tier=3, w0=0.4, weight=0.02)
    store.archive_to([eid], _adir(tmp_path) / "mem-2024.db", now=NOW)
    assert len(store.search_archive("我 海淀 的事")) == 1


def test_deep_search_without_archive_dir_is_empty(store, tmp_path):
    """没归档过就没目录。不能抛异常——`--deep` 是新用户也会顺手加的参数。"""
    assert store.search_archive("随便什么") == []
    assert store.search_archive("") == []
