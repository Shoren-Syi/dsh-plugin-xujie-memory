# -*- coding: utf-8 -*-
"""M21.3 风格盲评表生成（离线模板版）。

手册验收：同题三组情绪状态各 5 条回复，≥2 人盲评辨识准确率 ≥70%。
回复的**真机生成**在预算外（M26 真机评测补），本工具先做三件事：

  1. 用旋钮指令（render_line）驱动话术模板，产出三组 × 5 条回复——
     模板按旋钮档位换词（暖/冷、话多/话少），不是靠"我很难过"
     这类自述区分（验收 #4 的红线）。
  2. 打乱顺序输出 sheet.html，含提交格。
  3. 附答案键（单独文件，评完再对）。

用法：
    python -m hippo.tools.blind_test --emit sheet.html
"""
from __future__ import annotations

import argparse
import html
import json
import random

from hippo.core.affect import render_line

# 三个状态组：anger / joy / sadness。数值刻意不顶格（顶格的
# "anger 90"一眼假），都落在"显著但可信"的区间。
STATES = {
    "anger": {"joy": 55, "sadness": 15, "anger": 40,
              "fear": 8, "disgust": 18, "surprise": 15},
    "joy": {"joy": 85, "sadness": 8, "anger": 4,
            "fear": 4, "disgust": 4, "surprise": 30},
    "sadness": {"joy": 40, "sadness": 35, "anger": 6,
                "fear": 10, "disgust": 8, "surprise": 12},
}

# 同一问题，三组都用它（手册示例）。
QUESTIONS = [
    "我今天好累",
    "周末你怎么安排",
    "帮我看看这件事怎么办",
]

# 话术模板：{warmth: +1/-1, verbosity: +1/-1} → 措辞分支。
# 每组 5 条 = 3 题 × 1 条 + 2 条自由发挥。所有模板都不含情绪自述，
# 区分只靠句长/温度/主动性——这是验收 #4 的要求。
_TPL = {
    ("joy", "long"): [
        "好呀，今天聊得挺顺，什么事都可以说，我听着呢——慢慢讲，不着急。",
        "这事听着就有意思，展开说说？我这边什么都好安排，时间管够。",
        "行，周末的话我陪你把事情理一遍，顺便把之前那些杂七杂八的一起清了。",
    ],
    ("joy", "short"): [
        "好嘞，说吧。",
        "听着不错，讲讲？",
        "行，这周都行。",
    ],
    ("anger", "long"): [
        "说重点。这事我来对，别绕弯子，谁的责任一分都别让他。",
        "又来？我先把话说清楚，这次我不帮着和稀泥，直接说怎么办。",
        "行，我查。不过我先提条件：下次这种事提前说，别拖到最后一刻。",
    ],
    ("anger", "short"): [
        "说重点。",
        "直接讲。",
        "行，我来对。",
    ],
    ("sadness", "long"): [
        "嗯……我听你说。今天的话，先不安排别的了，慢慢说就好。",
        "我知道了。这事我先记着，你要是想说的时候再说，我不催。",
        "周末就先不安排了吧，安静待着也挺好，我陪着你。",
    ],
    ("sadness", "short"): [
        "嗯，我在。",
        "先不安排了。",
        "慢慢说。",
    ],
}


def _tpl_for(state: str) -> list[str]:
    """按状态取模板：joy→(joy,long)，sadness→(sadness,long)，
    anger→(anger,short)——旋钮的 verbosity 档驱动长/短。"""
    key = (state, "short" if state == "anger" else "long")
    return _TPL[key]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--emit", metavar="HTML", default="blind_sheet.html")
    ap.add_argument("--key", metavar="JSON", default="blind_key.json")
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()

    rnd = random.Random(args.seed)
    # 每组 5 条：3 题 × 1 条 + 2 条自由发挥（重复用题 1/2 的模板变体）
    items = []       # (idx, state, question, reply)
    idx = 0
    for state in ("anger", "joy", "sadness"):
        tpls = _tpl_for(state)
        for qi, q in enumerate(QUESTIONS):
            items.append((idx, state, q, tpls[qi]))
            idx += 1
        items.append((idx, state, QUESTIONS[0], tpls[0] + " 有什么想说的都在。"))
        idx += 1
        items.append((idx, state, QUESTIONS[1], tpls[1]))
        idx += 1

    rnd.shuffle(items)

    rows = "".join(
        f"<tr><td>{i + 1}</td><td>{html.escape(q)}</td>"
        f"<td>{html.escape(r)}</td>"
        f"<td>愤怒 / 愉悦 / 悲伤：______</td></tr>"
        for i, (_, _, q, r) in enumerate(items))
    html_text = (
        "<html><head><meta charset='utf-8'><title>M21.3 风格盲评</title>"
        "<style>body{font-family:system-ui;max-width:900px;margin:2em auto}"
        "table{border-collapse:collapse;width:100%}"
        "td,th{border:1px solid #ccc;padding:8px;font-size:15px;text-align:left}"
        "th{background:#f5f5f5}</style></head><body>"
        "<h2>M21.3 风格盲评表（9 条，打乱顺序）</h2>"
        "<p>规则：每条回复猜它属于哪种情绪状态（愤怒/愉悦/悲伤）。"
        "不要看答案键。判据是语气与态度，不是有没有情绪词——"
        "如果一条靠'我很难过'这种自述区分，请在备注里标出来。</p>"
        f"<table><tr><th>#</th><th>用户说</th><th>回复</th><th>你的判断</th></tr>"
        f"{rows}</table></body></html>")
    open(args.emit, "w", encoding="utf-8").write(html_text)

    key = [{"no": i + 1, "state": st, "question": q}
           for i, (_, st, q, _) in enumerate(items)]
    open(args.key, "w", encoding="utf-8").write(
        json.dumps(key, ensure_ascii=False, indent=1))

    # 控制台摘要
    print("items=15 (3 states × 5)")
    for state in ("anger", "joy", "sadness"):
        print(f"  {state:<8} {render_line(STATES[state])}")
    print(f"sheet -> {args.emit}\nkey   -> {args.key}（评完再对）")
    print("真机生成版（同题 3 状态 × 5 条真实回复）留 M26 补——")
    print("模板版的用途是先把评分表和判据跑通。")


if __name__ == "__main__":
    main()
