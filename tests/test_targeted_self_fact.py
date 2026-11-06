from hippo.config import Config
from hippo.core.pack import _targeted_self_fact, pack


def test_only_explicit_self_current_fact_is_selected(store):
    store.set_fact(session='s', key='age', value='30岁', when=100)
    other = store.upsert_entity(session='s', canonical='妈妈', type='person')
    store.set_fact(session='s', key='age', value='60岁', entity_id=other, when=110)
    assert '30岁' in _targeted_self_fact(store, 's', '我今年多大？')
    assert _targeted_self_fact(store, 's', '我妈妈今年多大？') == ''
    assert _targeted_self_fact(store, 's', '妈妈今年多大？') == ''
    assert _targeted_self_fact(store, 's', '我以前多大？') == ''
    assert _targeted_self_fact(store, 'other', '我今年多大？') == ''


def test_fact_is_available_even_when_fixed_fact_line_is_trimmed(store):
    store.set_fact(session='s', key='age', value='30岁', when=100)
    cfg = Config()
    cfg.pack.pinned_tokens = 5
    result = pack(store, 's', '我今年多大？', cfg=cfg, use_cache=False)
    assert '[当前事实] 用户的年龄：30岁' in result.text


def test_ambiguous_or_non_question_does_not_inject_fact(store):
    store.set_fact(session='s', key='age', value='30岁', when=100)
    for text in ('我今年多大', '我朋友多大？', '我说的年龄很好', '你今年多大？'):
        if text == '我今年多大':
            # “多大”本身是明确的疑问表达。
            assert _targeted_self_fact(store, 's', text)
        else:
            assert not _targeted_self_fact(store, 's', text)


def test_self_fact_uses_latest_active_value_only(store):
    store.set_fact(session='s', key='age', value='30岁', when=100)
    store.set_fact(session='s', key='age', value='31岁', when=200)
    result = _targeted_self_fact(store, 's', '我今年多大？')
    assert '31岁' in result
    assert '30岁' not in result


def test_self_fact_does_not_confuse_nested_possessive(store):
    store.set_fact(session='s', key='age', value='30岁', when=100)
    for question in ('我的猫今年多大？', '我老板今年多大？', '我朋友的年龄是多少？',
                     '我以前的年龄是多少？'):
        assert not _targeted_self_fact(store, 's', question)
