# -*- coding: utf-8 -*-
"""M21.1 人格预设区分度检查（离线部分）。

手册验收要求"每套录 20 条回复样本，人工评 5 分制"——回复生成需要
真机（预算外），这里先做**离线可验证的一半**。

区分度的口径要想清楚：同一 delta 下三套预设的旋钮输出**必然相同**
（旋钮由基线偏移驱动，偏移一样指令就一样）——这不是缺陷，是语义。
预设的区分度来自**同一事件后的状态轨迹不同**：warm 被夸一次常态
就在愉悦态里待一小时；sharp 被夸十分钟就回中性。所以评测单位是
"事件 → 轨迹"，不是"delta → 一行"。

用法：
    python -m hippo.tools.eval_persona
    python -m hippo.tools.eval_persona --emit persona_sheet.html
"""
from __future__ import annotations

import argparse

from hippo.core.affect import PRESETS, decay_moods, render_line
from hippo.core.affect import TAU_MINUTES

# 20 个事件场景：delta 怼到六维上（正负都有）。评测对每套预设输出
# "当下 + 30 分钟后"两行——轨迹差就是人设差。
SCENARIOS: list[dict[str, float]] = [
    {},                                                # 平静
    {"joy": 30}, {"sadness": 30}, {"anger": 30},
    {"fear": 30}, {"disgust": 30}, {"surprise": 30},
    {"joy": 30, "sadness": 20},                        # 苦尽甘来
    {"anger": 40, "fear": 20},                         # 又气又怕
    {"sadness": 25, "anger": 15},                      # 憋屈
    {"joy": 40, "surprise": 40},                       # 惊喜
    {"disgust": 35, "anger": 25},                      # 又恶心又火
    {"fear": 35, "surprise": 30},                      # 吓一跳
    {"sadness": 40}, {"joy": 50}, {"anger": 50},
    {"disgust": 45}, {"surprise": 50}, {"fear": 45},
    {"joy": -20, "sadness": 20},                       # 被泼冷水
    {"anger": 45, "disgust": 30},                      # 忍无可忍
]


def knob_vector(knobs) -> tuple[float, float, float, float]:
    return (knobs.warmth, knobs.initiative, knobs.verbosity, knobs.recall_breadth)


def trace(base: dict[str, float], delta: dict[str, float]) -> list[str]:
    """同一事件在某预设下的状态轨迹：当下 / 30 分钟后 / 3 小时后。"""
    from hippo.core.affect import apply_delta
    moods = apply_delta(base, delta, inhibition={})   # 预设对比时不叠抑制
    rows = [render_line(moods, baseline=base)]
    for minutes in (30, 180):
        moods = decay_moods(moods, baseline=base, tau=TAU_MINUTES, minutes=minutes)
        rows.append(render_line(moods, baseline=base))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--emit", metavar="HTML", default=None)
    args = ap.parse_args()

    names = list(PRESETS)

    # ---- 区分度量化：同一场景下，三套预设的"轨迹行是否不同"
    same = diff = 0
    for sc in SCENARIOS:
        lines = {n: trace(PRESETS[n], sc) for n in names}
        for i in range(3):
            if lines[names[0]][i] == lines[names[1]][i] == lines[names[2]][i]:
                same += 1
            else:
                diff += 1
    print(f"轨迹行区分度: {diff}/{same + diff} 个时间点三套不同 "
          f"（平静场景三套相同是正常的——人设只在有事时显形）")

    # ---- 场景表
    for sc in SCENARIOS:
        tag = "，".join(f"{m}{v:+.0f}" for m, v in sc.items()) or "平静"
        print(f"\n── {tag}")
        for name in names:
            rows = trace(PRESETS[name], sc)
            print(f"  {name:<8} now: {rows[0]}")
            print(f"  {'':<8} 30m: {rows[1]}")
            print(f"  {'':<8}  3h: {rows[2]}")

    if args.emit:
        import html as _html
        parts = ["<html><head><meta charset='utf-8'><title>M21.1 人格预设评分</title>",
                 "<style>body{font-family:system-ui;max-width:1100px;margin:2em auto}",
                 "table{border-collapse:collapse;width:100%}",
                 "td,th{border:1px solid #ccc;padding:6px;font-size:14px;text-align:left}",
                 "th{background:#f5f5f5}</style></head><body>",
                 "<h2>M21.1 人格预设评分清单（20 场景 × 3 预设 × 3 时刻）</h2>",
                 "<p>人工评分 1~5（是否符合该预设的预期人设），M26 真机评测时用。</p>",
                 "<table><tr><th>场景</th><th>预设</th><th>轨迹（now / 30m / 3h）</th><th>评分</th></tr>"]
        for sc in SCENARIOS:
            tag = "，".join(f"{m}{v:+.0f}" for m, v in sc.items()) or "平静"
            for name in names:
                rows = trace(PRESETS[name], sc)
                cell = "<br>".join(_html.escape(r) for r in rows)
                parts.append(f"<tr><td>{_html.escape(tag)}</td><td>{name}</td>"
                             f"<td>{cell}</td><td>______</td></tr>")
        parts.append("</table></body></html>")
        open(args.emit, "w", encoding="utf-8").write("".join(parts))
        print(f"\nsheet -> {args.emit}")


if __name__ == "__main__":
    main()
