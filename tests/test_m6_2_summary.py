"""M6.2 摘要质量。

三条主线：
  1. 校验本身（长度 / 实体 / 人称）——纯函数，便宜。
  2. 修复回路——最多一次，且必须拿 raw 重写，不能拿旧摘要改旧摘要。
  3. 修不好的降级——截断 + 打标记，不许静默。
"""

from __future__ import annotations

import json

import httpx
import pytest

from hippo.core.encode import Encoder
from hippo.core.schema import parse_result
from hippo.core.summary import (
    MAX_LEN, issues_of, keep_rate, missing_entities, repair_block, scan, truncate,
    too_long,
)
from hippo.core.turns import Turn, Window


# ---------------------------------------------------------------- 校验
def test_too_long_boundary():
    assert too_long("字" * MAX_LEN) is False
    assert too_long("字" * (MAX_LEN + 1)) is True


def test_missing_entity_detected():
    raw = "老王说他下周要去深圳出差"
    assert missing_entities("用户的朋友要出差", raw, ["老王"]) == ["老王"]
    assert missing_entities("老王下周去深圳出差", raw, ["老王"]) == []


def test_missing_entity_ignores_names_not_in_raw():
    """模型幻觉出的人不在原文里，这里不该二次惩罚——那是锚定阶段的事。"""
    assert missing_entities("用户搬家了", "我搬家了", ["张三丰"]) == []


def test_first_person_detected():
    assert issues_of(0, "我昨天搬家了", "我昨天搬家了") is not None
    assert "first_person" in issues_of(0, "我昨天搬家了", "我昨天搬家了").kinds


def test_first_person_allows_plural():
    """"我们"在转述里是合理的（用户和老王一起做了什么），"我"不是。"""
    assert issues_of(0, "用户和老王一起吃了饭", "我们和老王一起吃了饭") is None


def test_clean_summary_has_no_issue():
    assert issues_of(0, "用户从朝阳搬到海淀，房租涨了两千",
                     "我昨天搬家了，从朝阳搬到海淀，房租涨了两千") is None


def test_empty_summary_is_an_issue():
    issue = issues_of(0, "", "我搬家了")
    assert issue is not None and "empty" in issue.kinds


def test_issue_reports_all_kinds_at_once():
    """一条摘要可以同时超长 + 丢实体 + 第一人称，要一次报全，
    否则修完长度再发现丢人名，白花一次调用。"""
    issue = issues_of(0, "我" + "啰嗦" * 40, "我昨天和王小明一起搬家了", ["王小明"])
    assert set(issue.kinds) >= {"too_long", "missing_entity", "first_person"}


def test_scan_uses_per_event_entities():
    res = parse_result(json.dumps({"events": [
        {"raw": "老王说他下周去深圳", "summary": "用户的朋友要出差",
         "entities": [{"name": "老王", "type": "person"}]},
    ]}))
    issues = scan(res.events)
    assert len(issues) == 1 and "missing_entity" in issues[0].kinds


def test_keep_rate_counts_occurrences_not_events():
    """"一条事件提到三个人、摘要只保住一个" 应该是 1/3，不是 0/1。"""
    # 摘要只保住"用户""上海"两个，老王被丢了
    assert keep_rate(["用户去了上海"], ["用户和老王去了上海"],
                     [["用户", "老王", "上海"]]) == pytest.approx(2 / 3, abs=1e-4)
    assert keep_rate([], [], []) == 1.0


# ---------------------------------------------------------------- 截断
def test_truncate_prefers_punctuation():
    # 79 字，远超 70。构造时用 join，避免手数错字
    long = "，".join(["用户从朝阳搬到海淀"] * 8)
    assert len(long) > MAX_LEN
    out = truncate(long)
    assert len(out) <= MAX_LEN
    assert out.endswith("…")
    assert out[-2] == "，", f"没在标点处断：{out!r}"


def test_truncate_noop_when_short():
    assert truncate("用户搬家了") == "用户搬家了"


def test_truncate_without_punctuation_still_cuts():
    out = truncate("字" * 200)
    assert len(out) == 69 and out.endswith("…")


# ---------------------------------------------------------------- 修复 prompt
def test_repair_block_gives_raw_not_old_summary():
    """这是 M6.2 最重要的一条：修复的输入必须是原文。

    给旧摘要，等于让模型"把一句已经错了的话改通顺"，
    每修一轮掉一层信息，且永远不会报错。
    """
    res = parse_result(json.dumps({"events": [
        {"raw": "我昨天搬家了从朝阳搬到海淀", "summary": "我搬家",
         "entities": [{"name": "老王", "type": "person"}]},
    ]}))
    block = repair_block(res.events, scan(res.events))
    assert "我昨天搬家了从朝阳搬到海淀" in block
    assert "我搬家" not in block
    assert "老王" in block


def test_repair_block_truncates_long_raw():
    long_raw = "搬" * 900
    res = parse_result(json.dumps({"events": [{"raw": long_raw, "summary": "我"}]}))
    block = repair_block(res.events, scan(res.events), raw_limit=100)
    assert "搬" * 100 in block and "搬" * 101 not in block


# ---------------------------------------------------------------- 修复回路
def _window(text: str = "我昨天搬家了，从朝阳搬到海淀") -> Window:
    return Window(session="s", idxs=[0, 1], token_est=50, has_pending=False,
                  turns=[Turn(session="s", role="user", content=text, idx=0, ts=1),
                         Turn(session="s", role="assistant", content="好的", idx=1, ts=2)])


def _encoder(cfg, responses: list[str]) -> tuple[Encoder, list[dict]]:
    """按顺序返回预设响应的 client，并记录每次请求体。"""
    seen: list[dict] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        body = queue.pop(0) if queue else "{}"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": body}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Encoder(cfg, client, "k"), seen


def _ev(summary: str, raw: str = "我昨天搬家了，从朝阳搬到海淀",
        entities: list[str] | None = None) -> dict:
    return {"events": [{"raw": raw, "summary": summary,
                        "entities": [{"name": n, "type": "person"}
                                     for n in (entities or [])]}]}


async def test_repair_triggers_second_call(cfg):
    first = json.dumps(_ev("我搬家了"))                       # 第一人称
    second = json.dumps({"summaries": [{"i": 0, "summary": "用户从朝阳搬到海淀"}]})
    cfg.extract.summary_repair = True
    enc, seen = _encoder(cfg, [first, second])

    out = await enc.encode(_window())
    assert len(seen) == 2, "摘要不合格却没触发修复调用"
    assert out.result.events[0].summary == "用户从朝阳搬到海淀"
    assert out.result.events[0].summary_truncated == 0


async def test_no_repair_when_summary_is_clean(cfg):
    cfg.extract.summary_repair = True
    enc, seen = _encoder(cfg, [json.dumps(_ev("用户从朝阳搬到海淀，房租涨了两千"))])
    out = await enc.encode(_window())
    assert len(seen) == 1, "摘要没问题却多花了一次调用"
    assert out.status == "ok"


async def test_repair_only_once(cfg):
    """修不好就算了。反复修会把预算烧在非关键路径上。"""
    first = json.dumps(_ev("我搬家了"))
    second = json.dumps({"summaries": [{"i": 0, "summary": "我" + "还是第一人称" * 3}]})
    cfg.extract.summary_repair = True
    enc, seen = _encoder(cfg, [first, second])
    out = await enc.encode(_window())
    assert len(seen) == 2
    assert out.result.events[0].summary_flagged == 1, "修不好必须打标记"
    assert out.result.events[0].summary_truncated == 0, "这条没超长，不该记成截断"


async def test_repair_failure_does_not_degrade_extraction(cfg):
    """修复调用崩了，抽取本身还是成功的。摘要是中间层，不是命门。"""
    cfg.extract.summary_repair = True

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(_ev("我搬家了"))}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50}})
        return httpx.Response(500, text="boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    enc = Encoder(cfg, client, "k")
    out = await enc.encode(_window())

    assert out.status == "ok", "修复失败不该把整窗降级"
    assert out.result.events[0].summary_flagged == 1, "修复失败的摘要要留痕"


async def test_repair_out_of_range_index_is_ignored(cfg):
    """模型偶尔给个不存在的下标。直接索引会 IndexError，
    把一次成功的抽取变成降级——代价远大于"少修一条"。"""
    first = json.dumps(_ev("我搬家了"))
    second = json.dumps({"summaries": [
        {"i": 9, "summary": "越界的"},
        {"i": 0, "summary": "用户从朝阳搬到海淀"},
    ]})
    cfg.extract.summary_repair = True
    enc, _ = _encoder(cfg, [first, second])
    out = await enc.encode(_window())
    assert out.status == "ok"
    assert out.result.events[0].summary == "用户从朝阳搬到海淀"


async def test_repair_can_be_disabled(cfg):
    cfg.extract.summary_repair = False
    enc, seen = _encoder(cfg, [json.dumps(_ev("我搬家了"))])
    out = await enc.encode(_window())
    assert len(seen) == 1
    assert out.status == "ok"


async def test_repair_tokens_counted_in_cost(cfg):
    """修复调用的 token 必须计进成本。

    漏记的话，报表会显示"我们只花了一次调用的钱"，
    而实际有相当比例的窗口花了两到三次——成本模型从此不可信。
    """
    first = json.dumps(_ev("我搬家了"))
    second = json.dumps({"summaries": [{"i": 0, "summary": "用户从朝阳搬到海淀"}]})
    cfg.extract.summary_repair = True
    enc, _ = _encoder(cfg, [first, second])
    out = await enc.encode(_window())
    assert out.prompt_tokens == 200
    assert out.completion_tokens == 100


# ---------------------------------------------------------------- 统计口径
def test_flag_totals_do_not_depend_on_turns_table(tmp_path):
    """摘要统计不能按 `list_sessions()` 遍历。

    `list_sessions()` 查的是 turns 表，而事件可能存在于没有 turn 记录的会话
    （导入、回放、手动补录）。按 turns 遍历会漏掉它们，
    表现是"报表少了一整段"，且不报错——和 M5.5 那个 `except: pass` 同源。
    """
    from hippo.core.db import connect
    from hippo.core.migrate import migrate
    from hippo.core.store import Store

    conn = connect(tmp_path / "s.db")
    migrate(conn)
    s = Store(conn)
    s.add_event(session="imported", raw="原文", summary="摘要",
                summary_truncated=1, keywords=["原文"])

    assert s.list_sessions() == [], "这个会话故意没有 turn 记录"
    totals = s.summary_flag_totals()
    assert totals["n"] == 1 and totals["truncated"] == 1
    s.close()
