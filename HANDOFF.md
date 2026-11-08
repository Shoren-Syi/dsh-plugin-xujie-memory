# HANDOFF · 海马体插件（hippocampus-deepseek）

> 给接手的 AI：这份文档是唯一入口。读完它 + `施工记录.md`，你就能继续干。
> 最后更新：2026-11-08 · **1.1.7** · 施工记录已到 M62

---

## 1. 这是什么

给 DeepSeek / DSH 加「记得住事」的本地记忆插件。两种交付形态，同一套内核：

| 形态 | 入口 | 状态 |
|---|---|---|
| DSH 插件 | `dsh plugin --profile <p> add link:<本目录>` | 已实现，**真机重启验证未做** |
| OpenAI 兼容代理 | `pip install hippocampus-deepseek` + `hippo serve` | 已实现 |
| 记忆看板 | 内核 `http://127.0.0.1:<port>/board` | 已实现（独立页面） |

记忆只存本地 SQLite，不训练、不上传。

## 2. 现在到哪了

- M0–M27（交接包）：内核完成，1137 测试。
- M28：发布工程（打包/文档/回滚/Gate）。
- M29：预筛升级（heuristic，真实对话命中率 88%）+ DSH 插件形态 + 仓库化。
- M30：记忆看板（xujie 0.8 三维引擎移植 + 海马体三层 + 存疑星环）。

**测试**：Python `1173 passed` / Node `7 passed`。
**仓库**：git 已初始化，最新提交 `ccc45f9`。
**看板**：`hippo/data/board.html`（约 100KB 自包含）。

## 3. 剩余工作（按优先级）

### P0 · DSH 侧栏面板（client 半）
现在看板是独立页面 `/board`。要变成 DSH 左侧栏「记忆」面板：
- 参考 `xujie-memory` 的 client 半（`_incoming/dsh-plugin-xujie-memory-work-0.8.0/src/client/index.tsx`）：
  `export const inject = ['slots']` → `ctx.slots.inject('sidebar.panellist', register({name, id, label, order}, Icon))`
  + `ctx.slots.inject('main', register({name, key}, Panel))`。
- 用 esbuild 把面板打成 `lib/client.js`（`window.__ModuleLoader__.load(...)` 格式）。
- `package.json` 已有 `dsh.client` 与 `exports["./client"]`，补实现即可。

### P0 · 插件真机验证
本会话不能重启 DSH（会中断会话），所以插件「装上即用」只在 headless 模式验证过。
需要：装插件 → 重启 DSH → 聊两轮 → 确认 `~/.hippo/mem.db` 有 turns、下一轮注入生效。
独立实例已在 `C:\h\dsh2-home`（DSH_HOME），插件装在 headless profile。

### P1 · GitHub 推送
仓库已就绪（`.git` 在交付目录），本机无 GitHub 凭据：
```bash
cd <项目目录>
git remote add origin https://github.com/Shoren-Syi/hippocampus-deepseek.git
git push -u origin main
```

### P2 · 其他
- M26/M27 指标本机复跑（交接报告引用，未重跑）。
- 超长 user 消息抽取被 max_tokens 截断（degraded）——长文本先摘要。
- 事实列表只显示当前有效（历史事实在库不在列表）。

## 4. 运行 / 验证

```powershell
# 内核（真实测试库 t6：29 事件 / 4 事实 / 9 人物 / 4 存疑）
Set-Location C:\h\hippo
& ".\runtime\win32-x64\python.exe" -m hippo.dsh_kernel --db C:\h\t6\mem.db --port 8794
# 看板：http://127.0.0.1:8794/board

# 全量测试
python -m pytest -q          # venv：C:\h\.venv-hippo

# 看板交互验证（Edge headless + CDP）
node scripts/verify-board.mjs

# 多话题真实评测（需真实 key，花钱）
python -m hippo.tools.eval_real_topics http://127.0.0.1:8794

# 压测
python -m hippo.tools.stress_kernel http://127.0.0.1:8794 20 30
```

## 5. 目录地图

```
hippo/            内核（Python）
  dsh_kernel.py   DSH 插件内核（/health /event /assemble /board /extract）
  core/           store / pack / extractor / prescreen / ...
  data/board.html 记忆看板（自包含）
  tools/          import_conversations / eval_real_topics / stress_kernel
lib/index.js      DSH 插件宿主半（拉起内核/事件订阅/注入）
runtime/          包内 Python 3.12 embeddable + 依赖（52MB）
python/           无（内核直接用仓库根的 hippo/）
docs/             看板截图、看板设计交接说明
reports/          R-28/29/30 验收报告
scripts/          verify-board.mjs / verify-clean-install.ps1 / time-onboarding.ps1
tests/            Python 测试（1173）
test/             Node 测试（插件壳 + 真内核）
施工记录.md       M0–M30 施工日志（按 M 编号检索）
海马体插件-完整手册.md  原始设计与验收标准
```

## 6. 已知坑（务必先看）

1. **Windows 长路径**：构建/venv 放 `C:\h\...` 短路径，别放 `_incoming` 深目录（MAX_PATH）。
2. **路径沙箱**：从 DSH 工作区（Documents 下）启动的 exe 只能写工作区；放 `C:\h` 就没限制。
3. **embeddable Python**：`runtime/win32-x64/python312._pth` 是隔离模式，不认 `PYTHONPATH`，
   靠 `_pth` 里的相对路径 `..\..` 找项目根。
4. **端口**：内核默认 8766；现有 DSH Web 在 58153；测试用 8794/8767。
5. **看板数据**：测试库 `C:\h\t6\mem.db`；独立实例库 `C:\h\dsh2-home\hippo\mem.db`（空，聊几轮才有）。
6. **看板结构**：是海马体自己的三层（持久/重点/遗忘）+ 存疑星环，**不要**改回 xujie 的语义/情节/工作。

## 7. 交接包里的关键文档

| 文件 | 用途 |
|---|---|
| `START-HERE.md` | 原始 0.1.0 交接说明（M28 之前的上下文） |
| `施工记录.md` | 全部施工日志，含 M28/M29/M30 的决策与事故 |
| `海马体插件-完整手册.md` | 设计规范与每个 M 的 DoD |
| `reports/R-30-board.md` | 看板验收与当前状态 |
| `docs/看板设计-交接说明.md` | 看板前端的完整交接（数据接口/验收/坑） |
| `CHANGELOG.md` | 版本变更 |
| `ROLLBACK.md` | 回滚步骤 |
