"""录制回放（M0.3.3）—— 后面所有 LLM 相关测试的地基。

为什么必须在 M2 之前做完：M5 抽取器的 prompt 要改几十遍。
没有回放，每改一次就是一次真金白银的调用，200 轮评测根本跑不起来；
有了回放，`make test` 永远零成本、零网络、零抖动。

设计上刻意不用 respx / vcrpy：
  - 它们是"再装一个依赖"，而这个东西只需要 100 行；
  - httpx 自带 MockTransport，正好是 transport 层的钩子，够干净。

用法：
    from hippo.testing.cassette import Cassette

    c = Cassette("tests/cassettes")
    client = httpx.AsyncClient(transport=c.transport(), base_url="https://api.deepseek.com")

    HIPPO_RECORD=1 pytest ...   # 真调一次，落盘
    pytest ...                  # 之后全走文件，断网也能跑
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx


class CassetteMiss(RuntimeError):
    """回放未命中。信息必须给出"怎么补录"，而不是只说找不到文件。"""


def _canonical(request: httpx.Request) -> dict[str, Any]:
    """只取影响模型输出的部分。

    刻意排除 Authorization / 时间戳这类不影响语义的头，
    否则换个 key 就得全部重录。
    """
    try:
        body = json.loads(request.content.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        body = {"_raw": hashlib.sha256(request.content).hexdigest()}

    keys = ("model", "messages", "tools", "temperature", "top_p",
            "max_tokens", "response_format", "stream")
    return {k: body[k] for k in keys if k in body}


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class Cassette:
    """一个目录 = 一盒磁带。文件名是请求内容的哈希，内容无关的改动不会互撞。"""

    def __init__(
        self,
        directory: str | Path = "tests/cassettes",
        *,
        record: bool | None = None,
        inner: httpx.BaseTransport | None = None,
    ):
        self.dir = Path(directory)
        if record is None:
            record = os.environ.get("HIPPO_RECORD", "") == "1"
        self.record = record
        # 录制时用的真实通道。测试里注入 fake，就能零网络验证录制路径本身。
        self.inner = inner
        self.net_off = os.environ.get("HIPPO_NET_OFF", "") == "1"
        self.misses: list[str] = []
        self.hits = 0

    # ------------------------------------------------------------------ 存储
    def path_for(self, payload: dict[str, Any]) -> Path:
        return self.dir / f"{_digest(payload)}.json"

    def _load(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, path: Path, request: httpx.Request, response: httpx.Response) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        raw = response.content

        try:
            text = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = base64.b64encode(raw).decode("ascii")
            encoding = "base64"

        keep = ("content-type", "x-request-id", "content-length")
        record = {
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "status": response.status_code,
            "headers": {k: v for k, v in response.headers.items() if k.lower() in keep},
            "encoding": encoding,
            "body": text,
            # 留一份人类可读的请求摘要，方便肉眼确认这盒磁带录的是什么
            "request": {
                "model": _canonical(request).get("model"),
                "stream": _canonical(request).get("stream"),
                "turns": len(_canonical(request).get("messages") or []),
            },
        }
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    def _replay(self, record: dict[str, Any]) -> httpx.Response:
        body = record["body"]
        if record.get("encoding") == "base64":
            content = base64.b64decode(body)
        else:
            content = body.encode("utf-8")
        return httpx.Response(
            status_code=record["status"],
            headers=record.get("headers") or {},
            content=content,
        )

    # ------------------------------------------------------------------ 传输
    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = _canonical(request)
            path = self.path_for(payload)
            record = self._load(path)

            if record is not None:
                self.hits += 1
                return self._replay(record)

            if not self.record:
                self.misses.append(path.name)
                hint = (
                    f"cassette 未命中：{path.name}\n"
                    f"  请求 model={payload.get('model')} stream={payload.get('stream')} "
                    f"turns={len(payload.get('messages') or [])}\n"
                    f"  补录：HIPPO_RECORD=1 pytest <同一条测试>\n"
                    f"  （prompt 改了就一定会未命中，这是故意的：逼你看一眼新 prompt）"
                )
                raise CassetteMiss(hint)

            if self.net_off:
                raise CassetteMiss(
                    f"HIPPO_NET_OFF=1 但 cassette 未命中（{path.name}）；"
                    "录制模式需要联网，请先取消 HIPPO_NET_OFF"
                )

            # 录制：用独立 client 真发一次。别复用被测 client，否则递归。
            real = httpx.Client(timeout=60.0, transport=self.inner)
            try:
                resp = real.send(real.build_request(
                    request.method, request.url, content=request.content,
                    headers={k: v for k, v in request.headers.items() if k != "host"},
                ))
                self._save(path, request, resp)
                return self._replay({
                    "status": resp.status_code,
                    "headers": {k: v for k, v in resp.headers.items()
                                if k.lower() in ("content-type", "x-request-id")},
                    "encoding": "utf-8",
                    "body": resp.text,
                })
            finally:
                real.close()

        return httpx.MockTransport(handler)

    # ------------------------------------------------------------------ 辅助
    def size(self) -> int:
        return len(list(self.dir.glob("*.json"))) if self.dir.exists() else 0
