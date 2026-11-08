# -*- coding: utf-8 -*-
"""合并标注并校验：答案必须是该会话用户原话的连续片段。"""
from __future__ import annotations

import collections
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:\h\hippo")
from hippo.tools.eval_all import norm  # noqa: E402

STRESS = Path(r"C:\h\stress")
CORPUS = STRESS / "real-corpus.jsonl"
BATCHES = [STRESS / f"labels-batch-{i}.json" for i in range(4)]
BASE = STRESS / "human-labels-50.json"
OUT = STRESS / "human-labels-200.json"


def main() -> int:
    rows = [json.loads(l) for l in io.open(CORPUS, encoding="utf-8") if l.strip()]
    user_text: dict[str, str] = collections.defaultdict(str)
    for r in rows:
        if r["role"] == "user":
            user_text[r["session"]] += " " + r["text"]

    labels = json.loads(BASE.read_text(encoding="utf-8"))
    for path in BATCHES:
        if path.exists():
            labels.extend(json.loads(path.read_text(encoding="utf-8")))

    bad = []
    seen = set()
    uniq = []
    for item in labels:
        key = (item["session"], item["question"])
        if key in seen:
            continue
        seen.add(key)
        src = user_text.get(item["session"], "")
        if not src:
            # 50 题库来自另一份导出（private-eval-*），不在 real-corpus 里，跳过校验
            uniq.append(item)
            continue
        if norm(item["answer"]) not in norm(src):
            bad.append(item)
        uniq.append(item)

    OUT.write_text(json.dumps(uniq, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"合并后 {len(uniq)} 条；答案不在原话里的 {len(bad)} 条")
    for item in bad[:15]:
        print(f"  [{item['session']}] {item['question'][:26]} → {item['answer']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
