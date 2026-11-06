"""M14.0 开工前的工地整理：C 段事实必须带人。

这一组测试来自一次**误判**。M13 收尾时在真机库看到 prompt 里有

    已知：… commute=飞机 …

而用户自己的通勤是步行。当时的结论是"facts 表里同一个 key 有 3 条 active，
M8 写坏了"，并据此在 R-13-Gate 里给 M14 派了一个活：给 facts 加 entity 列。

M14 开工去库里查，发现：

    entity_id=3  老王  commute=飞机
    entity_id=5  用户  commute=步行
    entity_id=22 小李  commute=高铁

**数据完全正确，三条分属三个人，每人只有一条 active。**
错的是 `_pinned`：它按 `f.key` 去重，`who` 拿到了却没用。

所以 M14 不需要给 facts 加 entity——它早就有。需要的是：
1. 去重键改成 (人, 槽)
2. 输出带人名，否则合并之后读者不知道是谁的
3. multi 槽（diet）多值全留，重复值去掉

留这组测试，是为了让"症状在渲染层、却去查数据层"这个错误不再重犯：
下一个人看到 `commute=飞机` 挂在用户头上，先怀疑 `_pinned`。
"""

from __future__ import annotations

from hippo.core.pack import _pinned
from hippo.core.turns import estimate_tokens

S = "main"


def _facts(store, session: str = S) -> str:
    return _pinned(store, session)[0]


def test_same_key_three_people(store):
    """三个人都有 commute，必须各归各。"""
    u = store.upsert_entity(session=S, canonical="用户")
    wang = store.upsert_entity(session=S, canonical="老王")
    li = store.upsert_entity(session=S, canonical="小李")
    store.set_fact(session=S, key="commute", value="步行", entity_id=u, when=100)
    store.set_fact(session=S, key="commute", value="飞机", entity_id=wang, when=200)
    store.set_fact(session=S, key="commute", value="高铁", entity_id=li, when=300)

    text = _facts(store)
    assert "- 用户：commute=步行" in text
    assert "- 老王：commute=飞机" in text
    assert "- 小李：commute=高铁" in text
    # 关键反例：任何一行里都不许出现两个人的通勤方式拼在一起
    assert "commute=飞机" in text and "commute=步行" in text
    for line in text.splitlines():
        if line.startswith("- "):
            assert line.count("commute=") <= 1


def test_person_line_before_others(store):
    """用户自己排第一，其余按名字。顺序固定才谈得上字节稳定。"""
    u = store.upsert_entity(session=S, canonical="用户")
    for n in ("老张", "老王", "小李"):
        e = store.upsert_entity(session=S, canonical=n)
        store.set_fact(session=S, key="commute", value="地铁", entity_id=e, when=100)
    store.set_fact(session=S, key="city", value="北京", entity_id=u, when=100)

    rows = [ln for ln in _facts(store).splitlines() if ln.startswith("- ")]
    assert rows[0].startswith("- 用户：")
    assert [r.split("：")[0] for r in rows[1:]] == ["- 小李", "- 老张", "- 老王"]


def test_multi_slot_keeps_all_values(store):
    """diet 是 multi 槽：不吃辣 + 不吃香菜是两件事，不能压成一件。

    顺序按 `valid_from` 降序，最新说的在前——"我最近说过的忌口"比
    "三年前说过的那条"更该被模型先看到。
    """
    u = store.upsert_entity(session=S, canonical="用户")
    store.set_fact(session=S, key="diet", value="不吃辣", entity_id=u, when=100)
    store.set_fact(session=S, key="diet", value="不吃香菜", entity_id=u, when=200)

    text = _facts(store)
    assert "diet=不吃香菜、不吃辣" in text


def test_multi_slot_dedupes_identical_values(store):
    """真机库里两条"不吃辣"来自不同事件。重复值不进 prompt：正确但白占 token。"""
    u = store.upsert_entity(session=S, canonical="用户")
    store.set_fact(session=S, key="diet", value="不吃辣", entity_id=u, when=100)
    store.set_fact(session=S, key="diet", value="不吃辣", entity_id=u, when=200)
    assert "不吃辣、不吃辣" not in _facts(store)


def test_single_slot_takes_latest(store):
    """单值槽以最后一次说的为准。"""
    u = store.upsert_entity(session=S, canonical="用户")
    store.set_fact(session=S, key="city", value="朝阳", entity_id=u, when=100)
    store.set_fact(session=S, key="city", value="海淀", entity_id=u, when=200)
    text = _facts(store)
    assert "city=海淀" in text
    assert "city=朝阳" not in text


def test_fact_without_entity_counts_as_self(store):
    """没挂实体的事实就是关于用户的（作息、忌口这类）。"""
    store.set_fact(session=S, key="city", value="北京", when=100)
    assert "- 用户：city=北京" in _facts(store)


def test_pinned_survives_budget_with_16_facts(store, cfg):
    """C 段预算是 200。真机库有 16 条事实，一条一行会顶爆。

    M13 第一版修完就是一条一行，实测 250 token → 最后 4 条被裁掉，
    连 `city=北京` 都可能丢。改一人一行之后 192。
    """
    u = store.upsert_entity(session=S, canonical="用户")
    data = {"age": "32", "city": "北京", "district": "海淀", "commute": "步行",
            "employer": "字节", "occupation": "后端开发", "partner": "护士",
            "pet": "猫", "pet_age": "三岁", "pet_name": "年糕",
            "sleep": "晚11点睡早7点起", "mortgage": "有房贷", "health": "甲状腺需复查",
            "child": "孩子明年上小学", "diet": "不吃辣", "salary": "月薪三万"}
    for i, (k, v) in enumerate(data.items()):
        store.set_fact(session=S, key=k, value=v, entity_id=u, when=100 + i)

    text = _facts(store)
    assert estimate_tokens(text) <= 200, f"{estimate_tokens(text)} token 超了 C 段预算"
    assert len([ln for ln in text.splitlines() if ln.startswith("- ")]) == 1


def test_pinned_is_deterministic(store):
    """同一个库跑两次，字节必须一样。行序依赖 dict 迭代顺序的话这里会飘。"""
    u = store.upsert_entity(session=S, canonical="用户")
    for i, k in enumerate(("city", "district", "commute", "employer", "occupation")):
        store.set_fact(session=S, key=k, value=f"v{i}", entity_id=u, when=100 + i)
    assert _facts(store) == _facts(store)


def test_pinned_event_comes_first(store):
    """钉住的事件在事实之前。事实是背景，事件是"一定要记住"的那件事。"""
    eid = store.add_event(session=S, event_time=100, ingest_time=100,
                         summary="用户的婚礼在下个月", raw="婚礼下个月办",
                         pinned=1)
    u = store.upsert_entity(session=S, canonical="用户")
    store.set_fact(session=S, key="city", value="北京", entity_id=u, when=100)

    lines = [ln for ln in _facts(store).splitlines() if ln.startswith(("- ", "已知"))]
    assert lines[0].startswith("- 用户的婚礼")
    assert "已知：" in lines
