# R-32 · DSH Web 内的记忆看板（进行中）

2026-11-06，项目 `C:\h\hippo`；使用官方 DSH 0.2.1-rc.5 源码确认 Connection Fetch 注册及 owner 认证语义。

- 增加 DSH 客户端模块，注册左侧「记忆」入口及主面板。
- 插件宿主通过 `connection.fetch.register` 注册两个只读端点：看板文档及其数据。Connection 负责浏览器会话认证和 owner 权限。宿主按自身配置的内核端口取得数据；浏览器不直接跨域读取内核。
- 看板保留独立 HTTP 页面，用于同源访问；嵌入 DSH 时请求经过认证的专用数据端点。

## 验证

- `node --test test/board-routes.test.mjs test/plugin.test.mjs test/integration.test.mjs`：9 项通过。
- 已安装链接插件的隔离 Web profile，DSH 监听 58201，插件内核使用非默认端口 8767。实测未认证请求 401；认证后文档与库快照 200；带外站 Origin 的请求 403。
- Edge headless 实际打开 DSH，点击侧栏「记忆」，iframe 成功渲染真实隔离库的空状态；浏览器脚本异常列表为空。截图保存于当前任务的工作目录 `work/dsh-ui.png`。

## 待完成

- 上述验证仅覆盖 Web；桌面载体的页面和 Fetch 通道不同，尚未完成兼容性验证。
- 尚未用非空库验收 DSH 面板的列表、搜索、详情与不同尺寸，也未完成真实聊天的记忆写入、抽取和后续注入验证。
- 用户的真实使用场景、安装流程与发布包仍待完整验收；不能据此声称可上线。
