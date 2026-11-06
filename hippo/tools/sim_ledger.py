"""M23 账簿模拟器：10 组"自主动作 → 用户反馈"，结算对不对？

    python -m hippo.tools.sim_ledger

手册 M23 的 DoD：构造 10 组，结算正确率 100%。这里真的建库、真的
走 register/settle，不 mock 任何一步——mock 出来的 100% 没有意义。

十组覆盖的分支（每组只测一件事，错了能立刻定位）：

    1  改配置被骂              → edit_file / -1 / 生成懊悔
    2  主动发消息被谢          → message / +1 / 无懊悔
    3  两条待结算，反馈指向旧的 → 结算旧的那条，新的留着
    4  三天前的动作+今天抱怨    → 过期作废，不结算
    5  提议被否                → propose / -1
    6  翻目录被批评            → explore / -1
    7  自己本就不看好，被骂     → rpe 小，不生成懊悔
    8  同一动作被骂两次         → 第二次不再结算
    9  既夸又骂                → 负反馈优先
    10 反馈与动作毫无关联       → 取最近一条，但仍只结算 1 条
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from ..core import ledger
from ..core.db import connect
from ..core.migrate import migrate
from ..core.store import Store

DAY = 86400
T0 = 1_760_000_000


@dataclass
class Case:
    name: str
    ok: bool
    detail: str


def _fresh() -> Store:
    c = connect(":memory:")
    migrate(c)
    return Store(c)


def _run() -> list[Case]:
    out: list[Case] = []

    # ---- 1 改配置被骂
    s = _fresh()
    aid = ledger.register(s, "s", "擅自修改了配置文件", 0.85, now=T0)
    st = ledger.settle(s, "s", "你怎么又擅自改我配置，下次先问我", -1.0, now=T0 + 3600)
    ok = (st is not None and st.action_id == aid and st.actual == -1.0
          and st.rpe < 0 and st.regret_id is not None)
    out.append(Case("改配置被骂", ok,
                    f"rpe={st.rpe:.2f} regret={st.regret_id}" if st else "未结算"))

    # ---- 2 主动发消息被谢
    s = _fresh()
    aid = ledger.register(s, "s", "主动给用户发了条消息", 0.3, now=T0)
    st = ledger.settle(s, "s", "谢谢你提醒我吃药", 1.0, now=T0 + 600)
    ok = st is not None and st.actual == 1.0 and st.rpe > 0 and st.regret_id is None
    out.append(Case("发消息被谢", ok, f"rpe={st.rpe:+.2f}" if st else "未结算"))

    # ---- 3 两条待结算，反馈指向旧的那条
    s = _fresh()
    old = ledger.register(s, "s", "改了配置文件", 0.6, now=T0)
    new = ledger.register(s, "s", "给你发了条消息", 0.6, now=T0 + 60)
    st = ledger.settle(s, "s", "谁让你改我配置的", -1.0, now=T0 + 900)
    ok = (st is not None and st.action_id == old
          and s.get_ledger(old).settled == 1
          and s.get_ledger(new).settled == 0)
    out.append(Case("按相关度选旧的", ok,
                    f"结算={'旧的' if st and st.action_id == old else '错的'}"))

    # ---- 4 三天前的动作 + 今天抱怨
    s = _fresh()
    aid = ledger.register(s, "s", "改了配置文件", 0.5, now=T0)
    st = ledger.settle(s, "s", "今天真烦", -1.0, now=T0 + 4 * DAY)
    ok = st is None and s.get_ledger(aid).settled == -1
    out.append(Case("过期不猜", ok, f"settled={s.get_ledger(aid).settled}"))

    # ---- 5 提议被否
    s = _fresh()
    aid = ledger.register(s, "s", "建议用户换个方案", 0.7, now=T0)
    st = ledger.settle(s, "s", "别瞎建议，我自己有数", -1.0, now=T0 + 300)
    ok = st is not None and st.kind == "propose" and st.regret_id is not None
    out.append(Case("提议被否", ok, f"kind={st.kind if st else '-'}"))

    # ---- 6 翻目录被批评
    s = _fresh()
    aid = ledger.register(s, "s", "翻看了用户的工作目录", 0.4, now=T0)
    st = ledger.settle(s, "s", "你干嘛翻我目录，我不喜欢这样", -1.0, now=T0 + 120)
    ok = st is not None and st.kind == "explore" and st.rpe <= -0.5
    out.append(Case("翻目录被批评", ok, f"kind={st.kind if st else '-'}"))

    # ---- 7 自己本就不看好，被骂也不懊悔
    s = _fresh()
    aid = ledger.register(s, "s", "改了配置文件", -0.9, now=T0)
    st = ledger.settle(s, "s", "又改我配置", -1.0, now=T0 + 60)
    ok = st is not None and st.rpe > -0.5 and st.regret_id is None
    out.append(Case("没落差就不懊悔", ok, f"rpe={st.rpe:+.2f}" if st else "未结算"))

    # ---- 8 同一动作被骂两次
    s = _fresh()
    aid = ledger.register(s, "s", "改了配置文件", 0.8, now=T0)
    first = ledger.settle(s, "s", "别改我配置", -1.0, now=T0 + 60)
    second = ledger.settle(s, "s", "还是很烦", -1.0, now=T0 + 120)
    ok = first is not None and second is None and len(s.regrets("s")) == 1
    out.append(Case("不重复结算", ok, f"regrets={len(s.regrets('s'))}"))

    # ---- 9 既夸又骂
    s = _fresh()
    aid = ledger.register(s, "s", "改了配置文件", 0.6, now=T0)
    st = ledger.settle(s, "s", "思路不错，但不该不打招呼就动我配置", -1.0, now=T0 + 60)
    ok = st is not None and st.actual == -1.0
    out.append(Case("负反馈优先", ok, f"actual={st.actual:+.0f}" if st else "未结算"))

    # ---- 10 反馈与动作毫无关联
    s = _fresh()
    a1 = ledger.register(s, "s", "改了配置文件", 0.5, now=T0)
    a2 = ledger.register(s, "s", "给你发了条消息", 0.5, now=T0 + 60)
    st = ledger.settle(s, "s", "嗯", -1.0, now=T0 + 600)
    settled = [x for x in (a1, a2) if s.get_ledger(x).settled == 1]
    ok = st is not None and len(settled) == 1
    out.append(Case("无关联也只结 1 条", ok, f"settled={len(settled)}"))

    # ---- 11 数字不进 prompt（DoD 的第二半，单独记一行）
    from ..core.pack import pack
    from datetime import datetime
    s = _fresh()
    ledger.register(s, "s", "改了配置文件", 0.85, now=T0)
    ledger.settle(s, "s", "别改我配置", -1.0, now=T0 + 60)
    text = pack(s, "s", "我想改配置文件", use_cache=False,
                upcoming_action="改一下配置文件",
                now=datetime(2026, 1, 1, 12, 0)).text
    leak = [w for w in ("-1.85", "0.85", "predicted", "rpe") if w in text]
    out.append(Case("数值不进 prompt", not leak, f"泄漏={leak or '无'}"))

    return out


def main() -> int:
    import logging
    logging.getLogger("hippo.core.ledger").setLevel(logging.WARNING)
    argparse.ArgumentParser(description="M23 账簿结算模拟").parse_args()

    cases = _run()
    width = max(len(c.name) for c in cases)
    for i, c in enumerate(cases, 1):
        mark = "OK  " if c.ok else "FAIL"
        print(f"{i:>2}. {mark} {c.name.ljust(width)}  {c.detail}")
    ok = sum(1 for c in cases if c.ok)
    print(f"\n结算正确率 {ok}/{len(cases)} = {ok / len(cases):.0%}")
    return 0 if ok == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
