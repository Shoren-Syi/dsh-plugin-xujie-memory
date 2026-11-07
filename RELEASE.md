# 上线说明 · hippocampus-deepseek 0.2.5

## 这是什么

DSH（DeepSeek Harness）的本地长期记忆插件：自动从对话里抽结构化事件与会话摘要，
在需要时装配回上下文。记忆库是本机 SQLite（`~/.hippo/mem.db`），不经过第三方服务。

## 安装

两种方式，任选其一：

```powershell
# A. 从 tarball 安装（推荐，含 Windows 自带 Python 运行时）
dsh plugin --profile desktop add <路径>/dsh-plugin-hippocampus-deepseek-0.2.5.tgz

# B. 开发用，链接工作副本
dsh plugin --profile desktop add link:C:/h/hippo
```

装完**重启 DSH**（客户端插件在启动时加载），侧栏出现「记忆」即成功。

### 更新时注意（踩过的坑）

插件包里有 `python.exe`，运行时会被内核进程占用，pnpm 直接报 `EPERM`。
更新顺序必须是：

1. 杀掉内核进程（任务管理器里 `running from ...\dsh-plugin-hippocampus-deepseek\runtime` 的那个 python）；
2. 再执行 `dsh plugin add`；
3. 宿主会自动重新拉起内核。

## 回滚

```powershell
dsh plugin --profile desktop add dsh-plugin-hippocampus-deepseek@0.2.4
# 或彻底移除
dsh plugin --profile desktop remove dsh-plugin-hippocampus-deepseek
```

移除不会删记忆库（`~/.hippo/mem.db`），数据还在；要彻底清就手动删该文件。

## 已知限制（必须读）

1. **键要配**：自动抽取需要 `DEEPSEEK_API_KEY`（DSH 凭据服务 / 环境变量 / `~/.hippo/config.yaml`）。
   没 key 时插件仍记录对话原文，但不抽取。
2. **每会话一次摘要调用**：会话结束时生成 60~120 字摘要，约 150~200 输出 token。
3. **真实召回是"判定 90% / 严格原词 66%"**：50 条人工标注问题、判定由同型号模型完成，
   人工抽查一致 16/17。不要对外宣传成"90% 逐字准确"。
4. **只在同会话内检索**：跨会话记忆尚未打通。
5. **多实例**：端口会自动避让；但多个实例共享同一个 `~/.hippo/mem.db`（WAL + busy_timeout 兜底），
   没有做多进程写入压测外的强一致保证。
6. **抽取稀疏是设计使然**：任务型对话（"帮我改这个文件"）不会产生记忆条目；
   会话级摘要负责补住这部分上下文。

## 验收记录（0.2.5）

| 项 | 结果 |
| --- | --- |
| Python 全量测试 | 1,2xx passed / 0 failed |
| Node 测试 | 13 passed |
| 2 万条压力测试 | 0 硬失败（76 rps，写 p95 188ms，检索 p95 16.5ms） |
| 回归 3 千条（摘要改动后） | 0 硬失败（89.6 rps，写 p95 80ms，检索 p95 13.2ms） |
| 人工标注真实召回 50 题 | 判定 45/50（90%），严格 33/50（66%） |
| 干净环境装/用/卸 | clean = True |

## 桌面实机验收（2026-11-07 00:28，由主人确认）

- 重启 DSH 后侧栏出现「记忆」，点开面板正常
- 结论：客户端插件加载、面板渲染、服务端数据链路，端到端通过

## QQ 桥接（来自 0.8 实验分支，默认关闭）

让 DSH 智能体通过 OneBot（NapCat 等协议端）收发 QQ 私聊/群聊，可选定时主动发言与空间动态。
**默认关闭，必须显式配置**；启用前请读最后的风险段。

### 启用步骤

1. 起一个 OneBot 协议端（例如 NapCatQQ），确认 HTTP 与 WebSocket 都可用；
2. 把设置写进内核（服务端持久化，改动后宿主 5 秒内自动重连）：

```powershell
$body = @{
  selfId="<机器人QQ>"; wsUrl="ws://127.0.0.1:3001"; httpUrl="http://127.0.0.1:3000";
  accessToken="<协议端 token，没有就空>";
  privateAllow=@("<允许的QQ号>"); groupAllow=@("<允许的群号>"); requireMention=$true;
  provider="deepseek-official"; model="deepseek-flash"; maxTokens=800;
  budgetScope="<机器人QQ>"
} | ConvertTo-Json
Invoke-RestMethod "http://127.0.0.1:8766/qq/settings" -Method Post -Body $body -ContentType "application/json"
```

3. 配预算（金额单位：**百万分之一元**；不配预算则任何调用都会被拒）：

```powershell
$pol = @{ scope_id="<机器人QQ>"; per_request=100000; daily=1000000; monthly=10000000;
  input_price=1000000; output_price=2000000; daily_calls=20;
  activity_limits=@{ chat=20; proactive=2; qzone=1 } } | ConvertTo-Json
Invoke-RestMethod "http://127.0.0.1:8766/qq/budget/configure" -Method Post -Body $pol -ContentType "application/json"
```

4. 查状态：`GET /qq/budget/status?scope=<机器人QQ>`；停用：`DELETE /qq/settings`。

### 行为与边界

- 私聊/群聊白名单外一律不进模型；群聊默认要求 @ 机器人；同一条消息 5 分钟内去重。
- 每个「机器人 + 会话类型 + 对象 + 真实发言人」是独立 DSH 会话，记忆按人隔离。
- 主动发言/空间动态标记为内部来源，**不写入用户记忆**。
- 每次模型请求前预留预算，拿到真实用量后结算；价格或用量不可知时**保留预留并报错**，
  不会把估算当账单。
- 用量不完整、超预算、超次数：停止该次调用并提示，不静默降级。

### 风险（务必读）

- 走的是**第三方协议端**，不是官方机器人接口，存在账号风控风险，账号与风险由使用者自负。
- 空间动态接口在不同协议端版本上可能不可用。
- **本仓库没有对真实 QQ 做过联调**；已验证的是：过滤/发送/账本的单元测试 + 内核
  HTTP 全链路（设置→预算→预留→结算）+ 非法输入拒绝。真实协议端需要你自己压一遍。
