# -*- coding: utf-8 -*-
"""会话级摘要记忆。

为什么需要它：真实任务型对话里，事件抽取很稀疏（每个会话只沉淀 2~5 条），
而人问"我们之前那个项目定的是什么"时，答案往往散在没被抽成事件的那些轮次里。
检索层没有候选，排序再好也没用。

所以补一条"会话摘要"：一段话说明这个会话在做什么、定过什么、结论是什么。
它不替代事件，只是给检索补一个**总是存在**的锚点。

成本边界：每个会话最多一条，只在会话结束/显式触发时生成一次，约 1 次调用。
"""
from __future__ import annotations

import json
from typing import Any

from ..log import get_logger

log = get_logger("digest")

__all__ = ["DIGEST_KEY", "get_digest", "save_digest", "build_digest", "DIGEST_PROMPT"]

DIGEST_KEY = "session_digest"

DIGEST_PROMPT = """把下面这段对话压缩成一条**会话摘要**，给三个月后的自己看。

要求：
- 60~120 字，中文，第三人称。
- 必须写清：在做什么（项目/任务）、定过什么决定、当前结论或卡点。
- 具体值（数字、名称、日期、比例）能留就留。
- 不要评价、不要复述寒暄、不要分点。
- 只输出摘要正文，不要任何前缀。

对话：
{dialog}
"""


def get_digest(store: Any, session: str) -> str:
    return str(store.get_state(session, DIGEST_KEY, "") or "")


def save_digest(store: Any, session: str, text: str) -> None:
    body = " ".join((text or "").split())
    if not body:
        return
    store.set_state(session, DIGEST_KEY, body[:600])


def _render_dialog(turns: list[Any], max_chars: int = 2200) -> str:
    out, used = [], 0
    for t in turns:
        role = "用户" if getattr(t, "role", "") == "user" else "AI"
        line = f"{role}：{' '.join(str(getattr(t, 'content', '')).split())}"
        if used + len(line) > max_chars:
            break
        out.append(line)
        used += len(line)
    return "\n".join(out)


async def build_digest(store: Any, session: str, cfg: Any, client: Any, api_key: str,
                       *, limit: int = 40) -> str:
    """生成并保存会话摘要；失败返回空串（不影响主流程）。"""
    turns = store.get_turns(session, limit=limit)
    if len(turns) < 2:
        return ""
    dialog = _render_dialog(turns)
    if not dialog:
        return ""
    prompt = DIGEST_PROMPT.format(dialog=dialog)
    text = ""
    try:
        # 不能复用 Encoder.chat：那是抽取用的，会带 JSON 格式约束，纯文本会 400。
        base = str(getattr(cfg.deepseek, "base_url", "https://api.deepseek.com")).rstrip("/")
        resp = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": str(getattr(cfg.extract, "extract_model", "deepseek-flash")),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 220,
                # 必须关思考：开着的时候模型把整段摘要写进 reasoning_content，
                # content 是空字符串（实测），摘要就悄悄丢了。
                "thinking": {"type": "disabled"},
            },
        )
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        text = str(msg.get("content") or msg.get("reasoning_content") or "")
    except Exception as exc:  # noqa: BLE001 - 摘要失败不能影响对话
        log.warning("digest.build.failed", extra={"fields": {"err": repr(exc)[:160]}})
        return ""
    body = " ".join((text or "").split()).strip().strip('"')
    if len(body) < 8:
        return ""
    save_digest(store, session, body)
    log.info("digest.saved", extra={"fields": {"session": session, "chars": len(body)}})
    return body
