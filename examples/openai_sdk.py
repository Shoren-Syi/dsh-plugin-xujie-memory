"""海马体最小示例：用 OpenAI SDK 走本地代理。

前置（两个终端）：

    # 终端 1：初始化并起代理
    pip install hippocampus-deepseek
    hippo init
    hippo serve

    # 终端 2：装 SDK 并跑本文件
    pip install openai
    python examples/openai_sdk.py

代理会优先用你请求头里的 key 转发给上游；不传时用
`~/.hippo/config.yaml` 的 `deepseek.api_key`，再退到环境变量 `DEEPSEEK_API_KEY`。
"""

import os

from openai import OpenAI

BASE_URL = os.environ.get("HIPPO_BASE_URL", "http://127.0.0.1:8787/v1")
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "sk-placeholder")

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

# 1) 先说事实（后台抽取会把它写进本地记忆）
r1 = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "我叫张三，住在杭州，对花生过敏。"}],
)
print("回复 1:", r1.choices[0].message.content)

# 2) 正常聊天继续，几轮之后它应该记得你是谁（默认 5 轮抽一次）
r2 = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "我住哪？过敏什么？"}],
    extra_headers={"X-Hippo-Session": "demo"},
)
print("回复 2:", r2.choices[0].message.content)
