# 在 SillyTavern 里用海马体

前提：`hippo init` 跑过、`hippo serve` 已经在跑（默认 `127.0.0.1:8787`）。

## 步骤

1. 打开 SillyTavern，点顶部 **「插头」图标**（API 连接面板）。
2. **API** 选 `Chat Completion`；**Chat Completion Source** 选
   `Custom (OpenAI-compatible)`。
3. 填三个字段：

   | 字段 | 值 |
   |---|---|
   | Custom Endpoint / Base URL | `http://127.0.0.1:8787/v1` |
   | API Key | 你的 DeepSeek key（`sk-...`）。已经在 `~/.hippo/config.yaml` 里配过的话，这里填任意非空值即可 |
   | Model | `deepseek-chat` |

4. 点 **「Connect」**。状态灯变绿就可以正常聊天了。

## 验证记忆有没有生效

1. 对角色说：`我叫张三，对花生过敏。`
2. 继续聊满 5 轮（后台每 5 轮抽一次），或者直接重启 SillyTavern 会话。
3. 问：`我过敏什么？` —— 回答里应该出现「花生」。

## 记忆隔离（可选）

海马体按 `X-Hippo-Session` 请求头区分会话；不传时按首条消息自动归并。
SillyTavern 默认不传，所有角色共用一个会话。想按角色隔离，在
`~/.hippo/config.yaml` 里改 `session.header`（默认 `X-Hippo-Session`），
并在 SillyTavern 的 Custom Headers 里给每个角色带不同的值。

> 注意：记忆默认跨会话有效（人物卡、偏好、锚点是用户级的）。
> 只有「最多追问几次」的熔断按会话单独计。
