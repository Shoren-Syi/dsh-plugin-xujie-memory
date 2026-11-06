# CHANGELOG

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
