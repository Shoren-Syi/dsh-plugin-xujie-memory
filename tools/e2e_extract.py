"""M5 端到端演练：真 TCP、真 HTTP、真 SQLite，只有模型是假的。

为什么需要它：pytest 里全是 MockTransport，走不到"真的发一次请求"。
而真实故障恰恰在这一层——URL 拼错、`/v1` 重复、header 大小写、
响应 200 但 content-type 不对、httpx 的 timeout 参数位置写错。
这些 MockTransport 全测不出来，因为它根本不做 URL 解析。

跑法：
    python -m tools.e2e_extract

零成本、零网络（桩在 127.0.0.1）、可重复。产出直接贴进 R-5-Gate。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- 桩
_RE_USER = re.compile(r"^user: (.+)$", re.M)
_RE_KNOWN = re.compile(r"^- (.+)$", re.M)

STATS = {"chat": 0, "extract": 0, "bad": 0}


def _fake_extract(prompt: str) -> str:
    """从 prompt 里"读"出对话，编一份合法的抽取结果。

    刻意用第一条 user 原文做 raw——这样锚定校验会真的通过，
    而不是靠"随便写一句恰好是子串"蒙过去。
    """
    users = [m.group(1).strip() for m in _RE_USER.finditer(prompt)]
    if not users:
        return json.dumps({"events": []}, ensure_ascii=False)

    known = [m.group(1).split("（")[0] for m in _RE_KNOWN.finditer(prompt)]
    events = []
    for raw in users:
        if len(raw) < 5:          # 寒暄不记，模拟规则 6
            continue
        ents = [{"name": n, "type": "person"} for n in known if n in raw]
        if "老王" in raw and not any(e["name"] == "老王" for e in ents):
            ents.append({"name": "老王", "type": "person"})
        kws = [w for w in ("搬家", "朝阳", "海淀", "深圳", "出差", "老王", "后端", "加班")
               if w in raw][:4] or ["对话"]
        events.append({
            "raw": raw,
            "summary": raw[:40],
            "keywords": kws,
            "entities": ents,
            "event_time_hint": "昨天" if "昨天" in raw else "昨天下午",
            "arousal": 0.8 if ("吵架" in raw or "崩了" in raw) else 0.2,
        })
    raw = users[0] if users else ""

    out = {
        "events": events,
        "facts": ([{"entity": "用户", "key": "居住地", "value": "海淀", "confidence": 0.9}]
                  if "海淀" in raw else []),
        "traits": ([{"entity": "老王", "trait": "经常出差", "status": "inferred",
                     "evidence": [i for i, e in enumerate(events) if "老王" in e["raw"]][:1]}]
                   if events and any("老王" in e["raw"] and "出差" in e["raw"]
                                     for e in events) else []),
        "plans": ([{"content": "提醒用户收快递", "due_hint": "明天8点"}]
                  if "快递" in raw else []),
        "doubts": [],
        "affect_delta": {"joy": 3} if "搬家" in raw else {},
    }
    return json.dumps(out, ensure_ascii=False)


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静音
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")

        if not self.path.endswith("/chat/completions"):
            STATS["bad"] += 1
            self._json(404, {"error": "wrong path " + self.path})
            return

        is_extract = (body.get("response_format") or {}).get("type") == "json_object"
        if is_extract:
            STATS["extract"] += 1
            prompt = body["messages"][0]["content"]
            text = _fake_extract(prompt)
        else:
            STATS["chat"] += 1
            text = "好的，记住了。"

        self._json(200, {
            "id": "chatcmpl-e2e", "object": "chat.completion", "created": 1,
            "model": body.get("model", "deepseek-chat"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 850, "completion_tokens": 140,
                      "total_tokens": 990, "prompt_cache_hit_tokens": 0,
                      "prompt_cache_miss_tokens": 850},
        })

    def _json(self, code: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------- 语料
DIALOG = [
    ("我昨天下午搬家了，从朝阳搬到海淀，房租涨了两千", "挺折腾的，新家离公司近吗？"),
    ("近多了，走路二十分钟。对了老王说他下周要去深圳出差", "老王经常出差啊"),
    ("是啊，他上个月还在广州待了半个月", "那确实辛苦"),
    ("我下周得请他吃个饭，顺便把借他的书还了", "记得订位子"),
    ("最近公司项目赶得紧，天天加班到十点", "注意身体"),
    ("早", "早"),
    ("嗯", "嗯"),
    ("哈哈", "哈哈"),
    ("好的", "好的"),
    ("行", "行"),
]


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = srv.socket.getsockname()[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="hippo-e2e-"))
    db = tmp / "mem.db"

    from hippo.config import Config
    from hippo.core.db import connect
    from hippo.core.migrate import migrate
    from hippo.core.store import Store
    from hippo.core.turns import record_pair

    conn = connect(db)
    migrate(conn)
    conn.close()

    for u, a in DIALOG:
        record_pair(db, "main", u, a)

    cfg = Config()
    cfg.db_path = str(db)
    cfg.deepseek.base_url = f"http://127.0.0.1:{port}"
    cfg.deepseek.api_key = "sk-fake"

    from hippo.cli import _extract_sessions

    store = Store.open(db)
    try:
        before = store.pending_turn_count("main")
        reps = asyncio.run(_extract_sessions(cfg, store, "sk-fake", ["main"], True))
        after = store.pending_turn_count("main")
        counts = store.counts()
        runs = store.run_stats("main")
        doc = _sample(store)
    finally:
        store.close()

    print(f"\n上游桩 127.0.0.1:{port}")
    print(f"库     {db}")
    print(f"\n待抽 turn  {before} → {after}")
    print(f"上游调用   聊天 {STATS['chat']} 次 / 抽取 {STATS['extract']} 次 / 错路径 {STATS['bad']} 次")
    for rep in reps:
        print(f"\n[{rep.session}]")
        for r in rep.results:
            print("  " + r.line())
    print("\n入库")
    for k in ("events", "entities", "card_traits", "facts", "plans", "doubts",
              "event_entities", "extract_runs"):
        print(f"  {k:<16}{counts.get(k, 0)}")
    print(f"\n抽取统计  {runs}")
    print("\n抽到的记忆（前 5 条）")
    for line in doc:
        print("  " + line)

    srv.shutdown()

    ok = (
        counts["events"] >= 4
        and counts["extract_runs"] >= 1
        and runs["success_rate"] == 1.0
        and after == 0
        and STATS["bad"] == 0
        and runs["verbatim_fail"] == 0
    )
    print("\n" + ("✅ 端到端通过" if ok else "❌ 端到端未通过"))
    return 0 if ok else 1


def _sample(store) -> list[str]:
    rows = store.conn.execute(
        "SELECT id, tier, weight, summary, keywords, raw FROM events "
        "ORDER BY id LIMIT 5").fetchall()
    return [f"#{r['id']} t{r['tier']} w={r['weight']:.2f} [{r['keywords']}] "
            f"{(r['summary'] or r['raw'])[:30]}" for r in rows]


if __name__ == "__main__":
    sys.exit(main())
