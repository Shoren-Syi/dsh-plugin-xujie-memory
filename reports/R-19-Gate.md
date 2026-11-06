# R-19-Gate · M19 收口：计划记忆 + 调度

> M19 Gate　测试 893 → **942**（+49）　本轮花费 **¥0**（全程离线）

## 一、DoD

> 说"明天 8 点叫我"，次日 8:00±60s 收到主动消息。

链路已全通（离线验证）：
"明天8点" → parse_due 写入时一次换算成 epoch（±0s，没有运行时重算）
→ scheduler.tick 到点触发（误差上界 = tick 周期 60s，sim_plan 实测
delay=0s）→ pack 的 [计划] 段带进下一轮上下文（原话 + missed 说明）。
"主动消息"的最后一步（宿主把模型输出推给用户）依赖 M24 心跳统一
调度，本里程碑交付到"上下文已就绪"为止——如实交代，不虚报。

## 二、验收账

| 项 | 结果 |
|---|---|
| 20 种表述解析 | **20/20**（手册 ≥95%），期望值全部日历手算 |
| 写入时一次换算 | ✓ 库行无相对词、无运行时表达式 |
| recur 推进 | daily +1 天 / weekly +7 天，复用同一行，二次触发验证 |
| 误差 ≤60s | tick 周期 60s，sim_plan delay=0s |
| 并发只触发一次 | 10 次 tick 全跑，rowcount 仲裁 wins=1 |
| missed 补触发 | grace 5 分钟外标 missed=1 且仍触发，说明"晚了 X 分钟" |
| 模糊反问 | 解析失败落 doubt（走 M18.2 限流进上下文），不落 plan |
| defer 封顶 | 3 次后强制发，第 4 次 defer 被拒 |

## 三、修掉的坑

1. **mark_plan_fired 会洗掉 missed**：sweep 标完 missed=1，tick 用
   默认参数触发就把 flag 抹了——"这条晚了"的说明凭空消失。tick
   显式传 `missed=bool(p.missed)`。
2. **注入与触发的时序差**：fired=1 之后 due_plans 查不到，pack 靠
   fired_at 的 10 分钟窗口注入（双通道：已触发 + 未触发兜底）。
3. **周期计划落过去的点**："每周三下午3点"在周三 16 点说，parse_due
   按周期推到下周三——落一个已过去的点会被 tick 立刻触发，等于
   "你刚说完它就响"。

## 四、M19 一句话

> 计划 = 写入时一次换算的绝对时间戳 + 一个不调模型的 60s tick +
> 一段带原话的注入。三段各自都简单，正确性全在接缝上：
> 换算只做一次、触发只发生一次、注入只进一个窗口。

## 五、M19 Gate 交代（给 M20+）

1. **[计划] 注入约 30~50 token**（三行格式），dynamic 段 150 预算
   下与 [存疑]（25~35）并存时已接近满载——M20 情绪段（[状态] 一行
   约 15 token）加入后，超预算先丢情绪（M13.1 次序：存疑 > 计划 >
   情绪），不需要改代码，但 M20 收口要验证丢失次序真实生效。
2. **C 段 192/200 撞线仍未清**（M16 留）：M18+M19 的注入都在
   dynamic 段不占 C 段，账面安全；M26 评测时重测。
3. **tick 的 on_fire 回调是同步接口**（异步回调自动 create_task），
   M24 心跳接它时注意 loop 归属：run_forever 里已保证有 loop，
   宿主另起线程调 tick 的话要自己带 loop。
4. **defer 的"模型判断不适合打扰"目前是宿主决策**：插件层只提供
   defer_plan 的账（封顶 3 次）。M24 心跳做主动消息时，把"用户正在
   聊别的事"的判断接到 defer 上。
5. **pack 计划段用 hasattr 探测 recent_fired_plans**（兼容旧 Store
   的测试替身）——M20 之后若加第三个动态源，考虑把动态段重构为
   注册制，别再 if 叠 if。

## 六、账目

| 项 | 数 |
|---|---|
| 测试 | 893 → 942（M19.1 +29、M19.2 +12、M19.3 +8） |
| 新增代码 | scheduler.py · sim_plan.py · timeparse.parse_due |
| 改动 | persist.py（parse_due + 反问落 doubt）· pack.py（[计划] 段 + _fmt_plan）· store.py（sweep/advance/defer/recent_fired_plans/mark_fired 加 now） |
| 花费 | ¥0 |
