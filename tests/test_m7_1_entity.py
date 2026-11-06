"""M7.1 实体归一。

重点不是"能不能合并"，而是**合并错了能不能发现**。
所以这里的测试有一半在验证"不该合并的时候真的没合并"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hippo.core.entities import (
    AUTO_MERGE, NEW, SUSPECT, decide, normalize_name, similarity, surname_of,
)
from hippo.core.store import Store

CASES = Path(__file__).resolve().parent / "fixtures" / "entity_cases.jsonl"


# ---------------------------------------------------------------- 纯函数
def test_normalize_strips_honorific_and_prefix():
    assert normalize_name("隔壁老王") == "老王"
    assert normalize_name("我们老板") == "老板"
    assert normalize_name("王建国老师") == "王建国"
    assert normalize_name(" 陈　晨 ") == "陈晨"


def test_normalize_keeps_lao_and_xiao():
    # "老"是称呼的一部分，不是修饰语。删了它，"老王"和"王"就混了
    assert normalize_name("老王") == "老王"
    assert normalize_name("小王") == "小王"


def test_normalize_prefix_takes_longest_match_only():
    """去前缀只能认最长的那一个。

    第一版按列表顺序试，"用户的猫"先匹配到"用户的"但由于只剩 1 字而跳过，
    继续试"用户"，得到"的猫"——一个永远匹配不上的实体。
    """
    assert normalize_name("用户妈妈") == "妈妈"
    # 去掉"用户的"只剩"猫"一个字，宁可不处理，也不能产出"的猫"
    assert normalize_name("用户的猫") == "用户的猫"


def test_normalize_does_not_strip_short_suffix():
    # "王老师" 去"老师"只剩一个字，宁可不去
    assert normalize_name("王老师") == "王老师"


def test_surname_of_skips_familiar_prefix():
    assert surname_of("老王") == "王"
    assert surname_of("王建国") == "王"
    assert surname_of("阿May") == "M"


def test_trigram_jaccard_is_weak_on_chinese_names():
    """这条测试是在**记录一个事实**，不是在夸算法。

    中文两字名的 3-gram 带边界符之后只有两个 gram，交集必然为空；
    三字名差一个字只剩 0.2。所以手册里"相似度 >0.8 就合并"这条规则，
    在中文人名上几乎永远不会触发 —— 它不是防线，是装饰。
    """
    assert similarity("老王", "老李") == 0.0
    assert similarity("王建国", "王建军") == pytest.approx(0.2)
    assert similarity("张伟", "张玮") == 0.0
    # 归一化之后相等才会到 1.0
    assert similarity("隔壁老王", "老王") == 1.0


def test_decide_merges_only_on_exact():
    existing = [(7, "老王", "person", ["王哥"])]
    assert decide("王哥", "person", existing).action == AUTO_MERGE
    assert decide("隔壁老王", "person", existing).action == AUTO_MERGE
    # 没给别名，只有"可能相关"，不动手
    assert decide("王建国", "person", existing).action in (NEW, SUSPECT)


def test_decide_uses_incoming_aliases():
    """别名是双向的。

    库里先有"小张"，这一轮模型说"张敏，也叫小张"——
    只看 canonical 会判成两个实体，别名就白给了。
    """
    existing = [(3, "小张", "person", [])]
    m = decide("张敏", "person", existing, aliases=["小张"])
    assert m.action == AUTO_MERGE and m.target_id == 3


def test_decide_refuses_cross_type():
    existing = [(1, "朝阳", "place", [])]
    m = decide("朝阳", "person", existing)
    assert m.action == NEW and m.target_id is None


def test_decide_flags_one_char_off_as_doubt():
    existing = [(1, "王建国", "person", [])]
    m = decide("王建军", "person", existing)
    assert m.action == SUSPECT and m.target_id == 1
    assert m.reason == "one_char_off"


def test_no_prefix_merge():
    """删掉的那条规则要被记住：李娜 / 李娜娜 不自动合并。"""
    existing = [(1, "李娜", "person", [])]
    assert decide("李娜娜", "person", existing).action == NEW


# ---------------------------------------------------------------- Store 层
def test_resolve_creates_then_hits(store):
    a = store.resolve_entity(session="s", name="老王")
    b = store.resolve_entity(session="s", name="王哥", aliases=["老王"])
    assert a.target_id == b.target_id
    assert b.action == AUTO_MERGE


def test_resolve_same_name_two_types(store):
    """006 的核心：同名不同类是两个实体。

    以前 UNIQUE(session, canonical) 只允许一个，"朝阳"先被标成人，
    之后所有"搬到朝阳"都挂到那个人身上，角色卡长出"朝阳是什么性格"。
    """
    p = store.resolve_entity(session="s", name="朝阳", type="place")
    h = store.resolve_entity(session="s", name="朝阳", type="person")
    assert p.target_id != h.target_id
    assert store.get_entity(p.target_id).type == "place"
    assert store.get_entity(h.target_id).type == "person"


def test_resolve_records_doubt_once(store):
    store.resolve_entity(session="s", name="王建国")
    m1 = store.resolve_entity(session="s", name="王建军")
    # 第二次是同一个名字，命中自己刚建的那条 —— 这是对的，
    # 不然每提一次"王建军"就新建一个实体
    m2 = store.resolve_entity(session="s", name="王建军")
    assert m1.action == SUSPECT
    assert m2.action == AUTO_MERGE and m2.target_id == m1.target_id
    qs = [d.question for d in store.active_doubts("s")]
    assert len([q for q in qs if "王建军" in q]) == 1, qs


def test_resolve_doubt_does_not_merge(store):
    a = store.resolve_entity(session="s", name="张伟")
    b = store.resolve_entity(session="s", name="张玮")
    assert a.target_id != b.target_id


# ---------------------------------------------------------------- 合并
def _eid(store, name, type_="person") -> int:
    return store.resolve_entity(session="s", name=name, type=type_).target_id


def test_merge_moves_traits_and_events(store):
    a = _eid(store, "老王")
    b = _eid(store, "王建国")
    ev = store.add_event(session="s", raw="老王来了", entities=[a])
    store.add_trait(entity_id=a, trait="在创业公司做后端", evidence=[ev], confidence=0.6)

    st = store.merge_entities(a, b)
    assert st["traits"] == 1 and st["events"] == 1
    assert store.list_traits(b)[0].trait == "在创业公司做后端"
    # 事件关联要跟着走，否则检索到这条事件时会挂到一个已合并的壳上
    assert store.entities_of_event(ev) == [b]
    assert json.loads(store.conn.execute(
        "SELECT entities FROM events WHERE id=?", (ev,)).fetchone()[0]) == [b]


def test_merge_combines_duplicate_trait_evidence(store):
    a = _eid(store, "老王")
    b = _eid(store, "王建国")
    e1 = store.add_event(session="s", raw="x1", entities=[a])
    e2 = store.add_event(session="s", raw="x2", entities=[b])
    store.add_trait(entity_id=a, trait="做后端", evidence=[e1], confidence=0.6)
    store.add_trait(entity_id=b, trait="做后端", evidence=[e2], confidence=0.6)

    store.merge_entities(a, b)
    ts = store.list_traits(b)
    assert len(ts) == 1
    assert ts[0].evidence == [e1, e2]
    assert ts[0].confidence == pytest.approx(0.7)


def test_merge_refuses_cross_type_and_cross_session(store):
    from hippo.core.store import StoreError

    a = _eid(store, "朝阳", "place")
    b = _eid(store, "朝阳", "person")
    with pytest.raises(StoreError):
        store.merge_entities(a, b)

    c = store.resolve_entity(session="t", name="老王").target_id
    d = _eid(store, "老王")
    with pytest.raises(StoreError):
        store.merge_entities(d, c)


def test_merge_frees_the_old_name(store):
    """合并后原名字必须能重新创建。

    第一版把 src 行的 canonical 原地留着，于是以后出现同一个名字时
    INSERT 撞 UNIQUE(session, canonical, type) —— 报错在三个月后的某次抽取里。
    """
    a = _eid(store, "老王")
    b = _eid(store, "王建国")
    store.merge_entities(a, b)
    again = store.resolve_entity(session="s", name="老王")
    # 因为 canonical 进了 dst 的 aliases，命中 dst 才对
    assert again.target_id == b


# ---------------------------------------------------------------- 迁移守卫
def test_rebuild_migration_keeps_child_rows(tmp_path):
    """006 重建 entities 表时，event_entities 不能掉。

    DROP TABLE 在 foreign_keys=ON 时会顺着 ON DELETE CASCADE 静默清空子表，
    而且事后 foreign_key_check 还是干净的（级联删完是一致的）。
    所以只能靠重建前后的行数比对兜住 —— 这条测试就是那个兜底的哨兵。
    """
    from hippo.core.db import connect
    from hippo.core.migrate import migrate

    db = tmp_path / "v1.db"
    c = connect(db)
    migrate(c, target=1)
    eid = c.execute("INSERT INTO entities(session, canonical) VALUES ('s','老王')").lastrowid
    ev = c.execute(
        "INSERT INTO events(session, event_time, ingest_time, raw, entities)"
        " VALUES ('s',1,1,'老王来了',?)", (f"[{eid}]",)
    ).lastrowid
    migrate(c, target=2)
    assert c.execute("SELECT count(*) FROM event_entities").fetchone()[0] == 1

    migrate(c)   # 6、7
    assert c.execute("SELECT count(*) FROM event_entities").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM events WHERE id=?", (ev,)).fetchone()[0] == 1
    c.close()


# ---------------------------------------------------------------- 评测脚本
def test_eval_cases_pass():
    """跑一遍 fixtures，规则用例必须全过。

    注意：**这些用例是我自己编的**，20/20 只能说明"规则没有明显破洞"。
    真实结论要看 tools/eval_entity.py 打印的相似度分布，以及 M9 的真实对话。
    """
    from tools.eval_entity import load, run_one

    cases = load(CASES)
    easy = [c for c in cases if not c.get("hard")]
    fails = [c["id"] for c in easy if not run_one(c)[0]]
    assert not fails, fails
