# 海马体插件 · hippocampus-deepseek 0.1.0

**给 DeepSeek 加一个"记得住事"的海马体。** 改一行 `base_url` 就能用，
不换客户端、不改 prompt、不存对话到第三方。

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8787/v1", api_key="sk-...")
```

```bash
pip install hippocampus-deepseek   # 代理依赖已内置（fastapi / uvicorn / httpx）
hippo init          # 生成 config.yaml / mem.db / persona.md
hippo serve         # 起代理，然后把客户端的 base_url 指过来
```

首次启动会打印引导（重定向到日志时也看得见，已 flush）：

```
首次使用：把客户端（SillyTavern / OpenAI SDK / 任何 OpenAI 兼容客户端）的
  base_url 改成 "http://127.0.0.1:8787/v1" 就可以了。
```

## 示例

三个都能照做，见 `examples/`：

| 文件 | 作用 |
|---|---|
| `examples/curl.sh` | 两条 curl 验证代理通不通（零依赖） |
| `examples/openai_sdk.py` | 用 OpenAI SDK 说话并验证召回 |
| `examples/sillytavern.md` | SillyTavern 的完整配置步骤 |

## 发布看板

![海马体插件 0.1.0 发布看板](docs/dashboard.png)

深色神经场是手绘 Canvas 三维投影（三层配色分别对应语义 / 情节 / 工作记忆），
**只做视觉呈现，不参与运行时**。直接打开 `docs/dashboard.html` 可看自转动效。

---

## 它记得什么

| 层 | 内容 | 怎么来的 |
|---|---|---|
| 事实 | 住哪、过敏、生日、职业…… | 抽取时写进 `facts`，带生效时间 |
| 事件 | "上个月搬家""老王来杭州" | 每 5 轮抽一次窗口，存摘要 + 关键词 + 原文 |
| 人物 | 关系、称呼、特征 | 从对话里认实体，别名自动合并 |
| 情绪 | 六维心情 + 惯性衰减 | 由事件唤醒度驱动，不调模型 |
| 想问的 | "你搬家搬完了吗" | 从记忆缺口里长出来，每天最多 3 句 |

**不记得**：没聊过的、被用户删掉的、超过半衰期的琐事（hot 3 天 / warm 30 天，
重要的 180 天）；以及"你上周三下午三点说过什么"这种精确到时刻的追问——
它记的是事，不是录音。

## 成本

数字来自 `python -m hippo.tools.cost_report`，token 取自 M26 的 200 轮实测
（`hippo` 模式 573 token/轮），价格按 DeepSeek 官方 ¥2 / ¥0.5 / ¥8 每百万 token，
前缀缓存命中 72%。

| 档位 | 轮/天 | 月 token | 月成本 |
|---|---:|---:|---:|
| 轻度 | 20 | 434,040 | **¥1.08** |
| 中度 | 100 | 2,170,200 | **¥5.41** |
| 重度 | 500 | 10,851,000 | **¥27.04** |

- 输入 573 token = 人设 + 钉住 + 记忆 + 时间锚，输出按 150 token 的普通聊天估。
- **不含客户端自己发的对话历史**——装不装插件都要付那部分。
- 缓存按 72% 命中算，比不缓存省 **54%**；完全不命中时中度是 ¥7.27/月，
  命中 95% 时是 ¥4.81/月。
- 超预算会自动降级（停止抽取、只装人设/钉住/时间锚），次日 00:00 恢复，
  也可以 `hippo budget reset`。`hippo stats` 会显示当前是否降级。

## 备份与恢复

```bash
hippo backup             # 生成 <库目录>/backups/mem-YYYYMMDD-HHMMSS.db，保留最近 7 份
hippo restore --latest   # 先停 serve，再用最近一份备份还原整个库
hippo doctor             # 恢复后自检
```

- 备份走 `VACUUM INTO` 一致快照：WAL 模式下直接 `cp mem.db` 会拷到半个事务。
- `serve` 启动时发现当天还没备份，会自动补一份。
- 恢复时旧库改名 `mem.db.pre-restore-*.bak` 留档，不会直接删。

## 配置

全部配置项见 `hippo/data/config.default.yaml`（带注释），用户配置在
`~/.hippo/config.yaml`。最常改的三个：`deepseek.base_url`、`deepseek.api_key`、
`budget.daily_tokens`。

## 常见问题

**怎么删记忆？** `hippo recall "关键词"` 找到 id，然后 `hippo pin --event N` 或
直接删库里的行；归档用 `hippo archive`。

**为什么它说记不清？** 检索没命中（换个说法再问）、或那条记忆已经衰减到 cold。
`hippo why "你那句话"` 能看到预筛为什么放行/挡掉。

**换模型怎么办？** 改 `deepseek.model` 和 `extract_model` 即可；抽取对模型不敏感，
温度固定 0.2。

**缓存为什么没命中？** 先跑 `python -m tools.probe_cache --offline`：离线正常
说明是我们的前缀结构没问题，问题在上游。见 `reports/R-13.3.md`。

**装不上怎么办？** 先 `hippo doctor`：Python 需 ≥ 3.11、SQLite 需 ≥ 3.34 且编入
FTS5 trigram。Windows 上请用 python.org 的安装包（应用商店版 `python` 是占位
stub，装了也跑不了）。

**会不会上传数据？** 不会。记忆只写本地 SQLite（`~/.hippo/mem.db`），记忆内容只
在你自己的 prompt 里出现；上游只收到拼好的那一轮 prompt——和你直接用 DeepSeek 一样。

**能不能多设备同步？** 不能自动同步。把 `~/.hippo/mem.db`（或 `backups/` 里的
快照）拷到另一台机器同一路径即可。

**换了客户端还能用吗？** 能。任何 OpenAI 兼容客户端都行（OpenAI SDK、SillyTavern、
Cline、curl……），只改 `base_url`。

**记忆会不会越积越大？** 会衰减：hot 3 天 / warm 30 天 / 重要 180 天；冷记忆可
`hippo archive` 归档，`hippo stats --disk` 看占用。

**Windows 中文控制台会乱码/报错吗？** 0.1.0 起 CLI 全部用 ASCII 标记
（`[OK]` / `[FAIL]`），GBK 控制台不再因 emoji 崩溃。
