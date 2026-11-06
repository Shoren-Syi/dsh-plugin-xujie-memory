"""M8.1 验收：facts 写入 —— key 闭集、值归一、同值幂等、异值留痕。

这一节的重点不是"能写进去"，而是**什么不该写进去**：
表外的 key、句子级别的值、把粒度差异当成搬家。
三条里任何一条失守，后面 M8.2 的矛盾检测都会变成"每换一种说法就报一次"，
而指标（检出率）反而更高——这正是最难发现的那种坏。
"""

from __future__ import annotations

import sqlite3

import pytest

from hippo.core.facts import (
    decide_fact, explain, facets, fold_value, load_keys, more_specific, same_value,
)


# ---------------------------------------------------------------- key 闭集
def test_key_alias_maps_to_canonical():
    kt = load_keys()
    for raw in ("居住城市", "居住地", "city", "现居", "住在哪"):
        assert kt.canonical(raw) == "city", raw
    for raw in ("工作单位", "公司", "employer", "在哪上班"):
        assert kt.canonical(raw) == "employer", raw


def test_key_exact_wins_over_containment():
    """`工作单位` 不能被"包含 `工作`"抢去 occupation。

    包含匹配是个兜底，不能抢精确匹配的活。顺序反了，
    所有带"工作"的 key 都会落到 occupation 上。
    """
    kt = load_keys()
    assert kt.canonical("工作单位") == "employer"
    assert kt.canonical("工作") == "occupation"


def test_key_containment_picks_longest_alias():
    kt = load_keys()
    assert kt.canonical("房贷剩余年限") == "mortgage"
    assert kt.canonical("用户的居住城市") == "city"


def test_unknown_key_rejected():
    kt = load_keys()
    for raw in ("银行卡密码", "今天心情", "", "   "):
        assert kt.canonical(raw) is None, raw


def test_key_table_is_bounded():
    """手册要求实际使用 key 数 ≤40。表本身超了就说明"闭集"名存实亡。"""
    kt = load_keys()
    assert 20 <= len(kt) <= 40, len(kt)
    assert all(k.card in ("single", "multi") for k in kt.keys.values())
    assert all(k.type in ("place", "org", "enum", "number", "date", "text")
               for k in kt.keys.values())


# ---------------------------------------------------------------- 值归一
@pytest.mark.parametrize("type_,a,b", [
    ("place", "上海", "上海市"),
    ("place", "上海", "沪"),
    ("place", "海淀", "海淀区"),
    ("org", "字节", "字节跳动"),
    ("org", "字节跳动有限公司", "字节"),
    ("org", "清华", "清华大学"),
    ("number", "三岁", "3岁"),
    ("number", "二十年", "20年"),
])
def test_same_value_surface_variants(type_, a, b):
    assert same_value(type_, a, b), explain(type_, a, b)


@pytest.mark.parametrize("type_,a,b", [
    ("place", "北京", "上海"),
    ("org", "字节", "腾讯"),
    ("text", "张伟", "张伟明"),      # 差一个字是两个人，不许合并
    ("text", "后端", "服务端"),      # 语义相同但字面不同，规则层不该猜
])
def test_different_value_not_merged(type_, a, b):
    assert not same_value(type_, a, b), explain(type_, a, b)


def test_more_specific_place_and_org():
    assert more_specific("place", "上海市浦东新区", "上海")
    # 机构名的粒度差异多在分支/分部上（`字节` / `字节上海分公司`）。
    # `字节跳动` vs `字节` 不属于这一类：它归一到同值，走 same 那条路。
    assert more_specific("org", "字节上海分公司", "字节")
    assert same_value("org", "字节跳动", "字节")
    # facets：括号里的是"更细的说法"，不是另一个值
    assert facets("place", "北京（海淀）") == ["北京", "海淀"]
    assert same_value("place", "北京（海淀）", "海淀")


def test_more_specific_does_not_swallow_other_entities():
    """`北京银行` 不是 `北京` 的细化，是另一家机构。

    前缀包含是最容易过拟合的规则：`字节 ⊂ 字节跳动` 对，
    `北京 ⊂ 北京银行` 错。加一条通名守卫，把后者挡在门外。
    """
    assert not more_specific("org", "北京银行", "北京")
    assert not more_specific("text", "王建国", "王建")


def test_fold_does_not_truncate_person_name():
    assert fold_value("text", "张伟") == "张伟"
    assert fold_value("text", "张伟明") == "张伟明"


# ---------------------------------------------------------------- 写入语义
def test_same_value_is_idempotent(store):
    w1 = store.set_fact(session="s", key="居住城市", value="上海")
    w2 = store.set_fact(session="s", key="居住城市", value="上海市")
    assert w1.action == "insert" and w1.id is not None
    assert w2.action == "same" and w2.id == w1.id
    assert len(store.get_fact("s", "city")) == 1


def test_different_value_closes_old(store):
    store.set_fact(session="s", key="city", value="北京", when=1000, confidence=0.9)
    w = store.set_fact(session="s", key="city", value="上海", when=2000, confidence=0.9)
    assert w.action == "replace"
    active = store.get_fact("s", "city")
    assert [(f.value, f.valid_from) for f in active] == [("上海", 2000)]
    old = [f for f in store.get_fact("s", "city", include_historical=True)
           if f.value == "北京"][0]
    assert old.valid_to == 2000


def test_refine_is_not_a_move(store):
    """先说北京、后说海淀，不算搬家。

    走 replace 的话，`valid_from` 会变成"说海淀的那天"，
    于是"你是什么时候搬的家"会答成"上周三"——用户根本没搬。
    """
    store.set_fact(session="s", key="city", value="北京", when=1000, confidence=0.9)
    w = store.set_fact(session="s", key="city", value="北京（海淀）", when=2000, confidence=0.9)
    assert w.action in ("same", "refine")
    active = store.get_fact("s", "city")
    assert len(active) == 1
    assert active[0].value == "北京（海淀）"
    assert active[0].valid_from == 1000          # 仍然是第一次说"北京"的时间
    assert active[0].valid_to is None


def test_multi_key_coexists(store):
    for v in ("不吃辣", "不吃香菜", "不喝冰的"):
        assert store.set_fact(session="s", key="饮食忌口", value=v).action == "insert"
    vals = [f.value for f in store.get_fact("s", "diet")]
    assert len(vals) == 3


def test_multi_key_dedup(store):
    store.set_fact(session="s", key="diet", value="不吃辣")
    store.set_fact(session="s", key="diet", value="不吃辣 ")
    assert len(store.get_fact("s", "diet")) == 1


def test_unknown_key_not_written(store):
    w = store.set_fact(session="s", key="银行卡密码", value="1234")
    assert w.action == "reject" and w.id is None
    assert store.counts()["facts"] == 0


def test_low_confidence_writes_doubt_not_fact(store):
    store.set_fact(session="s", key="city", value="北京", confidence=0.9)
    w = store.set_fact(session="s", key="city", value="广州", confidence=0.4)
    assert w.action == "doubt" and w.id is None
    assert [f.value for f in store.get_fact("s", "city")] == ["北京"]
    qs = [d.question for d in store.active_doubts("s")]
    assert any("广州" in q for q in qs), qs


def test_entity_scoped(store):
    """"用户的城市"和"老王的城市"是两个槽，不能互相顶掉。"""
    u = store.resolve_entity(session="s", name="用户").target_id
    o = store.resolve_entity(session="s", name="老王").target_id
    store.set_fact(session="s", key="city", value="上海", entity_id=u)
    store.set_fact(session="s", key="city", value="深圳", entity_id=o)
    assert [f.value for f in store.get_fact("s", "city", entity_id=u)] == ["上海"]
    assert [f.value for f in store.get_fact("s", "city", entity_id=o)] == ["深圳"]


def test_db_rejects_second_active_single(store):
    """唯一 active 必须是**数据库**保证的，不是代码自觉。

    这里绕过 set_fact 直接 INSERT：如果约束只在 Python 里，
    任何一次手写 SQL、任何一次批量修复都会悄悄制造两条 current。
    """
    store.set_fact(session="s", key="city", value="上海", entity_id=None)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO facts(session, entity_id, key, value, card, valid_from)"
            " VALUES ('s', NULL, 'city', '北京', 'single', 1)"
        )


def test_db_allows_second_active_multi(store):
    store.set_fact(session="s", key="diet", value="不吃辣")
    store.conn.execute(
        "INSERT INTO facts(session, entity_id, key, value, card, valid_from)"
        " VALUES ('s', NULL, 'diet', '不吃香菜', 'multi', 1)"
    )
    assert len(store.get_fact("s", "diet")) == 2


def test_decide_fact_is_pure():
    """判定逻辑不依赖数据库——这样它能被穷举测试，而不是靠开库试。"""
    kt = load_keys()
    city = kt.get("city")
    d = decide_fact(city, [(7, "北京")], "上海", 0.9)
    assert (d.action, d.close_ids) == ("replace", (7,))
    d = decide_fact(city, [(7, "北京")], "北京", 0.9)
    assert (d.action, d.keep_id) == ("same", 7)
    d = decide_fact(city, [], "北京", 0.9)
    assert d.action == "insert"
