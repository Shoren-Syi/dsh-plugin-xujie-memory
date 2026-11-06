### R-28 Gate · 发布门（P6 Gate）

| 项 | 内容 |
|---|---|
| 里程碑 | M28 发布 |
| 子任务 | P6 Gate（发布门） |
| 日期 | 2026-11-06 |
| 执行人 | 哦鲸鲸（DSH agent） |
| 环境 | Python 3.12.14 / SQLite 3.53.1 / Windows 10.0.26300 |

---

#### 1. 交付物

| 文件 | 说明 |
|---|---|
| `hippo/core/backup.py` | 备份/恢复编排（新增） |
| `hippo/core/store.py` | `Store.backup_to()`（新增方法） |
| `hippo/cli.py` | `backup` 子命令、`restore --latest/--backup`、serve 引导语/flush、doctor ASCII |
| `hippo/tools/import_conversations.py` | 真实导出导入工具（新增） |
| `hippo/data/config.default.yaml` | prescreen 开关说明 |
| `pyproject.toml` | server 依赖并入主依赖 |
| `README.md` | 重写（9 FAQ + 示例 + 备份恢复） |
| `examples/` | `openai_sdk.py` / `curl.sh` / `sillytavern.md`（新增） |
| `tests/test_m28_3_backup.py` | 8 条备份/恢复测试（新增） |
| `ROLLBACK.md` / `CHANGELOG.md` | 发布文档（新增） |
| `reports/R-28.1/.2/.3.md`、`R-28.4-real-data.md` | 验收报告（新增） |

#### 2. 验收项（发布门三要件）

| # | 要求 | 实测 | 结论 |
|---|---|---|---|
| 1 | M26 六项指标达标 | 召回 0.980 / 573 token/轮 / 月成本 ¥1.08–27.04 / pack p95 19.3ms / 20 并发 p95 69ms / RSS 76MB —— **引用交接包 R-26-final 与 R-27.1，本次未重跑** | ✅（引用） |
| 2 | M27 压测通过 | p95 69ms @20 并发、0 次 `database is locked`（R-27.1） | ✅（引用） |
| 3 | M28.1 干净环境可装 | 全新 venv：install → init → doctor 8/8 → serve /health 200 → uninstall 无残留（R-28.1） | ✅（本次实测） |

补充验收：

| # | 要求 | 实测 | 结论 |
|---|---|---|---|
| 4 | 全量测试 | **1145 passed / 0 failed / 0 skipped**（44.4s） | ✅ |
| 5 | 真实数据有效性 | 3 段 / 120 轮真实对话：关预筛 33 事件、召回 ITX 6 命中 / GPT 5 命中（R-28.4） | ✅（补充） |

#### 3. 测试记录

```
$ python -m pytest --junitxml=junit.xml -q
TESTS=1145 FAILURES=0 ERRORS=0 SKIPPED=0 TIME=44.371

$ python -m hippo.cli doctor --dir <fresh>
8/8 通过

$ python -m hippo.cli backup --dir <fresh> && python -m hippo.cli restore --latest --dir <fresh>
备份完成 → .../backups/mem-20261106-120047.db
已从备份恢复 → .../mem.db
```

#### 4. 关键数据

| 指标 | 值 |
|---|---|
| 发布版本 | 0.1.0 |
| 测试 | 1145（1137 交接 + 8 新增） |
| 从零 onboarding（自动化） | 27.0s |
| 真实数据花费 | ¥0.2061（测试余额 ¥10） |
| 包内文件 | hippo/ 83 文件 + tests/ + tools/ + reports/ + 手册/施工记录 |

发布前需要人工确认的项（非代码问题）：
- `sillytavern.md` 未在真机 SillyTavern 验证（本机没装）；
- M26/M27 指标引用交接报告，未在本机重跑（压测要建 8000/10 万条数据，耗时不计入 M28）。

#### 5. 遗留问题

| # | 问题 | 影响 | 处理 |
|---|---|---|---|
| 1 | 默认规则预筛对真实对话覆盖不足（95% 窗口被挡） | 高（开箱观感） | 配置注释 + README 已提示；M29 换预筛算法 |
| 2 | 超长 user 消息（整篇备忘录）抽取输出被 max_tokens 截断 | 中 | 按设计降级不阻塞；backlog：长文本先摘要 |
| 3 | 每日自动备份只在 serve 启动时检查 | 中 | backlog：挂 M24 心跳 tick |
| 4 | SillyTavern 示例未真机截图 | 低 | backlog |

#### 6. 降级说明

Gate 的 M26/M27 两项为**引用交接包既有报告**而非本机重跑：M28 的范围是交付工程，
重跑 8000/10 万条数据的压测与 200 轮评测不在本里程碑内。若要求"发布前复验全部
指标"，需要额外 1–2 小时机器时间（命令与基线见 R-26-final / R-27.1）。

#### 7. Gate 结论

**有条件通过**，允许发布 0.1.0。条件：第 6 节所述的 M26/M27 复验按需在发布前
补跑（或接受引用既有报告）；预筛问题如实写进发布说明。
