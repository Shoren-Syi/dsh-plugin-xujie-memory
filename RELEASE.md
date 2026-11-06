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
