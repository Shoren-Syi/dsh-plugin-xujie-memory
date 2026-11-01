"""稳定的编码器基准（XM-02 / XM-13）。

回答两个问题，只报数据不下结论：

1. **现在的编码器到底多少分**：固定语料、固定匹配规则、可复现的 JSON。
   不报"感觉好多了"，改一行代码就重跑、和 baseline 比 delta。
2. **错在哪**：按语言现象 / 语料分组给分，并列出具体错句，避免平均值掩盖局部灾难。

为什么是「可复现」：报告里带语料 hash、预测 hash、匹配规则版本；同一份输入
两次运行必须得到同一个预测 hash（determinism 字段）。规则编码器本身是确定的，
LLM 编码器不保证——所以基准默认只跑 RuleEncoder，LLM 的稳定性另用 evaluate.py 的
--repeat 观测。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .encoder import RuleEncoder

BENCHMARK_VERSION = 1
SLOT_KEYS = ("debtor", "creditor", "item", "quantity", "unit")
# 编码器用 self/player 两种写法，标注统一成 player。
SLOT_ALIASES = {"self": "player", "me": "player", "我": "player", "你": "kitty"}


def _canon_value(key: str, value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if key in ("debtor", "creditor"):
            value = SLOT_ALIASES.get(value, value)
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 6)
    return value


def canon_slots(slots: dict[str, Any]) -> dict[str, Any]:
    return {k: _canon_value(k, v) for k, v in slots.items() if v is not None}


def gold_intent(interp: dict[str, Any]) -> str | None:
    slots = interp.get("slots") or {}
    return interp.get("intent") or slots.get("intent") or interp.get("semantic_op")


def _pred_dict(pred) -> dict[str, Any]:
    return {
        "relation": pred.relation,
        "intent": pred.semantic_op,
        "slots": canon_slots(pred.slots or {}),
    }


def score_item(item: dict[str, Any], preds: list[Any]) -> dict[str, Any]:
    golds = [
        {
            "relation": g.get("relation"),
            "intent": gold_intent(g),
            "slots": canon_slots(g.get("slots") or {}),
        }
        for g in item.get("interpretations") or []
    ]
    predicted = [_pred_dict(p) for p in preds]
    gold_relations = {g["relation"] for g in golds}
    pred_relations = {p["relation"] for p in predicted}
    relation_hit = bool(gold_relations & pred_relations)
    false_positive = sorted(pred_relations - gold_relations)

    # 无关句（gold 没有 interpretation）：预测为空才算对；
    # 任何预测都是误报。以前把它算成"永远错"，会低估编码器。
    if not golds:
        empty = not predicted
        return {
            "id": item.get("id"),
            "lang": item.get("lang"),
            "utterance": item.get("utterance"),
            "gold": golds,
            "predicted": predicted,
            "relation_hit": empty,
            "intent_hit": empty,
            "exact": empty,
            "slot_matches": {},
            "false_positive": false_positive,
        }

    best: dict[str, Any] | None = None
    best_score = -1
    best_detail: dict[str, Any] = {}
    for gold in golds:
        for pred in predicted:
            slot_matches = {}
            for key in SLOT_KEYS:
                if key not in gold["slots"]:
                    continue
                slot_matches[key] = pred["slots"].get(key) == gold["slots"].get(key)
            score = (
                (2 if pred["relation"] == gold["relation"] else 0)
                + (1 if pred["relation"] == gold["relation"] and pred["intent"] == gold["intent"] else 0)
                + sum(1 for ok in slot_matches.values() if ok)
            )
            if score > best_score:
                best_score = score
                best = pred
                best_detail = {"gold": gold, "slot_matches": slot_matches}
    if best is None:
        return {
            "id": item.get("id"),
            "lang": item.get("lang"),
            "utterance": item.get("utterance"),
            "gold": golds,
            "predicted": predicted,
            "relation_hit": False,
            "intent_hit": False,
            "exact": False,
            "slot_matches": {},
            "false_positive": false_positive,
        }

    gold = best_detail["gold"]
    intent_hit = best["relation"] == gold["relation"] and best["intent"] == gold["intent"]
    exact = intent_hit and all(best_detail["slot_matches"].values())
    return {
        "id": item.get("id"),
        "lang": item.get("lang"),
        "utterance": item.get("utterance"),
        "gold": golds,
        "predicted": predicted,
        "relation_hit": best["relation"] == gold["relation"],
        "intent_hit": intent_hit,
        "exact": exact,
        "slot_matches": best_detail["slot_matches"],
        "false_positive": false_positive,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    slot_totals: Counter[str] = Counter()
    slot_hits: Counter[str] = Counter()
    for row in rows:
        for key, ok in row["slot_matches"].items():
            slot_totals[key] += 1
            slot_hits[key] += 1 if ok else 0
    by_lang: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        label = str(row.get("lang") or "未分组")
        by_lang[label].append(row)
        # 「断言-完整」「断言-已确认」归到「断言」，避免 1 条一组看不出趋势。
        by_family[label.split("-")[0]].append(row)

    def rate(part: int, total: int) -> float | None:
        return round(part / total, 4) if total else None

    summary = {
        "n": n,
        "coverage": rate(sum(1 for r in rows if r["predicted"]), n),
        "relation_accuracy": rate(sum(1 for r in rows if r["relation_hit"]), n),
        "intent_accuracy": rate(sum(1 for r in rows if r["intent_hit"]), n),
        "exact_match": rate(sum(1 for r in rows if r["exact"]), n),
        "false_positive_rate": rate(sum(1 for r in rows if r["false_positive"]), n),
        "slot_accuracy": {
            key: rate(slot_hits[key], slot_totals[key]) for key in SLOT_KEYS
        },
    }
    return {"summary": summary, "by_family": _grouped(by_family, rate), "by_lang": _grouped(by_lang, rate)}


def _grouped(groups: dict[str, list[dict[str, Any]]], rate) -> dict[str, dict[str, Any]]:
    out = {}
    for name, group in sorted(groups.items()):
        out[name] = {
            "n": len(group),
            "relation_accuracy": rate(sum(1 for r in group if r["relation_hit"]), len(group)),
            "intent_accuracy": rate(sum(1 for r in group if r["intent_hit"]), len(group)),
            "exact_match": rate(sum(1 for r in group if r["exact"]), len(group)),
        }
    return out


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_corpus(path: str | Path) -> list[dict[str, Any]]:
    items = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def run(corpus_path: str | Path, encoder=None, *, limit: int | None = None) -> dict[str, Any]:
    """跑一遍基准，返回可复现的报告（不含时间戳）。"""
    items = load_corpus(corpus_path)
    if limit is not None:
        items = items[:limit]
    encoder = encoder or RuleEncoder()
    rows = []
    for item in items:
        preds = encoder.encode(str(item.get("id")), item.get("utterance") or "", dict(item.get("context") or {}))
        rows.append(score_item(item, preds))

    predictions = [
        {"id": row["id"], "predicted": row["predicted"]} for row in rows
    ]
    pred_hash = _sha(_canonical(predictions))
    # 规则编码器是确定的，跑两遍校验；LLM 编码器不重复跑（浪费钱），
    # 它的稳定性用 --samples / examples/evaluate.py --repeat 单独观测。
    deterministic: bool | None = None
    if type(encoder).__name__ == "RuleEncoder":
        second = [
            _pred_dict(p)
            for item in items
            for p in encoder.encode(str(item.get("id")), item.get("utterance") or "", dict(item.get("context") or {}))
        ]
        deterministic = _sha(_canonical(second)) == _sha(
            _canonical([p for row in rows for p in row["predicted"]])
        )

    usage = None
    usages = getattr(encoder, "usage", None) or []
    if usages:
        usage = {
            "calls": len(usages),
            "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usages),
            "completion_tokens": sum(int(u.get("completion_tokens") or 0) for u in usages),
            "reasoning_tokens": sum(
                int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
                for u in usages
            ),
            "total_tokens": sum(int(u.get("total_tokens") or 0) for u in usages),
        }

    report = {
        "benchmark_version": BENCHMARK_VERSION,
        "encoder": type(encoder).__name__,
        "corpus": {
            "path": Path(corpus_path).name,
            "sha256": _sha(Path(corpus_path).read_text(encoding="utf-8")),
            "n": len(items),
        },
        **aggregate(rows),
        "deterministic": deterministic,
        "predictions_sha256": pred_hash,
        "usage": usage,
        "errors": [row for row in rows if not row["exact"]],
    }
    return report


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """只比 summary 里的标量；分组和逐句用 errors 自己看。"""
    out = {}
    for key, value in current["summary"].items():
        if key == "slot_accuracy":
            out[key] = {
                slot: _delta(baseline["summary"]["slot_accuracy"].get(slot), value[slot])
                for slot in value
            }
        else:
            out[key] = _delta(baseline["summary"].get(key), value)
    return out


def _delta(old: Any, new: Any) -> dict[str, Any]:
    if old is None or new is None:
        return {"baseline": old, "current": new, "delta": None}
    return {"baseline": old, "current": new, "delta": round(new - old, 4)}
