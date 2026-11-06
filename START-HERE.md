# 接手说明 · START HERE

> 这份文件是写给**下一个接手的人或 AI**的。读完这一份，你应该能在 30 分钟内
> 判断出"这东西是什么、做到哪、下一步干什么、哪些地方不能碰"。
>
> 详细施工过程在 `施工记录.md`（5726 行，按时间正序；不用读全，按 M 编号检索即可）；
> 原始设计与验收标准在 `海马体插件-完整手册.md`；每个里程碑的实测数据在 `reports/`。

---

## 0. 三十秒

**这是一个给 DeepSeek 用的旁路记忆中间件。** 它跑在本地，是一个 OpenAI 兼容的
HTTP 代理：客户端把 `base_url` 从 `https://api.deepseek.com` 改成
`http://localhost:8787/v1`，其余一行代码不用动，AI 就"记得住事"了。

- 它**不改模型、不改客户端、不训练**，只做两件事：把该记的写进本地 SQLite，把该想起来的拼进这一轮的 prompt。
- 全部逻辑在**一个 Python 包**里（`hippo/`，61 个文件 19,620 行），依赖只有 `pydantic / pyyaml / tzdata`，服务端再加 `fastapi / uvicorn / httpx`。
- 数据全在 `~/.hippo/`（`config.yaml` + `mem.db` + `persona.md`），不上传任何第三方。

**当前状态：M0–M27 全部完成，M28（发布）未开工。测试 1137 条全绿。**

---

## 1. 先跑起来（10 分钟，不花钱）

```bash
cd <解压目录>
pip install -e ".[server,dev]"        # 只装依赖，不联网跑模型

python -m pytest -q                    # 期望：1137 passed（约 2–4 分钟）
```

跑通测试只证明"代码没坏"。要证明"能用"，走一遍三步：

```bash
python -m hippo.cli init --dir /tmp/h1        # 建 config/persona/mem.db，跑 17 个迁移
python -m hippo.cli doctor --dir /tmp/h1      # 期望：8/8 通过
python -m hippo.cli serve --dir /tmp/h1 --port 8787
```

> `doctor` 手册里写的是 5 项，实现成了 8 项（多了 FTS5 trigram、预筛词表、抽取 prompt
> 三项）——**这是升级不是偏差**，别为了对齐手册把它删回去。

零成本验证整条链路（假上游 + 真代理 + 真 TCP）：

```bash
python -m hippo.tools.loadtest --requests 100 --concurrency 20 --events 8000
```

期望：`p50≈59ms / p95≈69ms`、错误率 0%、`database is locked` 0 次。

要真连模型，得在 `~/.hippo/config.yaml` 填 `deepseek.api_key`。**这会花钱**，
一次完整评测约 ¥0.1（跑 200 轮对话 + 抽取）。

> **这个包本身已经验证过**（打包时在干净 venv 里做了完整回归）：
> `pip install` 成功 → `hippo --version` 正常 → `init` 跑完 17 个迁移 →
> `doctor` 8/8 → 全量测试 1137 全过 → `check_fixture` / `eval_timeparse` 可运行。
> 所以你解压后如果第一步就崩，问题在环境（Python 版本、缺 fastapi），不在包。
>
> 注意：`hippo.tools.*` 和 `tools.*` 里有几个脚本要读 `tests/fixtures/`，
> **必须在源码根目录跑**（`cd` 到解压目录），装到 site-packages 后跑会找不到语料。

---

## 2. 现在在哪一步

| 阶段 | 里程碑 | 状态 |
|---|---|---|
| P0 地基 | M0 骨架 / M1 数据库 / M2 代理 / M3 落库 | ✅ |
| P1 写入 | M4 预筛 / M5 抽取 / M6 三级表示 / M7 实体 / M8 事实矛盾 | ✅ |
| P2 读取 | M9 L1 / M10 L2 / M11 L3 / M12 话题 / M13 pack | ✅ |
| P3 动力学 | M14 权重 / M15 巩固 / M16 注入深度 / M17 三重时间戳 | ✅ |
| P4 临场 | M18 存疑 / M19 计划 / M20 情绪 / M21 调参 | ✅ |
| P5 自运转 | M22 放弃 / M23 账簿 / M24 心跳 / M25 好奇 | ✅ |
| P6 交付 | M26 评测集 / M27 性能成本 | ✅ |
| P6 交付 | **M28 发布** | ⬜ **没开工，这是你的活** |

关键指标（全部有报告，可复现）：

| 指标 | 值 | 出处 |
|---|---|---|
| 测试 | 1137 passed | `python -m pytest --tb=no` |
| 召回（200 轮语料，50 针） | **0.980** | `reports/R-26-final.md` |
| 每轮 token（hippo 模式） | 573 | `reports/R-26.2.md` |
| 月成本（20/100/500 轮每天） | ¥1.08 / ¥5.41 / ¥27.04 | `reports/R-27.2.md` |
| pack 延迟（文件库 8000 事件） | p50 12.4ms / p95 19.3ms | `reports/R-27.1.md` |
| 20 并发 p95 | 69ms（直连 30ms） | `reports/R-27.1.md` |
| 进程 RSS（含 8000 事件） | 76MB | `reports/R-27.1.md` |

---

## 3. 五条硬约束（用户定的，不可协商）

1. **能落地** —— 每个里程碑必须能跑、有测试、有数字。不接受"设计上可行"。
2. **不许改主体设计** —— 五层记忆、六维情绪、多巴胺/懊悔、放弃分级、心跳、好奇，这五套机制的**结构**不能动。实现细节可以改，章节可以补，但不能推翻重来。
3. **逻辑通** —— 任何一个参数、任何一条规则，要能回答"为什么是这个值"。答不上来就说明还没想清楚。
4. **无感、少依赖、自然涌现 + 定向检索** —— 用户不该感知到"插件在工作"；不额外要求用户维护；记忆靠日常对话长出来，检索靠实体/时间/关键词定向捞，不做全量向量扫描。
5. **成本低** —— 模型调用只允许出现在两个地方：**抽取**和**心跳升级**。其余全部规则/统计实现。当前 573 token/轮是硬指标，涨上去要解释。

---

## 4. 目录地图

```
.
├── START-HERE.md              ← 你正在读的
├── README.md                  ← 给"用户"看的（M28.2 要补到 8 条 FAQ）
├── 海马体插件-完整手册.md       ← 原始方案 + 施工规范 + 每个 M 的 DoD（164KB）
├── 施工记录.md                 ← 5726 行施工日志，按 M 编号可检索
├── pyproject.toml             ← 打包配置（entry point 已配好）
├── Makefile                   ← make test / smoke / serve / verify / journal
├── hippo/
│   ├── cli.py                 ← 全部子命令（init doctor serve stats budget curiosity …）
│   ├── config.py              ← pydantic 配置模型，带区间校验
│   ├── adapters/
│   │   ├── proxy.py           ← FastAPI 代理：转发 + 注入 + 抽取调度 + 心跳接线
│   │   └── session.py         ← 会话 id 解析
│   ├── core/                  ← 37 个模块，见下表
│   ├── data/
│   │   ├── config.default.yaml  ← 带注释的默认配置（用户配置的模板）
│   │   ├── lexicon.yaml         ← 预筛词表（57 条规则）
│   │   ├── prompts/*.md         ← 抽取 prompt
│   │   └── migrations/001..017  ← 单向迁移，只加不改
│   └── tools/                 ← 评测/仿真/压测（随包发布，`python -m hippo.tools.xxx`）
├── tests/                     ← 82 个文件 14,118 行，1137 用例
│   ├── fixtures/              ← dialog_200.jsonl + .pins.json（200 轮评测语料）
│   └── fake_deepseek.py       ← 假上游，所有代理测试都用它
├── tools/                     ← 一次性探针/调参脚本（开发期产物，不随包安装）
└── reports/                   ← 109 份验收报告 R-<M>.<n>.md + R-<M>-Gate.md
```

### `hippo/core/` 按职责分组

| 组 | 模块 |
|---|---|
| 存储 | `store.py`（唯一 DAO，3000+ 行）、`db.py`、`migrate.py`、`schema.py`、`models.py` |
| 写入 | `prescreen.py` 预筛、`extractor.py` 调模型、`keywords.py`、`summary.py`、`encode.py`、`persist.py` 落库编排 |
| 表示 | `entities.py` 实体归一、`facts.py` 事实与矛盾、`card.py` 角色卡、`synonyms.py` 别名 |
| 读取 | `retrieve.py` 三层下钻、`rank.py` 排序、`drill.py`、`topic.py` 话题切换、`pack.py` 装配（**唯一出口**） |
| 动力学 | `weight.py` 权重、`dynamics.py` 衰减、`consolidate.py` 夜间巩固、`pin.py` 钉住 |
| 临场 | `doubt.py` 存疑池、`timeparse.py` 时间解析、`affect.py` 六维情绪、`scheduler.py` |
| 自运转 | `giveup.py` 放弃、`ledger.py` 账簿、`heartbeat.py` 心跳、`curiosity.py` 好奇 |
| 支撑 | `budget.py` 预算熔断、`telemetry.py` 埋点、`turns.py` 轮次、`lexicon.py` |

---

## 5. 不变量（改了就前功尽弃）

这些是 27 个里程碑里反复踩坑换来的。**动它们之前先读对应报告，别凭直觉。**

### 5.1 只有一个出口

**往 prompt 里塞东西的唯一路径是 `core/pack.py` 的 `pack()`。**
适配器、CLI、任何新功能都不许自己拼字符串塞进 messages。
`pack()` 返回的 `PackResult` 里每一段都是 `Block(used, tokens, text)`，
`render()` 用固定分隔符拼接、**无空行、无随机顺序**——这是前缀缓存能命中的前提。

### 5.2 状态回填错一步 = 下一轮静默失忆

`last_pack_d` / `last_pack_turns` 这类状态是"上一轮装了什么"的账本，
下一轮靠它判断能不能复用 D 段（记忆段）。写错一次，下一轮会以为"已经装过了"
而装一个空块，**表现是 AI 突然失忆，日志一切正常**。

所以：**任何降级/异常/早退分支，一律不写 `last_pack_*`。** `test_m27_3_breaker.py`
里有一条专门守这个。

### 5.3 迁移单向，只加不改

`hippo/data/migrations/NNN_*.sql` 按序号执行，`migrate()` 记录已执行版本。
- 只能新增文件，**不许改已发布的迁移**（别人的库已经执行过，改了不会重跑）。
- 新列必须带默认值或可空，否则旧库升级会炸。
- 加索引用 `CREATE INDEX IF NOT EXISTS`；部分索引用 `WHERE`（见 `017_pinned_index.sql`）。

### 5.4 DAO 规则：核心代码不许裸写 SQL

`tests/test_m1_3_dao.py` 会扫源码，核心模块里出现 `conn.execute` 直接红。
原因不是洁癖：**只读模式、审计、连接复用三个横切逻辑都挂在 `Store` 上**，
绕过它就绕过了闸门。

要加查询 → 在 `store.py` 加方法 → 写测试。这个约束救过好几次命
（M27 的 `cost_report` 就是被它抓出来改成 `store.token_sum(session=)` 的）。

### 5.5 只读闸门在 `Store.tx()` 最前面

探索模式（`explore_mode`）下所有写操作必须在**事务入口**被拦，
包括 `in_transaction` 的嵌套分支。别在每个写方法里各写一遍——会漏。

### 5.6 预算是钱，账必须落盘

额度计数**不能放内存**：重启即清零 = 用户重启服务就能无限刷。
每轮两次聚合查询（`metrics` + `extract_runs`），走索引亚毫秒。
`budget.reset` 只写时间戳，**不删 metrics**——删了"这个月花了多少"永久失真。

### 5.7 模型调用只有两处

抽取（`extractor.py`）和心跳升级（`heartbeat.py` 的 `Escalator`）。
其余全部规则实现。新功能想调模型，先说服自己"规则真的做不到"。
升级还有三道闸：白名单常量（不是配置）、单次 300 token、每日 10 次。

### 5.8 抽取失败绝不能影响对话

代理的转发路径和抽取路径是分开的：抽取是后台 task，失败只记日志 + `extract_runs`
状态位，**不重试、不阻塞、不改变已返回的响应**。

### 5.9 时间：存秒级 `ts`，调度用单调钟

库里所有时间戳是 int 秒（`ts`）。请求内比较用 `ts`，**定时器间隔用
`time.monotonic()`**——用墙钟算 `next_at += interval` 会在 NTP 校时后漂移。

### 5.10 prompt 前缀必须稳定

`persona → 钉住 → 记忆 → 时间锚` 顺序固定，块内顺序按分数+id 稳定排序。
任何"随机打散"或"按当前时间插一句"都会让前缀缓存失效（省 54% 就没了）。

---

## 6. 施工方法（照着做，别自创）

每个里程碑的固定套路，27 次都是这么走的：

1. **读手册**：`grep -n "^### M28" 海马体插件-完整手册.md`，把那一节读完（含 DoD、验收表、测试命令、预期结果）。
2. **先写测试**，红着。测试文件命名 `tests/test_m28_1_xxx.py`，一个子任务一个文件。
3. **实现**，让测试绿。中途发现设计漏洞 → 记下来，**不要偷偷改设计**。
4. **跑全量**：`python -m pytest --tb=short`，必须 0 failed。任何红都必须修，不许 skip。
5. **出报告**：`reports/R-28.1.md`，模板在手册「二、验收报告模板」，七节：交付物 / 验收项 / 测试记录 / 关键数据 / 遗留问题 / 降级说明 / Gate 结论。
6. **追加施工记录**：往 `施工记录.md` 末尾追加一章（`<!-- APPEND_MARKER -->` 之前），必须包含：
   - 开工判断（为什么这么拆）
   - 决策（每个岔路口选了什么、为什么）
   - 施工过程（关键代码在哪、怎么验证）
   - **事故**（踩的坑，这是最值钱的部分，写清楚"症状 → 根因 → 修法"）
   - 收工状态（测试数、花费、遗留）
7. **跑一次评测**（改动涉及检索/装配时）：`python -m hippo.tools.eval_all --all`，召回掉 2 个点以上必须能解释。

**报告不是形式**。`reports/` 里 109 份报告的价值在于：任何一个数字都能追溯到
"哪台机器、什么配置、几条数据、怎么跑的"。

---

## 7. 你要做的下一件事：M28

手册第 4541 行起。三小节，DoD 是**全新机器 pip install 后三条命令跑起来**。

### M28.1 打包与 init（半天）

现状：`pyproject.toml` 的 `[project.scripts]` 已配好、`init`/`doctor` 已实现
（doctor 8/8 全绿）、`package-data` 已声明 sql/yaml/md。

还差：
- **干净环境验证**。手册给的 docker 命令要跑一遍（本机没 docker 就用 `python -m venv` 等价验证，报告里写明替代方式）。
- `pip uninstall` 后确认 `~/.hippo` 之外无残留（`site-packages` 里 `hippo*`、`*.egg-info`、entry point 脚本）。
- 首次 serve 打一句引导："把 base_url 改成 http://localhost:8787/v1 就可以了。"

### M28.2 文档与示例（半天）

现状：README 只有定位 + 记得什么 + 成本表 + 4 条 FAQ。`examples/` 目录**不存在**。

还差：
- README 补到 **≥8 条 FAQ**（现有 4 条：删记忆 / 说记不清 / 换模型 / 缓存没命中）。
  建议补：装不上怎么办 / 会不会上传数据 / 能不能多设备同步 / 换了客户端还能用吗。
- `examples/` 三个可执行示例：`openai_sdk.py`、`curl.sh`、`sillytavern.md`（截图可以后补，文字步骤必须能照做）。
- **从零计时走一遍**，把 `onboarding_time` 写进 R-28.2。自己写的文档自己看得懂不算数。

### M28.3 灰度与回滚（半天）

还差：
- `hippo backup` / `hippo restore --latest`（`~/.hippo/backups/`，保留 7 份）。SQLite 用 `VACUUM INTO` 或 `backup API`，**不要直接 cp**（WAL 模式下会拷到半个事务）。
- `ROLLBACK.md`：停服务 → 换版本 → `hippo doctor` → 起服务，目标 <5 分钟。
- `CHANGELOG.md`：按 M 编号补，0.x 期间允许 break 但要显著标注。
- 旧版本读新库的验证：建一个新库，用"忽略未知列"的方式读，确认不崩。

### P6 Gate（发布门）

`M26 六项指标达标 + M27 压测通过 + M28.1 干净环境可装`，三者缺一不发。
前两项已达标（见第 2 节表格），**只差 M28.1**。

---

## 8. 已知缺陷（**不要顺手修**）

| # | 现象 | 为什么现在不修 |
|---|---|---|
| 1 | P50 针漏：问"养过什么动物"，记的是"狗 金毛" | 上位词，字面检索天然够不着。要修只能上向量检索，会推翻"少依赖"约束。**留在基线里当已知损失**，别为 1 个点堆同义词——会把别的查询挤坏。 |
| 2 | L1 无实体时 6.2ms（8000 事件），手册目标 5ms | 有实体时更快（走倒排）。这是 M9 的账，收益 <1ms 不值得动检索主路径。 |
| 3 | 单进程并发上限约 300 | 瓶颈是 pack 同步跑在事件循环上，不是 SQLite 写锁（全程 0 次 locked）。个人插件真实并发 1–3。要改得把 pack 挪到线程池，是另一个量级的改动。 |
| 4 | 缓存节省 54%，手册示例写 63% | 63% 对应 84% 命中率；实测 72% → 54%。**手册的算法没错，是假设不同**，报告里给了 0/50/72/95% 敏感性表。别改代码去凑手册数字。 |

---

## 9. 坑清单（按主题，都是真踩过的）

### Python / SQLite

- **`sqlite3` 默认 `isolation_level` 会隐式开事务**。项目统一用 `isolation_level=None` + 显式 `BEGIN`，在 `db.py` 里。别改成默认值。
- **`row_factory` 必须设成 `sqlite3.Row`**，否则 `row["x"]` 报错、`row[0]` 静默取错列。
- **WAL 模式下 `cp mem.db` 会拷到半个事务**。备份必须用 `VACUUM INTO` 或 `conn.backup()`。
- **`LIKE '%x'` 不走索引**。31k 行倒排实测 1.6ms，可接受；800 万行就不行了。
- 中文 `LIKE` 大小写不敏感是 ASCII-only，中文无影响，但英文关键词要统一 `lower()`（迁移 009 干过）。

### 时间

- **`datetime.now()` 在测试里必须能注入**。所有涉及"现在"的函数都留 `now=` 参数，测试传固定值。忘了留 → 用例半夜跑挂。
- **`parse_due("下个月十号早上八点")` 曾经返回"明天"**——事故级（搬家提前一个月）。后来加了 `parse_plan_time()` 专门处理远期表达。改时间解析必须跑 `python -m tools.eval_timeparse`（44 例）。
- **单调钟 vs 墙钟**：调度用 `monotonic`，存储用 `time.time()`。混用会漂。

### 检索

- **事实问句会被"话题延续"吞掉**。问"我约的复查是哪天"时，如果上一轮在聊别的，旧逻辑判成延续直接返回零记忆。修法是 `topic.py` 里 `FACT_MARKERS`（什么时候/哪天/多少…）**放在"同实体"判断之后**——顺序错了会把正常追问也打成事实问句。
- **实体别名不全 = 检索不到**。摘要写"我妹结婚"，用户问"妹妹"就捞不到。别名表必须覆盖口语变体（我妈/妈妈、我妹/妹妹）。
- **计划只写 `plans` 表会召回失败**，必须双写 `events`。

### 代理 / 异步

- **抽取调度有三道闸**（总开关 / 攒够轮数 / 该 session 已在抽），少一道就会一次对话排 5 个任务打同一个窗口。
- **`asyncio.Task` 必须持引用**（`app.state.extract_tasks`），否则可能被 GC 掉。
- **流式响应不能先读完再转发**，TTFT 会从 30ms 变 2s。用 `StreamingResponse` 边收边发。

### 评测

- **50 个针共用一个库会互相污染**，必须每针 `clone_store`。
- **中文数字要归一**（"三天" vs "3 天"），否则永远匹配不上。
- **gold 关键词必须是短词**。整句当关键词会让分数从 0.47 掉到 0.20。
- **成本基线要带人设**。不带人设的基线比的是"空 prompt"，没有意义。

### 压测

- **`ThreadingHTTPServer` 的 `request_queue_size` 默认 5**。20 并发一起连，第 6 个之后 SYN 被丢，客户端 1 秒后重传——你会看到一条**恒为 1000ms 的假 p95**，而且看起来像代理的锅。
- **backlog 是 server 的属性，不是 handler 的**。写在 handler 类上完全不生效（第一次就修错了地方）。
- 压测脚本自己出错时，报出来的数字往往"很像真的"。**看到整数秒/整数毫秒先怀疑工具**。

### 观测

- **`structlog` 的输出 `capfd` 抓不到**，测试里要 `monkeypatch` 或者用 `caplog`。
- **告警只留一个出口**。`_warn_once` 之后又无条件 `log.warning` = 每个 tick 刷屏。

---

## 10. 环境、钱、密钥

- **Python ≥ 3.11**（用了 `X | None` 语法和 `tomllib`）。
- **SQLite ≥ 3.34**（`VACUUM INTO`），FTS5 + trigram 要编译进去（`doctor` 会检）。
- 依赖：`pydantic>=2.6`、`pyyaml>=6.0`、`tzdata>=2024.1`；server extra 加 `fastapi/uvicorn/httpx`。
- **密钥只在 `~/.hippo/config.yaml`**，代码里没有任何硬编码；`.gitignore` 已排除。
- **花费**：27 个里程碑合计约 ¥1.5（大部分是 M5–M8 的真机抽取验证）。日常开发用假上游 + 规则评测，**¥0**。`python -m hippo.tools.cost_report` 能算出当前库花了多少。

---

## 11. 读文档的顺序

第一次接手，按这个顺序读，**别一上来读手册**（164KB，会淹）：

1. 本文件（10 分钟）
2. `README.md`（3 分钟，知道对外怎么说）
3. `reports/R-26-final.md` + `reports/R-27-Gate.md`（10 分钟，知道现在什么水平）
4. `hippo/core/pack.py` 的 `pack()` 主函数（20 分钟，全场唯一出口）
5. `hippo/adapters/proxy.py` 的请求处理主体（20 分钟，知道怎么接进来的）
6. 手册「一、施工总则」+「二、验收报告模板」（10 分钟，知道规矩）
7. 需要动哪个模块，再 `grep -n "^## M<编号>" 施工记录.md` 读那一章

要改检索 → 先读 `reports/R-9.*` `R-10.*` `R-11.*` `R-26.*`；
要改装配 → 先读 `R-13.*` `R-16.*` `R-27.*`；
要动情绪/账簿/好奇 → 先读 `R-20.*` `R-23.*` `R-25.*`。

---

## 12. 最后三句

1. **先跑测试，再改代码。** 1137 条里有一半是守着上面那些不变量的，它们红了说明你踩线了，不是测试过时。
2. **每个数字都要能复现。** 报告里写"p95 69ms"就必须有一条命令能再跑出 69ms。写不出来就别写数字。
3. **不确定就问用户。** 他明确说过"全权负责，我只要结果"，但设计层面的取舍（比如要不要为 P50 上向量检索）必须他点头。
