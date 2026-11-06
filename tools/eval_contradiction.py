"""M8.2 评测：矛盾检出率与误报率。

手册的验收是"15 组矛盾对，检出率 ≥95%"。这个指标单独看是**没有意义**的：
把每一条新事实都判成矛盾，检出率 100%。所以这份语料里
矛盾对和"看起来像矛盾但不是"的对各占一半，两个数字必须一起报：

    检出率（recall）  = 该报的报了几条
    误报数（false pos）= 不该报的报了几条 ← 这个比 recall 重要

误报的代价不对称：漏一条矛盾只是记忆里留着旧值（下次还会被纠正），
误报一条会把"我搬到了海淀"记成"你搬家了"，并且**把旧的 valid_to 写死**，
之后查"我以前住哪"会给出一个从未结束的区间。

用法：
    python3 -m tools.eval_contradiction
    python3 -m tools.eval_contradiction --only refine --show
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from hippo.core.db import connect
from hippo.core.migrate import migrate
from hippo.core.store import Store

CASES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "contradictions.jsonl"
OUT = Path("reports/data/eval_contradiction.json")

# 该报矛盾的 kind：contradiction 是常规状态变化，guard 是"看着像细化、
# 其实是换了对象"（北京银行→北京、张伟→张伟明）。后者是误报的重灾区。
MUST_DETECT = {"contradiction", "guard"}
# 不该报矛盾的 kind：same 是同值异形，refine 是粒度差异，multi 是多值槽
MUST_NOT = {"same", "refine", "multi"}


def _naive_decide(key, existing, value, confidence=0.6, *, label="用户", threshold=0.6):
    """M8 之前的实现：字符串相等就算同值，其余一律判矛盾。

    留着它不是为了怀旧，是为了**证明这份语料有牙**：
    如果一个"什么都没做对"的实现也能拿满分，说明用例太软。
    """
    from hippo.core.facts import Decision

    for fid, old in existing:
        if old.strip() == str(value).strip():
            return Decision("same", keep_id=fid, value=value)
    if not existing:
        return Decision("insert", value=value)
    if not key.single:
        return Decision("insert", value=value)
    if confidence >= threshold:
        return Decision("replace", close_ids=tuple(f for f, _ in existing), value=value)
    return Decision("doubt", value=value, question="?")


def load_cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def run_one(case: dict) -> dict:
    """每个用例独立建库。共享库会让上一个用例的 active 行影响下一个的判定。"""
    tmp = Path(tempfile.mkdtemp(prefix="hippo-c-"))
    c = connect(tmp / "c.db")
    migrate(c)
    st = Store(c)
    try:
        ev = st.add_event(session="s", raw=f"{case['old']} -> {case['new']}",
                          keywords=["测试"], summary="测试事件")
        w1 = st.set_fact(session="s", key=case["key"], value=case["old"],
                         confidence=case["conf"], source_event=ev, when=1000)
        w2 = st.set_fact(session="s", key=case["key"], value=case["new"],
                         confidence=case["conf"], source_event=ev, when=2000)
        active = st.get_fact("s", case["key"])
        hist = st.get_fact("s", case["key"], include_historical=True)
        closed = [f for f in hist if f.valid_to is not None]
        return {
            "id": case["id"], "kind": case["kind"], "key": case["key"],
            "old": case["old"], "new": case["new"],
            "first_action": w1.action, "action": w2.action,
            "n_active": len(active),
            "n_closed": len(closed),
            "reason": w2.decision.reason,
            "question": w2.decision.question,
        }
    finally:
        st.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只看某个 kind")
    ap.add_argument("--show", action="store_true", help="逐条打印")
    ap.add_argument("--naive", action="store_true",
                    help="用 M8 之前的朴素比较跑一遍，验证语料有牙")
    args = ap.parse_args()

    if args.naive:
        import hippo.core.store as _st

        _st.decide_fact = _naive_decide
        print("（朴素模式：只做字符串相等，不做任何归一）")

    cases = load_cases()
    if args.only:
        cases = [c for c in cases if c["kind"] == args.only]

    rows = [run_one(c) for c in cases]

    hit = fn = fp = 0
    bad: list[str] = []
    for r in rows:
        detected = r["action"] == "replace" and r["n_closed"] >= 1
        if r["kind"] in MUST_DETECT:
            if detected:
                hit += 1
            else:
                fn += 1
                bad.append(f"  漏报 {r['id']} [{r['kind']}] {r['key']}: "
                           f"{r['old']} → {r['new']}  实际={r['action']}")
        elif r["kind"] in MUST_NOT:
            if detected:
                fp += 1
                bad.append(f"  误报 {r['id']} [{r['kind']}] {r['key']}: "
                           f"{r['old']} → {r['new']}  实际={r['action']}")
        elif r["kind"] == "low_conf":
            # 低置信度不许改库：要么 doubt，要么 same。replace 就是错。
            if r["action"] == "replace":
                fp += 1
                bad.append(f"  误报 {r['id']} [low_conf] {r['key']}: 低置信度覆盖了旧值")
            elif r["action"] != "doubt":
                bad.append(f"  存疑 {r['id']} [low_conf] 期望 doubt，实际 {r['action']}")

        if args.show:
            print(f"  {r['id']:<4} {r['kind']:<14} {r['key']:<12} "
                  f"{r['old']:<12} → {r['new']:<12} {r['action']:<8} {r['reason']}")

    must = len([r for r in rows if r["kind"] in MUST_DETECT])
    not_must = len([r for r in rows if r["kind"] in MUST_NOT])
    recall = hit / must if must else 0.0

    print(f"\n用例 {len(rows)}")
    print(f"矛盾检出 {hit}/{must} = {recall:.1%}   漏报 {fn}   误报 {fp}/{not_must}")
    by_kind: dict[str, list[str]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r["action"])
    for k, acts in by_kind.items():
        print(f"  {k:<14} {len(acts):>2} 条  动作 {dict((a, acts.count(a)) for a in set(acts))}")
    if bad:
        print("\n问题：")
        print("\n".join(bad))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "cases": len(rows), "must_detect": must, "hit": hit, "miss": fn,
        "false_positive": fp, "recall": round(recall, 4), "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = fn == 0 and fp == 0
    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
