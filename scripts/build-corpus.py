# -*- coding: utf-8 -*-
"""从 DeepSeek 导出 zip 重建评测语料（real-corpus.jsonl）。

导出格式：conversations.json = [{id,title,inserted_at,mapping:{node:{parent,children,message}}}]
一条会话是一条消息树，沿主分支（每层选子树最深的孩子）线性化。
"""
from __future__ import annotations

import io
import json
import sys
import zipfile

sys.setrecursionlimit(100000)
from pathlib import Path

ZIP = Path(r"C:\Users\27203\Downloads\deepseek_data-2026-11-06.zip")
OUT = Path(r"C:\h\stress\real-corpus.jsonl")


def fragments_of(message: dict) -> tuple[str, str]:
    """DeepSeek 导出用 fragments 而不是 role：REQUEST=用户，RESPONSE=助手，THINK=思考。

    思考内容不进语料——它是模型内部推理，不是"人说过的话"。
    """
    user_parts: list[str] = []
    ai_parts: list[str] = []
    for frag in message.get("fragments") or []:
        if not isinstance(frag, dict):
            continue
        kind = str(frag.get("type") or "").upper()
        content = frag.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if kind == "REQUEST":
            user_parts.append(content.strip())
        elif kind in ("RESPONSE", "TEXT"):
            ai_parts.append(content.strip())
    if user_parts:
        return "user", "\n".join(user_parts)
    if ai_parts:
        return "assistant", "\n".join(ai_parts)
    return "", ""


_DEPTH: dict[str, int] = {}


def subtree_depth(mapping: dict, node_id: str) -> int:
    """子树深度，带缓存：导出里的消息树可能上千层，纯递归会撞栈。"""
    if node_id in _DEPTH:
        return _DEPTH[node_id]
    node = mapping.get(node_id) or {}
    children = node.get("children") or []
    depth = 0 if not children else 1 + max(subtree_depth(mapping, c) for c in children)
    _DEPTH[node_id] = depth
    return depth


def linearize(mapping: dict) -> list[dict]:
    out: list[dict] = []
    node_id = "root"
    seen: set[str] = set()
    while node_id and node_id not in seen:
        seen.add(node_id)
        node = mapping.get(node_id) or {}
        role, text = fragments_of(node.get("message") or {})
        if role and text:
            out.append({"role": role, "text": text})
        children = node.get("children") or []
        if not children:
            break
        node_id = max(children, key=lambda c: subtree_depth(mapping, c))
    return out


def main() -> int:
    data = json.loads(zipfile.ZipFile(ZIP).read("conversations.json").decode("utf-8"))
    convs = sorted(data, key=lambda c: c.get("inserted_at") or "")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with io.open(OUT, "w", encoding="utf-8") as fh:
        for i, conv in enumerate(convs, 1):
            msgs = linearize(conv.get("mapping") or {})
            if len(msgs) < 2:
                continue
            session = f"real-{i:03d}"
            for m in msgs:
                fh.write(json.dumps({"session": session, "role": m["role"], "text": m["text"]},
                                    ensure_ascii=False) + "\n")
                rows += 1
    print(f"会话 {len(convs)} 个，写出 {rows} 条 -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
