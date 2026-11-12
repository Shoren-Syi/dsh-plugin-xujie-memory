# 更新日志

## 1.1.19

- 遗忘 ≠ 丢失：`retrieve.min_weight` 真正生效（日常过滤），显式回忆（以前/之前/当时）穿透
- 跨会话身份边界：事实/特征跨会话继承（`retrieve.cross_session`，默认开），事件/计划/存疑仍按会话隔离
- tests: `test_m77_forgetting.py` 3 例、`test_m78_cross_session.py` 3 例

## 1.1.18

- 清理：`Heartbeat` 不再每次 tick 创建从未调用的 `Escalator`，改为按需属性
- 至此 recompute / consolidate / backup / heartbeat 全部接入运行时

## 1.1.17

- 心跳真正接入（以前 `Heartbeat` 只在 CLI 里被引用，插件从未启动）
  - 内核按 `heartbeat.interval_seconds`(60s) 跑六步：plans/doubts/mood/tier/consolidate/curiosity
  - 每日备份顺带在心跳里检查；单步失败不影响其它
  - 结构上无 LLM 调用，挂机不额外花钱
- 修复：`TickReport` 无 `llm_calls`（改用 `hb.counts`）、系统状态键 `_sys`
- tests: `test_m75_heartbeat_loop.py` 3 例

## 1.1.16

- 备份接线到插件（以前只有 CLI）
  - 维护循环每天自动备份一次（保留最近 7 份）
  - `POST /backup` / `GET /backups` / `POST /restore`；面板「状态」页备份区
  - 恢复保留 `.pre-restore-*.bak` 后悔药，并提示重启 DSH
- tests: `test_m74_backup.py` 3 例

## 1.1.15

- 接线两个"只在 CLI 里有"的能力：权重/tier 重算、夜间巩固
  - 内核后台任务默认每天跑一次，纯本地不调模型
  - 面板「状态」页新增「立即维护」按钮（`POST /maintain`）
  - 配置 `maintenance.*`；失败只记 warning，不影响内核
- 新增 `scripts/audit-modules.py`（运行时可达性审计）与 `reports/R-52`
- tests: `test_m73_maintenance.py` 3 例

## 1.1.14

- 修复：`/event` 接受空会话名/超长会话名（会产生"无归属"记忆）
  - `session` 限制 1~200 字符，非法值 422
- 已验证：无效 API key 下内核不崩、摘要失败静默重试
- tests: `test_m61` 补 1 例

## 1.1.13

- 修复：超长问题串拖慢装配（5 万字 50.9ms → 6.8ms）
  - 入口截断：pack 取前 4000 字做检索判断，l2_search 再兜底 2000 字
- tests: `test_huge_query_is_bounded`

## 1.1.12

- 修复：原话兜底导致检索变慢（3000 条压测 p95 13.4 → 34.8ms）
  - 根因：`LIKE '%词%'` 不命中时全表扫，10 万条 turn 单次 40ms
  - 修法：只扫最近 5000 条 turn（走 (session, idx) 索引范围扫描）
  - 结果：不命中查询 40.5ms → 1.65ms；压测检索 p95 回到 16.9ms
  - 顺带修边界差一（`>=` → `>`）
- tests: `test_m63_raw_turns.py` 补 2 例

## 1.1.11

- 写入幂等 + 本地合并（对标 Hy-Memory 的 reconcile，零额外模型调用）
  - 同一段原话重放：跳过，不改任何东西（幂等）
  - 同一件事换说法：把新关键词/更高 importance 合并进旧事件，raw/summary 不动
  - 合并后的关键词补进检索索引，保证可召回
- 新增 `stats.events_merged` 计数
- tests: `test_m68_merge.py` 3 例

## 1.1.10

- 代码对话用「工程记忆」提示词（对标 Hy-Memory 的 coding 链路）
  - 本地正则判断代码窗口（零调用），只在该窗口追加补充规则，不替换基础 prompt
  - 记：改了什么/为什么/坑/决策，路径与命令逐字保留；importance 打分标准单列
  - 合成对照：同一段代码对话 0 条 → 1 条，且抓到文件路径
- 配置 `extract.coding_prompt`（默认开）
- tests: `test_m67_coding_prompt.py` 3 例

## 1.1.9

- 新增「原话兜底」层（对标腾讯 Hy-Memory 的 L1_RAW）：事件没命中时，从 turns 表捞原话进上下文
  - 只按关键词匹配、最多 3 条、独立预算，不挤事件；零模型调用
  - A/B：143 题严格命中 55 → **60（+3.5pp）**
  - 二级「最近两条原话」实测无增益，默认关闭
- 新增脚本：`build-corpus.py`（从导出 zip 重建语料）、`ab-raw-turns.py` / `ab-raw-measure.py`（A/B）
- tests: `test_m63_raw_turns.py` 4 例

## 1.1.8

- 修复：窗口缩窄时，会话头部工具条（智能体团队/费用明细/标签/移动到工作区/删除会话等）
  互相压住并盖到标题上——宿主 headerActions 没有 flex-wrap，插件客户端补一条可换行兜底
- 兜底只作用于 `[class*="_headerActions"]`，不改其它布局，重复注入去重
- 回归测试：`test/header-shim.test.mjs`（断言换行/省略/去重，且不误伤别的选择器）

## 1.1.7

- 统一版本号：包、内核 `__version__`、README、RELEASE、CHANGELOG、面板显示全部为 1.1.7
  （此前出现过包 1.1.5 / 内核 1.0.0 并存，健康检查显示旧版本，容易让人以为装错）
- 发布文档重写：安装/升级/回退/验收/实测效果/已知限制一节说清
- 一致性测试：包版本 == 内核版本、README 安装命令版本、RELEASE/CHANGELOG 必须提到当前版本

## 1.1.0 - 1.1.6（同日修复）

- 1.1.6 版本对齐 + README 实测数据与已知限制
- 1.1.5 坏配置文件不再拖垮启动；`/event` 只收 user/assistant
- 1.1.4 QQ 配置热生效抽成 bridge 并端到端计时；Windows 迁移锁共享冲突修复
- 1.1.3 待补写回合落盘（重启不丢）
- 1.1.2 双实例同库冷启动两个崩溃点（迁移竞态、WAL pragma 顺序）
- 1.1.1 写库失败不再丢回合（有界重试队列）
- 1.1.0 keep-alive 与 busy_timeout 撞车导致的丢数据竞态

# CHANGELOG

## 1.0.0

- 叙界记忆 1.0 使用海马体内核；npm 包继续使用 dsh-plugin-xujie-memory。
- 旧版数据库不自动迁移；升级前请备份，必要时可回退 0.7.2。

## 0.2.0 · DSH 插件形态 + 预筛升级（M29）

### 新增：DSH 插件（插入即用）

- `package.json` + `cordis.patch.yml` + `lib/index.js`（宿主半）：装上重启即可，
  自动拉起内核、订阅 `session/event`、`systemPrompt.section` 注入记忆。
- 包内 Python 运行时（embeddable 3.12.10 + pydantic / pyyaml / fastapi / uvicorn / httpx），
  用户零安装；`runtime/win32-x64/` 相对路径写入 `_pth`，装到任何目录都能 import。
- 抽取 key 自动解析：配置 → `HIPPO_API_KEY` / `DEEPSEEK_API_KEY` → DSH 凭据 → `~/.hippo/config.yaml`。
- 内核子进程带 `--parent-pid`，宿主退出不留孤儿；日志 `~/.hippo/kernel.log`（1MB 轮转）。

### 改进：预筛（M29）

- 新增 `extract.prescreen_mode`：`rules`（原行为）/ `heuristic`（默认）/ `off`。
- heuristic = 规则命中即放；不中时按「纯寒暄/纯评价挡、带数字短句放、≥8 字放」判断。
- 真实对话 120 轮对照：规则预筛 1 事件 → heuristic **29 事件**（关预筛 33 事件），
  花费还更低（¥0.156 vs ¥0.195）。
- 多话题真实评测（10 话题 / 20 查询）：**20/20 = 100% 命中**。

### 修复

- DSH 内核 `/extract` 调用参数 bug（`force` 关键字）。
- `/assemble` 改走线程池：20 并发 p95 从 712ms → **394ms**，吞吐 41 → 69.6 req/s。
- `_pth` 隔离模式导致 `PYTHONPATH` 失效（embeddable Python），改为相对路径。

### 测试

| 层 | 数量 | 结果 |
|---|---|---|
| Python（pytest） | **1173** | 0 failed / 0 skipped |
| Node（插件壳 + 真内核往返） | 7 | 全过 |
| 多话题真实评测 | 20 查询 | 100% |
| 压测（20 并发 × 30 轮 × 2） | 600 请求 | 0 错误，p95 394ms |

## 0.1.0 · 首个可分发版本（M28）

M0–M27 全部里程碑（1137 条测试全绿）+ M28 发布工程。

### M28.1 打包与 init

- `pip install hippocampus-deepseek` 一次装齐（`fastapi` / `uvicorn` / `httpx`
  已并入主依赖；`[server]` extra 保留为空壳兼容旧命令）。
- 干净环境验收：全新 venv → install → `hippo init` → `hippo serve` 跑通，
  `hippo doctor` 8/8，`pip uninstall` 后无残留。
- 首次 `serve` 打印 base_url 引导；输出重定向时也可见（显式 flush）。
- 修复：中文 Windows（GBK 控制台）下 `doctor` 的 ✅/❌ 触发
  `UnicodeEncodeError` 崩溃 → 全部改 ASCII 标记（`[OK]` / `[FAIL]`）。

### M28.2 文档与示例

- README 重写：30 秒上手、示例索引、备份恢复、**9 条 FAQ**。
- `examples/` 三个可执行示例：
  `openai_sdk.py`、`curl.sh`、`sillytavern.md`。
- 从零计时（自动化）：`venv → pip install → init → serve → /health` **27.0s**
  （pip 缓存热；冷缓存约 +30s）。

### M28.3 灰度与回滚

- 新增 `hippo backup`：`VACUUM INTO` 一致快照到 `<库目录>/backups/`，
  保留最近 7 份（`--keep` 可调）。
- `hippo restore --latest` / `--backup <path>`：整库恢复；旧库改名
  `mem.db.pre-restore-*.bak` 留档。`restore --id`（归档库拉单条）语义不变。
- `serve` 启动时自动补当日备份（失败只告警，不拦服务）。
- `ROLLBACK.md`：代码回滚 / 数据回滚 / 完全重来三条路径。
- 旧版本读新库验证：迁移只加不改，未知列/表不影响旧代码读取（有测试守门）。

## 里程碑索引

| 编号 | 名称 |
|---|---|
| M0 | 骨架 / 录制回放 |
| M1 | 数据库（schema / FTS / DAO） |
| M2 | 代理（转发 / 流式 / 会话） |
| M3 | 落库 |
| M4 | 预筛 |
| M5 | 抽取 |
| M6 | 三级表示（关键词 / 摘要 / 原文不可变） |
| M7 | 实体与角色卡 |
| M8 | 事实与矛盾 / 时间解析 |
| M9 | L1 检索 |
| M10 | L2 倒排 + 排序 |
| M11 | L3 原文下钻 |
| M12 | 话题切换 |
| M13 | pack 装配 |
| M14 | 权重与分层 |
| M15 | 巩固 / 合并 / 归档 |
| M16 | 注入深度 / 钉住 |
| M17 | 三重时间戳 |
| M18 | 存疑池 |
| M19 | 计划 |
| M20 | 六维情绪 |
| M21 | 调参 |
| M22 | 放弃分级 |
| M23 | 账簿 |
| M24 | 心跳 |
| M25 | 好奇 |
| M26 | 评测集与六项指标 |
| M27 | 性能与成本 |
| M28 | 发布 |
