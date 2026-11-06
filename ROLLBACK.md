# 回滚与恢复

目标：出事 **5 分钟内**退回可用状态。三种情况分开处理。

## A. 只回滚代码（数据不动）

数据库迁移是**单向只加**的：旧代码读新库不会崩（多出来的列/表它不选）。
所以回滚代码不用动数据。

1. 停代理：终端里 `Ctrl+C`（或 `hippo stats` 找到进程后 taskkill）。
2. 装回上一个版本：

   ```bash
   pip install "hippocampus-deepseek==0.0.9"   # 换成上一版版本号 / wheel 路径
   ```

3. 自检：

   ```bash
   hippo doctor          # 期望 8/8
   ```

4. 起服务：

   ```bash
   hippo serve
   ```

## B. 数据也回滚（库损坏 / 误删）

前提：代理**已停**（运行中文件被锁，恢复会失败）。

1. 看有哪些备份：

   ```bash
   ls ~/.hippo/backups/          # mem-YYYYMMDD-HHMMSS.db，保留最近 7 份
   ```

2. 恢复最近一份：

   ```bash
   hippo restore --latest
   # 或指定：hippo restore --backup ~/.hippo/backups/mem-20261106-120047.db
   ```

3. 自检 + 起服务：

   ```bash
   hippo doctor && hippo serve
   ```

旧库会自动改名 `mem.db.pre-restore-<时间>.bak`，确认没问题再手动删。

## C. 完全重来

```bash
mv ~/.hippo ~/.hippo.bak-$(date +%Y%m%d)   # 保留现场
hippo init                                  # 新库
```

## 回滚检查清单

- [ ] 代理已停
- [ ] 目标版本可获取（pip 源 / 本地 wheel）
- [ ] `hippo doctor` 8/8
- [ ] 起服务后 `/health` 返回 200
- [ ] 对话一条能正常收发
- [ ] 旧库备份还在（`mem.db.pre-restore-*.bak`）
