# R-42 · 会话级摘要：做出来了，但注入有 bug

## 做了什么

`hippo/core/digest.py`：会话结束（/drain）时用真实模型生成一条 60~120 字摘要，
写进 `session_state`；`pack()` 把它当"必记"内容注入。

4 个真实会话实测生成结果（134~146 字），内容质量可以，关键值都在：

- `private-eval-01`：「…仅保留一个初始恢复密钥存于U盘、放程序员柜中；若U盘丢失被毁，则通过腾讯云后台修改。」
- `private-eval-05`：「…画风定为新中式2D动漫平涂…3:4竖幅半身像…」
- `private-eval-08`：「…属性设精力值、狗品值、拆家值、伤人次数…」

## 踩到的坑（都已修或记录）

1. **`Encoder.chat` 不能复用**：它是抽取特化的，带 JSON 格式约束，纯文本请求返回 400。
2. **思考模式把摘要写进 `reasoning_content`**，`content` 是空的——摘要静默丢失。
   已加 `thinking: disabled` + `content or reasoning_content` 兜底。

## 还没解决：注入了但被裁成 0

实测：

```
digest present: True      ← 库里确实有
degraded: False
blocks: persona 357 | card 52 | pinned 0 | memory 92 | dynamic 0 | anchor 11
```

摘要写进了 C 段（pinned），但 C 段最后 token 是 **0**——被分项/总预算裁剪吃掉了。
所以摘要"生成了、也放了、但没进 prompt"。下一轮要查 `budget_of["pinned"]` 的算法，
给摘要留一个不可裁的额度（比如单独一个 block 或提高 C 段下限）。

## 召回现状

| 口径 | 结果 |
| --- | --- |
| 严格原词（20 题人工标注） | 5 → **6/20（30%）** |
| 模型判定（correct+partial） | **6/20（30%）** |

摘要目前只贡献 +1 题，因为它基本没被真正注入。修好注入后再测才有意义。
