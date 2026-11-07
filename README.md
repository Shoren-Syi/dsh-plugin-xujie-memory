# 叙界记忆 1.0

**让 DeepSeek Harness 在对话中记住重要的事。** 叙界记忆 1.0 使用全新的海马体记忆内核，把会话记录、结构化记忆和可视化管理放在一起。

> **从 0.7.x 升级前请先备份。** 1.0 更换了记忆内核和数据库位置，不会自动读取或迁移旧版数据。旧数据库不会因安装 1.0 而自动删除；保留旧数据，确认满足需要后再升级。回退方法见下文。

## 安装

需要已安装的 DeepSeek Harness（DSH）及可用的 profile。安装 npm 包后重启 DSH：

```bash
dsh plugin --profile <你的profile> add dsh-plugin-xujie-memory@1.0.0
```

也可以在 DSH 的添加插件界面输入 `dsh-plugin-xujie-memory`。安装成功并重启后，从侧栏的「记忆」进入记忆面板。首次使用时，将抽取用的 DeepSeek API key 配置到 DSH 凭据服务、`DEEPSEEK_API_KEY` 环境变量或插件设置中；只登录 DeepSeek 账户不等于配置了 API key。没有可用 key 时仍可记录对话原文，但不会进行模型抽取。

Windows x64 安装包内带 Python 运行时；其他平台需要可用的 Python 和内核依赖。插件会启动本地内核，无须手动改模型 `base_url`。

## 1.0 有什么

- **自动记录与召回**：接收会话事件、提取结构化记忆，在对话时组装相关上下文。
- **记忆面板**：以图形方式查看记忆及其关联，支持检索和管理。
- **本地存储**：默认数据库位于 `~/.hippo/mem.db`，内核日志位于 `~/.hippo/kernel.log`。
- **可选 QQ 桥接**：默认关闭，可在面板中配置第三方 OneBot 协议端。该功能尚未完成真实 QQ 联调；配置和限制见 [上线说明](RELEASE.md)。

1.0 使用海马体内核，和 0.7.x 的内部数据结构不同。当前记忆检索以同一会话为范围，跨会话召回尚未打通；不要依赖它找回旧版记忆。

## 从 0.7.x 升级和回退

1. 停止 DSH，备份旧版数据库以及 DSH profile 配置；不要删除旧数据。
2. 安装 1.0 并重启 DSH。新版默认创建或使用 `~/.hippo/mem.db`，不会自动导入旧库。
3. 检查侧栏、API key 配置以及新产生的记忆。旧版自定义配置不保证兼容。

若需要回退，先停止 DSH 与仍在运行的插件内核，再安装旧版并重启：

```bash
dsh plugin --profile <你的profile> add dsh-plugin-xujie-memory@0.7.2
```

恢复旧版时，保留 1.0 的数据库备份，按旧版配置使用旧数据库。Windows 上运行中的 Python 进程可能占用包内文件，升级或回退前需先停止对应进程。

## 隐私与费用

记忆数据库保存在本地。开启模型抽取时，对话片段会发送到你配置的模型服务；召回的记忆也可能作为提示词发送给对话模型。API 请求按服务商的实际价格和用量计费，请查看 [DeepSeek 官方价格](https://api-docs.deepseek.com/quick_start/pricing/)。

## 开发与反馈

源码、问题反馈和使用说明见 [GitHub 仓库](https://github.com/Shoren-Syi/dsh-plugin-xujie-memory)。本项目按 [MIT 协议](LICENSE)发布。
