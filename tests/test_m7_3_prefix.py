"""M7.3 inferred 前缀与注入过滤。

这一层的唯一目标：**AI 的推测永远不会以事实身份进入上下文**。
所以每个断言都在检查"没渗出去"，而不是"渲染得好看"。
"""

from __future__ import annotations

import pytest

from hippo.core.card import (
    INFERRED_PREFIX, MIN_INJECT_CONF, format_trait, injectable, render_card,
    render_traits,
)
from hippo.core.models import CardTrait, Entity


def _t(**kw) -> CardTrait:
    base = dict(id=1, entity_id=1, trait="做后端", status="observed",
                evidence=[10], confidence=0.8, valid_from=1, valid_to=None, updated_at=1)
    base.update(kw)
    return CardTrait(**base)


def _e(**kw) -> Entity:
    base = dict(id=1, session="s", canonical="老王", aliases=[], type="person",
                confidence=0.82, first_seen=1, last_seen=2)
    base.update(kw)
    return Entity(**base)


def test_inferred_always_prefixed():
    assert format_trait(_t(status="inferred", trait="可能对现状不满")) == \
        f"{INFERRED_PREFIX}可能对现状不满"
    assert format_trait(_t(status="observed")) == "做后端"


def test_observed_never_prefixed():
    assert INFERRED_PREFIX not in format_trait(_t(status="observed", trait="养了一只猫"))


def test_low_confidence_not_injected():
    low = _t(status="inferred", confidence=0.45)
    assert injectable(low) is False
    lines = render_traits([_t(id=2, confidence=0.8), low])
    assert lines == ["做后端"]


def test_expired_trait_not_injected():
    assert injectable(_t(valid_to=123)) is False


def test_observed_first_then_confidence(store=None):
    ts = [
        _t(id=1, status="inferred", confidence=0.9, trait="推测A"),
        _t(id=2, status="observed", confidence=0.6, trait="事实B"),
        _t(id=3, status="observed", confidence=0.9, trait="事实C"),
    ]
    assert render_traits(ts) == ["事实C", "事实B", f"{INFERRED_PREFIX}推测A"]


def test_max_n_caps_context():
    ts = [_t(id=i, trait=f"特征{i}", confidence=0.5 + i / 100) for i in range(20)]
    assert len(render_traits(ts, max_n=5)) == 5


def test_render_card_marks_uninjected():
    e = _e()
    ts = [
        _t(id=1, trait="在创业公司做后端", confidence=0.9, evidence=[1024, 1087]),
        _t(id=2, status="inferred", trait="可能对现状不满", confidence=0.45, evidence=[1090]),
    ]
    out = render_card(e, ts)
    assert out.splitlines()[0] == "老王 (person, conf 0.82)"
    assert "[observed] 在创业公司做后端" in out
    assert "#1024" in out and "#1087" in out
    assert f"[inferred] {INFERRED_PREFIX}可能对现状不满" in out
    assert "(未注入)" in out


def test_render_card_empty():
    assert "没有特征" in render_card(_e(), [])


def test_threshold_is_half():
    assert MIN_INJECT_CONF == 0.5
    assert injectable(_t(confidence=0.5)) is True
    assert injectable(_t(confidence=0.49)) is False


# ---------------------------------------------------------------- CLI
def test_cli_traits_lists_card(tmp_path, capsys):
    from hippo.cli import main
    from hippo.core.db import connect
    from hippo.core.migrate import migrate
    from hippo.core.store import Store

    db = tmp_path / "mem.db"
    c = connect(db)
    migrate(c)
    st = Store(c)
    eid = st.resolve_entity(session="main", name="老王").target_id
    ev = st.add_event(session="main", raw="我在创业公司做后端", entities=[eid])
    st.add_trait(entity_id=eid, trait="在创业公司做后端", evidence=[ev],
                 confidence=0.82, slot="employer")
    st.add_trait(entity_id=eid, trait="可能对现状不满", evidence=[ev],
                 status="inferred", confidence=0.45)
    c.close()

    rc = main(["traits", "--dir", str(tmp_path), "--entity", "老王"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "老王 (person" in out
    assert "[observed] 在创业公司做后端" in out
    assert "[inferred]" in out and "(未注入)" in out


def test_cli_traits_finds_by_alias(tmp_path, capsys):
    from hippo.cli import main
    from hippo.core.db import connect
    from hippo.core.migrate import migrate
    from hippo.core.store import Store

    c = connect(tmp_path / "mem.db")
    migrate(c)
    Store(c).resolve_entity(session="main", name="老王", aliases=["王哥"])
    c.close()

    rc = main(["traits", "--dir", str(tmp_path), "--entity", "王哥"])
    assert rc == 0
    assert "老王" in capsys.readouterr().out


def test_cli_traits_unknown_entity(tmp_path, capsys):
    from hippo.cli import main
    from hippo.core.db import connect
    from hippo.core.migrate import migrate

    c = connect(tmp_path / "mem.db")
    migrate(c)
    c.close()

    rc = main(["traits", "--dir", str(tmp_path), "--entity", "不存在的人"])
    assert rc == 1
    assert "没找到" in capsys.readouterr().out
