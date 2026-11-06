# R-37 · 接手复核：稳定段版本号、定向查询正则与两处否决实验

2026-11-06，Windows x64 + PowerShell 7，DSH Desktop 0.2.2-rc.1，工作副本 `C:\h\hippo`。

本文记录接手新交接包后的独立复核（不是在原报告上改数字）。所有结论都来自本机实测，
命令与样本在文末列出；没测到的地方明确写成"未验证"。

## 一、接手后的四项基线复核（全部通过）

| 项 | 结果 | 说明 |
| --- | --- | --- |
| Python 全量测试 | 1197 passed / 0 failed（含新增 10 例） | `.venv-hippo` 3.12.14 下 `pytest -q` |
| Node 测试 | 11 passed / 0 failed | 插件壳 + 看板路由 + 真内核 HTTP 往返 |
| gold 检索基线 | **49/50 = 98%**，矛盾 0.000 | `eval_all --all --report --idle` |
| 成本 | 1.05x（相对 none）/ **0.130**（相对全量重读） | 每轮约 573 token |

gold 基线只能证明"检索通路没坏"，它用标注直接写库，**不测模型抽取**。

## 二、真实模型抽取库上的 50 问：44/50（88%）

用现成库 `model-benchmark-fixed.db`（真实 `deepseek-flash` 抽取的 200 轮语料，
20 个窗口全部 ok，输入 69,005 / 输出 16,234 token）在**当前代码**上重跑同一批 50 问：

    44/50 = 88%（此前记录 38/50 = 76%，为加入自身事实定向查询与自适应预算之前）
    分类：fact 14/15 · rel 8/12 · pref 9/10 · plan 8/8 · seed 5/5
    漏：P13 P20 P21 P23 P25 P34（rel 占 4 条）

这复现了交接说明里的 44/50，但**它是已知合成语料**，不是独立真实盲测，
不能当作真实用户召回率对外宣称。

## 三、修掉一处真实的缓存正确性缺陷

`pack()` 原来在**分项/总预算裁剪之前**计算稳定段版本号 `[ctx-v{n}]`，裁剪之后照样发。
C 段（钉住事实）一旦被预算裁掉，版本号描述的是裁剪前的字节，实际注入的却是裁剪后的字节。
版本号是给上游缓存用的键：键没变、内容变了，就会复用错误前缀。

实测（pinned 预算压到 12 token）：注入版本 `5623`，实际字节哈希 `0362`，不一致。

修法：A 段先用与 `[ctx-vxxxx]` **等长**的占位符，让预算算术看到最终字节；等所有裁剪落地后，
用实际注入的 A（去版本行）+ B + C 算哈希再等长替换。`_pack_degraded()` 保持原样（它有意只哈希 A+C）。

修复后同一场景：注入版本 `0362`，实际哈希 `0362`，一致。新增回归
`tests/test_m29_3_pack_stability.py`：

- C 段被裁时版本号 == 实际字节哈希
- 总预算受压、角色卡被压缩时版本号 == 实际字节哈希，且相关记忆没被挤掉
- 同一状态连打两次字节完全一致
- 多值槽（忌口）三条以内全给、超过截断标"等"
- 历史事实不因定向查询漏出
- 归属/历史守卫（问别人、问"以前"一律不注入）

## 四、修掉定向自身事实查询的一处正则漏写

`_targeted_self_fact()` 原先只允许"的"出现在"现在/今年"**前面**：
`我的住址是什么` 能命中，`我现在的住址是什么` 命中不了——同一件事换个说法就查不到。
已改成时间副词两侧都允许"的"，并补参数化用例（现在/目前/今年 两两组合）。

## 五、两处**被数据否决**的改法（没有合入）

按"不为已知 50 题调参"的纪律，两处看起来能涨点的改法都先做离线对照，结果否决：

1. **给前 K 条记忆补原话片段**（K=1/2，取 raw 前 160 字）。
   - bench 50：+0；gold 50：**-3 / -4**；真实 14 问：+1 / 0；每轮 +4~11 token。
   - 原因：原话会按档位预算挤占摘要，而 gold 的答案本来在摘要里。
   - 结论：不做。原话层继续只在"用户明确要细节/事件被钉住"时下钻。

2. **把「用户/要求/表示/提出」等摘要体裁词加入检索停用词**。
   - bench / gold / 真实 14 问：全部 ±0，token ±0。
   - 原因：问题里的"用户"来自实体匹配而不是词表；且排名不因它变化。
   - 结论：不做，避免无证据地动词表。

## 六、真实聊天那组（7/19 严格、2/14 判定）到底卡在哪

用 `user-dialogue-labels` 的期望片段（不是 judge 里模型自己的作答）逐题复核 14 问：

    进上下文 2 · 原文有且被选中但摘要改写了原词 6 · 原文有但没选中 3 · 库里根本没有 2 · 摘要有但检索没选中 1

- "摘要改写了原词"这类（如原文"存放在U盘上，而那个U盘放在程序员的柜子里" → 摘要"存于U盘，U盘放在程序员柜子里"）
  是**同义压缩**，严格原词匹配必然判错，但语义是在的。
- 另外这组问题由模型生成，普遍写成第三人称"用户…"，把"用户"塞进了检索词；
  且小样本会话（3~5 条事件）本身检索空间极小。
- 结论：这条指标是**下界**，不能当"真实召回率 7/19"讲，也不能靠它调参。
  要更可信的真实数字，需要冻结的用户原话标注 + 人工或独立模型判定，见 R-36 的遗留。

## 七、打包与安装（本轮已完成的部分）

- 交付目录重新同步：`_incoming/hippocampus-deepseek-0.1.0`（排除 `.git`/缓存/探针），
  另按 `package.json` 的 `files` 白名单打 tarball：
  `_pkg/dsh-plugin-hippocampus-deepseek-0.2.0.tgz`，21,104,534 字节，2414 个条目。
- 包内容核验：`package.json`、`lib/index.js`、`lib/client.js`、`lib/board-routes.js`、
  `cordis.patch.yml`、`hippo/data/board.html`、`hippo/dsh_kernel.py`、
  `hippo/data/prompts/extract.md`、`runtime/win32-x64/python.exe`、`LICENSE`、`README.md`
  全部在包内；无 `_probe`/`__pycache__`/`.git`/`.pyc`/`.log` 泄漏。
- **自足性**：把 tarball 解到空目录，直接用包内运行时
  `python -m hippo.dsh_kernel --db <新库> --port 18799` 启动成功，
  `/health` ok、`/favicon.ico` 204、`/board/data` 空库返回 0 节点（符合预期）。
- **看板真实数据**：同一包内运行时挂真实测试库，`/board` 页面 106,421 字节，
  含 `PERSISTENT`/`FORGETTING`/`drawDoubtRing`/`ringAngle` 标记（即"三层 + 可拖拽星环"）；
  `/board/data` 返回 50 节点 / 51 边，分层 persistent 17 · doubt 4 · forgotten 29，
  与库里实际记录一致，装饰元素不进数据。
- **DSH 安装**：`dsh plugin --profile pkgtest add <tgz>` 在隔离 `DSH_HOME` 下 10s 装好
  （pnpm 11.28.5，`+ dsh-plugin-hippocampus-deepseek 0.2.0`）；安装目录内
  `lib/client.js`、`board.html`、包内 python 全在；`--dump-config` 里出现插件层
  `id: hippocampus-deepseek`。

## 八、headless 首跑的两个真问题（已修）

上一节那句"判断是首次引导卡住"是错的，查下去发现两个都是**插件自己的恶性缺陷**，
只是平时单实例用着看不出来。

### 8.1 `inject` 把 credentials 当硬依赖 → 没有凭据服务的 profile 里插件根本不激活

`lib/index.js` 里原本是 `export const inject = ['systemPrompt', 'credentials']`。
cordis 里模块级 `inject` 是"必需服务"，缺了就一直 pending、整个插件不激活。
实测在 headless profile 下：

    dsh: warning: 1 entry did not activate
    hippocampus-deepseek (dsh-plugin-hippocampus-deepseek): pending (waiting for service: credentials)

后果不是"少个功能"，是**连对话原文都不记**——用户装完看着什么都没发生。
而读 key 的地方本来就是惰性访问 + 兜底（`ctx.get?.('credentials')` + try/catch +
配置/环境变量兜底），根本不需要它当硬依赖。

改成 `export const inject = ['systemPrompt']` 后，同一 profile 不再 pending，
缺 key 时也只报缺少模型 key（这是宿主的事），插件照常记录。

### 8.2 端口写死 8766 → 第二个实例内核 bind 失败并 1.5s 无限重拉

`config.port: 8766` 是写死的。同一台机器开第二个 DSH 实例（桌面 + Web、或自己起
headless）时，第二个实例的内核直接失败：

    ERROR: [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8766)
    kernel exit 3

然后 `child.on('exit')` 无条件 1.5 秒后重拉 → 每秒一次进程狂拉 + 日志刷屏，
界面上完全看不出来记忆其实一条没记。多实例是**很常见的用法**，这算恶性缺陷。

三处一起改：
- `pickPort()`：从期望端口开始探测，返回真正空闲的端口（先顺序 20 个，再从高位随机 20 个）；
  `portFree()` 用 `net.createServer` 在 127.0.0.1 上试绑。
- `cordis.patch.yml` 的默认 `port` 改成 `0`（= 自动：先试 8766，被占就往后挪），
  填具体端口仍然锁死，被占时打警告并退避。
- 失败重拉改成**指数退避** 1.5s → 3s → 6s … 封顶 30s，连续 6 次后只提醒一次；
  不再无条件 1.5 秒。

实测（8766 被正在运行的桌面实例占着）：

    spawn ... --port 8767 --parent-pid 23352
    库 ~/.hippo/mem.db：turns 0 → 2

即：**自动换端口 + 真的记下了这一轮**。

### 8.3 顺带修掉运行时里的 SyntaxWarning

打包运行时日志里刷 `timeparse.py:721: SyntaxWarning: invalid escape sequence '\d'`——
非原生字符串里的正则。3.12 是警告，未来版本直接 SyntaxError。已改成原生字符串，
并用 `python -W error::SyntaxWarning -m compileall -f` 全仓复查干净。

## 九、仍未完成

- **桌面/Web 侧栏面板的点击验收**：需要重启主人自己的 DSH 才会加载新包，本会话不能重启它。
  已验证的是：打包产物能自足启动内核、看板页面与数据接口正常、`dsh plugin add` 能装进干净 profile。
- **GitHub 推送**：本机无凭据，交付目录已 init+commit，需要主人给 token 或自己执行推送。
- **真实用户语料的独立盲测**：样本与判定方式都还不够"独立"（见第六节）。

## 十一、装进主人真实桌面 profile（2026-11-06 19:50）

之前一直没往主人的 `~/.dsh/profiles/desktop` 里装（遵守"不动现有 DSH"），所以侧栏没有「记忆」
是**没装**，不是坏了。本轮经主人要求装上：

- 先备份 `package.json`（`package.json.bak-before-hippo`）。
- `plugin_manager install_bundle` 安装 tgz：`application: applied`，pnpm 5.7s，
  `+ dsh-plugin-hippocampus-deepseek 0.2.0`。
- 校验：`package.json` 的 `dependencies` 与 `dsh.profile.bundles` 均含该包；
  宿主半已经真的跑起来了——`~/.hippo/kernel.log` 出现
  `spawn .../dsh-plugin-hippocampus-deepseek/runtime/win32-x64/python.exe -m hippo.dsh_kernel --port 8766`，
  `/health` 返回 `extract: true`（说明从 DSH 凭据服务拿到了 key）。
- **slot 名交叉核对**：扫了本机同版本 1270 个客户端插件文件，
  `sidebar.panellist` 正被 `dsh-taskboard`、`@smalltailqwq/dsh-client-ui-skin-deep-whale-manager` 使用，
  `main` 也存在 → 我们 `lib/client.js` 用的 slot 名与当前 DSH 版本一致。
- 侧栏还没出现的唯一原因是**客户端插件在页面启动时加载**，而安装发生在应用启动之后：
  刷新页面（Ctrl+R）应可加载；不行就整程序重启。

## 十二、本轮新增/修改的文件


- `hippo/core/pack.py`：版本号占位符 + 裁完回填；`_targeted_self_fact()` 正则修正
- `lib/index.js`：`inject` 去掉 credentials；`pickPort()`/`portFree()`；指数退避重拉
- `cordis.patch.yml`：默认 `port: 0`（自动选端口）
- `hippo/core/timeparse.py`：正则改原生字符串（消 SyntaxWarning）
- `tests/test_m29_3_pack_stability.py`（新增 10 例）、`test/plugin.test.mjs`（+2 例端口测试）、
  `tests/test_targeted_self_fact.py`（沿用）

## 附：本轮命令与样本

```powershell
# 全量测试
& C:\h\.venv-hippo\Scripts\python.exe -m pytest -q --tb=short
# Node 测试（本机 node 由 DSH 自带 electron 提供）
$env:ELECTRON_RUN_AS_NODE=1
& "C:\Users\27203\AppData\Local\Programs\DeepSeek Harness\DeepSeek Harness.exe" `
  --test test/board-routes.test.mjs test/plugin.test.mjs test/integration.test.mjs test/prompt-integration.test.mjs
# gold 基线
& C:\h\.venv-hippo\Scripts\python.exe -m hippo.tools.eval_all --all --report --idle
```

- 真实模型抽取库：`model-benchmark-fixed.db`（私有，不入交付物），50 问复测脚本 `_probe/probe-remeasure-bench.py`
- 版式/一致性探针：`_probe/probe-budget-version.py`；否决实验：`_probe/probe-raw-quote.py`、`_probe/probe-variants.py`
- 真实对话诊断：`_probe/probe-user-misses2.py`（只打印本地诊断，不外发内容）
