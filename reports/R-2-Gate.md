# R-2-Gate M2 代理透传 · 里程碑验收

| 项 | 内容 |
|---|---|
| 里程碑 | M2 代理透传 |
| 子任务 | M2.1 / M2.2 / M2.3 |
| 日期 | 2026-11-04 |
| 结论 | **通过** |

## 1. 交付清单

| 文件 | 行数 | 说明 |
|---|---|---|
| `hippo/adapters/proxy.py` | 322 | OpenAI 兼容路由、上游转发、SSE 中继、埋点 |
| `hippo/adapters/session.py` | 77 | 三级会话识别 |
| `hippo/log.py` | 197 | JSON 行日志 |
| `hippo/core/telemetry.py` | 127 | Span 四时间点 + 成本 |
| `hippo/testing/cassette.py` | 177 | 录制回放 |
| `hippo/cli.py` | +45 | `hippo serve` |
| `tools/smoke.py` | 118 | 端到端自动验证 |
| `tools/fake_upstream.py` | 76 | 桩上游 |
| `tools/live_check.py` | 74 | 真连体检（花钱） |
| `tests/*` | 39 例 | 见下 |

## 2. 测试总览

```
$ python -m pytest tests/ --disable-warnings
........................................................................  [ 87%]
..........                                                                [100%]
82 passed in 8.03s
```

| 模块 | 用例数 | 说明 |
|---|---|---|
| M1（既有） | 43 | 库结构、FTS、DAO |
| M0.3 日志 | 4 | JSON 合法性、上下文还原、异常入日志 |
| M0.3 录制回放 | 6 | 录/放/未命中/换 key/流式原样 |
| M2.1 路由 | 10 | 透传、401、429、502、400、埋点 |
| M2.2 流式 | 10 | 逐行 yield、真 TCP、usage、错误透传 |
| M2.3 session | 9 | 三级识别、稳定性、隔离 |

`make smoke` 另跑一遍真网络链路（真 uvicorn + 真 TCP），输出 `SMOKE PASS`。

## 3. 现在能做什么

```bash
hippo init
hippo serve                    # → http://127.0.0.1:8787

# 任何 OpenAI 兼容客户端
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-...")
```

此时它还是一个"纯透传"的空壳——不改写、不记忆、不加延迟。
但管道已经通了，且每一轮都留下了可查的账（日志 + metrics）。

## 4. 偏差登记

| # | 偏差 | 理由 | 风险 |
|---|---|---|---|
| 1 | M2 阶段唯一改写：注入 `stream_options.include_usage` | 不加则 usage 恒为 0，成本统计全废 | 低。已验证不覆盖客户端自有 stream_options |
| 2 | `deepseek-reasoner` 跳过上述注入 | 它参数集不同，多塞字段有 400 风险，收益仅是"推理模型也可统计" | 低。reasoner 不作为角色路径（手册已锁 `deepseek-chat`） |
| 3 | 不用 respx，用 `httpx.MockTransport` | 少一个依赖，能力等价 | 无 |
| 4 | 不用 `TimedRotatingFileHandler`，自写按天切分 | 前者当前文件名固定为 `hippo.log`，与约定不符 | 无 |
| 5 | `t0~t3` 用毫秒、`metrics.ts` 用秒 | 秒级看不出组包开销；ts 需与 events 表同轴 | 无（已在文档写明） |
| 6 | 未实现 `request.is_disconnected()` 显式检测 | Starlette 在客户端断开时会取消生成器，`finally` 里的 `aclose()` 已覆盖 | 低 |

## 5. 风险与待办

| # | 项 | 何时必须处理 |
|---|---|---|
| 1 | `Store.events_of_entity()` 用 `entities LIKE '%id%'`，实体 1 会误命中 11/21 | **M9 开工前**，建 `event_entities` 关联表（迁移 002） |
| 2 | 代理尚未落库对话（turns 表空） | M3 |
| 3 | 尚未接入 pack（t1≡t0） | M13 |
| 4 | 无并发压测 | M27 成本与稳定性 |
| 5 | 真连体检需人工提供 key | M28 发布前跑一次 |

## 6. Gate 判定

| 条件 | 状态 |
|---|---|
| 三个子任务全部 ✅ | 是 |
| 全量回归无失败 | 是（82 passed） |
| 端到端 smoke 通过 | 是 |
| 遗留问题已登记且有归属里程碑 | 是 |
| 无"为了过验收而放宽标准" | 是（流式验证从 ASGITransport 换成真 TCP，是**加严**不是放宽） |

**通过，允许进入 M3 会话落库。**
