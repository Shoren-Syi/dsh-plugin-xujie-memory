
## 补充：这段对话像软件工程任务（代码专用记忆规则）

上面的规则继续有效，但**记什么**要按工程任务调整。

### 记这些（每类一条，不要合并）

1. **改了什么**：文件路径、函数/类名、命令、配置项，**逐字保留**
   例：`src/auth/login.ts 里的 refreshToken() 改成用 httpOnly cookie`
   例：`pnpm add -D vitest@2.1.0`、`ALTER TABLE events ADD COLUMN importance INTEGER`
2. **为什么改**：报错原文、需求、约束、失败的尝试
   例：`因为 ERR_PNPM_EPERM，Windows 下运行中的 python.exe 锁住了包内文件`
3. **结论与坑**：试过但不行的方法、版本限制、平台差异
   例：`不用 ws 库：Node 内置 WebSocket 够用且免依赖`
4. **决策**：选型、阈值、兼容性承诺
   例：`engines.dsh 定为 >=0.2.2-rc.1 <0.3.0-0`

### 不记

- 寒暄、致谢、复述整段代码
- 单纯解释某个 API 是什么（除非用户随后据此做了决定）
- 你说"已修复"但用户还没确认验证的（记成「待验证」而不是「已修复」）

### importance 打分

- 5：破坏性变更、数据迁移、线上事故、安全相关
- 4：架构决策、接口契约、依赖/版本升级
- 3：普通功能改动、重构
- 2：调试过程、临时排查
- 1：格式调整、注释、重命名

### 例子

输入："我把 db.py 里的 busy_timeout 挪到 journal_mode 前面了，因为 WAL 切换要排他锁，之前两个实例同时启动会 database is locked"
输出事件（1 条）：
- summary：`db.py 中把 PRAGMA busy_timeout 移到 journal_mode 之前，避免双实例冷启动时 database is locked`
- values：`PRAGMA busy_timeout`、`journal_mode`、`database is locked`
- importance：5
