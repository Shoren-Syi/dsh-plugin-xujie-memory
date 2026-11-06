"""M27.1 并发压测：20 并发下不崩、不变慢太多。

    python -m hippo.tools.loadtest --concurrency 20 --requests 100

## 这个脚本量的是什么

    TTFT    从发出请求到收到**第一个正文分块**
    P95     20 并发里最慢的那 5%——平均值会掩盖排队
    错误率  非 200 / 连接失败 / 超时
    RSS     代理进程的 VmHWM（内核记的峰值，不是采样出来的）
    locked  "database is locked" 出现次数

## 为什么还要跑一遍直连

只报"经代理 80ms"没有意义——用户不知道上游本身要多少。所以同一批请求
先直连假上游跑一遍，再经代理跑一遍，**差值才是插件自己的开销**。
手册的 P95 <1.2s 是含模型生成的；我们这里假上游固定 6×20ms，
所以能验的是"代理没有把延迟放大一个量级"，不是"DeepSeek 有多快"。

## 为什么用真进程，而不是 ASGITransport

ASGITransport 把所有请求塞在一个事件循环里跑，SQLite 写锁、uvicorn 的
连接处理、lifespan 里的常驻写连接全都测不到。压测的意义就是测这些。

## 数据规模

默认先灌 8000 条事件再压。空库上 pack 是"秒回"的，那个数字不能写进报告：
M26 的 1.4ms 就是内存库 + 空库测出来的，文件库 + WAL + 8000 条才是
用户感知的（见 R-27.1 第三节）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from datetime import datetime

from hippo.config import load_config
from hippo.core.db import connect
from hippo.core.migrate import migrate
from hippo.core.pack import pack
from hippo.core.store import Store

# 用户会怎么说话。刻意不抄摘要里的词——抄了等于自问自答，检索永远命中。
QUERIES = [
    "我上次说的搬家是哪天？",
    "房租现在多少来着",
    "老王什么时候来杭州",
    "我家猫叫什么名字",
    "我体检报告出来了吗",
    "年终奖发了多少",
    "我最近是不是该辞职了",
    "我妈身体怎么样",
    "周末那顿饭定在哪",
    "我的吉他还在吗",
]

TOPICS = ["搬家", "房租", "通勤", "加班", "体检", "房贷", "出差", "猫粮",
          "年终奖", "辞职", "吉他", "蛋糕", "复查", "开会", "高铁", "香菜"]


# ------------------------------------------------------------------ 假上游
class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    chunks = 6
    delay = 0.02

    def log_message(self, *a):  # noqa: D401 - 压测时不要刷屏
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        stream = bool(body.get("stream"))

        if not stream:
            data = json.dumps({
                "id": "chatcmpl-load", "object": "chat.completion", "created": 1,
                "model": "deepseek-chat",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "好。"}}],
                "usage": {"prompt_tokens": 600, "completion_tokens": 4,
                          "prompt_cache_hit_tokens": 400},
            }, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

        def send(payload: bytes) -> bool:
            try:
                self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                # 客户端只取首块就断（压测量 TTFT 就是这样），写后续块必然失败。
                # 这不是错误，不该刷屏。
                return False

        for i in range(self.chunks):
            chunk = {"id": "chatcmpl-load", "object": "chat.completion.chunk",
                     "created": 1, "model": "deepseek-chat",
                     "choices": [{"index": 0, "delta": {"content": f"第{i}段。"},
                                  "finish_reason": None}]}
            if not send(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()):
                return
            if self.delay:
                time.sleep(self.delay)
        tail = {"id": "chatcmpl-load", "object": "chat.completion.chunk",
                "choices": [], "usage": {"prompt_tokens": 600, "completion_tokens": 18,
                                         "prompt_cache_hit_tokens": 400}}
        if not send(f"data: {json.dumps(tail, ensure_ascii=False)}\n\n".encode()):
            return
        send(b"data: [DONE]\n\n")
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server(ThreadingHTTPServer):
    """listen backlog 默认只有 5。20 并发一起连，第 6 个之后 SYN 被丢，
    客户端 1 秒后重传——压测会看到一条假的 p95≈1000ms，而且看起来像是
    代理的锅。第一次跑就踩了这个坑，所以单独起个子类。

    注意：backlog 是**服务器**的属性，不是 handler 的。第一版写在了
    handler 类上（因为 ThreadingHTTPServer 的第二个参数是 handler），
    完全没生效。
    """

    daemon_threads = True
    request_queue_size = 128


def start_upstream(chunks: int, delay: float) -> ThreadingHTTPServer:
    cls = type("Upstream", (_Upstream,), {"chunks": chunks, "delay": delay})
    srv = _Server(("127.0.0.1", 0), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ------------------------------------------------------------------ 灌数
def seed(db: Path, n: int, entities: int = 40) -> int:
    """8000 条事件。走 bulk_import，不逐条 add_event（后者每条重建 FTS）。"""
    conn = connect(db)
    migrate(conn)
    store = Store(conn)
    rows = []
    base = int(time.time()) - 180 * 86400
    for i in range(n):
        topic = TOPICS[i % len(TOPICS)]
        other = TOPICS[(i * 3) % len(TOPICS)]
        person = f"人物{i % entities:02d}"
        # 摘要写得像人话，长度接近真机（15~40 字），否则 token 统计会虚低
        summary = f"{person}和用户聊到{topic}，顺便提到{other}的变化"
        raw = f"我跟你讲，{topic}这事拖了挺久，{other}也一起变了。"
        rows.append(("bench", base + i * 600, base + i * 600,
                     2, 0.3 + (i % 7) / 10, 0.3 + (i % 7) / 10,
                     f"{person} {topic} {other} 用户", summary, raw))
    store.bulk_import_events(rows)
    # 钉两条，让 C 段有内容（空 C 段的 pack 会偏快）
    store.add_event(session="bench", raw="我对花生过敏，别推荐带花生的。",
                    summary="用户对花生过敏", keywords="花生 过敏 忌口",
                    pinned=1, w0=0.9)
    conn.close()
    return n + 1


# ------------------------------------------------------------------ 延迟
def rss_peak_kb(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def rss_now_kb(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


class _Watch(threading.Thread):
    """每 50ms 采一次 RSS。VmHWM 是内核峰值，但它只在进程活着时可读，
    而我们要的是"压测期间"的峰值，不是"包含启动"的峰值。两个都报。"""

    def __init__(self, pid: int):
        super().__init__(daemon=True)
        self.pid = pid
        self.peak = 0
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            self.peak = max(self.peak, rss_now_kb(self.pid))
            time.sleep(0.05)


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    k = min(len(vs) - 1, max(0, int(round((len(vs) - 1) * q))))
    return vs[k]


async def _one(client: httpx.AsyncClient, url: str, msg: str,
               key: str) -> tuple[float, int]:
    """发一轮，返回 (TTFT 毫秒, 正文字节数)。"""
    payload = {"model": "deepseek-chat", "stream": True,
               "messages": [{"role": "user", "content": msg}]}
    t0 = time.perf_counter()
    nbytes = 0
    async with client.stream("POST", url, json=payload,
                             headers={"authorization": f"Bearer {key}"}) as r:
        if r.status_code != 200:
            body = await r.aread()
            raise RuntimeError(f"HTTP {r.status_code}: {body[:120]!r}")
        async for chunk in r.aiter_bytes():
            if chunk:
                nbytes += len(chunk)
                if "delta" in chunk.decode("utf-8", "ignore") or nbytes > 40:
                    break
    return (time.perf_counter() - t0) * 1000, nbytes


async def drive(url: str, total: int, concurrency: int,
                key: str = "sk-load") -> dict:
    """N 个协程抢一个队列。每个协程串行发，模拟"一个人连续聊"。"""
    q: asyncio.Queue[str] = asyncio.Queue()
    for i in range(total):
        q.put_nowait(QUERIES[i % len(QUERIES)])
    ttfts: list[float] = []
    errors: list[str] = []
    lock = asyncio.Lock()

    async with httpx.AsyncClient(timeout=60.0,
                                 limits=httpx.Limits(max_connections=concurrency + 4)) as c:
        async def worker() -> None:
            while True:
                try:
                    msg = q.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    ms, _ = await _one(c, url, msg, key)
                    async with lock:
                        ttfts.append(ms)
                except Exception as exc:  # noqa: BLE001
                    async with lock:
                        errors.append(f"{type(exc).__name__}: {exc}"[:160])

        await asyncio.gather(*[worker() for _ in range(concurrency)])
    return {"ttfts": ttfts, "errors": errors}


# ------------------------------------------------------------------ 主流程
def _write_workspace(root: Path, upstream_port: int, model: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    cfg = root / "config.yaml"
    cfg.write_text(
        "db_path: " + str(root / "mem.db") + "\n"
        "timezone: Asia/Shanghai\n"
        "deepseek:\n"
        f"  base_url: http://127.0.0.1:{upstream_port}\n"
        f"  model: {model}\n"
        "  api_key: sk-load\n"
        "  timeout: 30\n"
        "extract:\n"
        "  enabled: false\n"          # 压测不测抽取，省得干扰延迟
        "heartbeat:\n"
        "  interval_seconds: 3600\n",  # 首轮延迟一小时，压测期间不会醒
        encoding="utf-8",
    )
    (root / "persona.md").write_text(
        "# 我是谁\n\n你是阿岚，一个记得住事的对话伙伴。说话简短，不背诵记忆。\n",
        encoding="utf-8")
    return cfg


def _wait_health(base: str, timeout: float = 20.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = httpx.get(base + "/health", timeout=1.0)
            if r.status_code == 200 and r.json().get("db") == "ok":
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)
    return False


def _bench_pack(db: Path, repeat: int, cfg_path: Path, warm: int = 20) -> dict:
    """文件库 + WAL 上的 pack 延迟（M26 遗留项：1.4ms 是内存库的数字）。

    两种口径都报：
      `cold`  use_cache=False，每轮强制重新检索——**最坏情况**
      `warm`  正常缓存路径，同话题连续聊——**典型情况**

    只报后者会把"检索没触发"算成 pack 很快；只报前者会把用户感知的
    延迟说高一倍。两个都要。
    """
    store = Store.open(db, load_config(cfg_path))
    try:
        now = datetime.now()

        def loop(use_cache: bool) -> list[float]:
            ts: list[float] = []
            for i in range(repeat + warm):
                t0 = time.perf_counter()
                pack(store, "bench", QUERIES[i % len(QUERIES)], now=now,
                     use_cache=use_cache)
                ms = (time.perf_counter() - t0) * 1000
                if i >= warm:             # 前 warm 次是冷启动（读页、建缓存）
                    ts.append(ms)
            return ts

        cold = loop(False)
        # 热路径必须**同一个话题连着聊**，否则每轮都在换话题，复用永远不触发，
        # 测出来和 cold 一样——那就等于没测。
        pack(store, "bench", QUERIES[0], now=now, use_cache=True)
        warm: list[float] = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            pack(store, "bench", QUERIES[0], now=now, use_cache=True)
            warm.append((time.perf_counter() - t0) * 1000)

        def agg(ts: list[float]) -> dict[str, float]:
            return {"p50": statistics.median(ts), "p95": pct(ts, 0.95),
                    "p99": pct(ts, 0.99), "max": max(ts)}

        return {"cold": agg(cold), "warm": agg(warm),
                "cold_first": cold[0] if cold else 0.0}
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M27.1 并发压测")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--requests", type=int, default=100)
    ap.add_argument("--events", type=int, default=8000)
    ap.add_argument("--chunks", type=int, default=6)
    ap.add_argument("--chunk-delay", type=float, default=0.02)
    ap.add_argument("--pack-repeat", type=int, default=200)
    ap.add_argument("--dir", default="")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    root = Path(args.dir or tempfile.mkdtemp(prefix="hippo-load-"))
    root.mkdir(parents=True, exist_ok=True)
    db = root / "mem.db"

    t_seed = time.perf_counter()
    n = seed(db, args.events)
    seed_s = time.perf_counter() - t_seed
    size_mb = db.stat().st_size / 1024 / 1024
    print(f"库 {db}  事件 {n}  文件 {size_mb:.1f}MB  灌库 {seed_s:.1f}s")

    # ---- 文件库上的 pack 延迟（先测，避免压测把页缓存炒热后失真）
    up = start_upstream(args.chunks, args.chunk_delay)
    up_port = up.server_address[1]
    up_url = f"http://127.0.0.1:{up_port}/v1/chat/completions"
    _write_workspace(root, up_port, "deepseek-chat")

    pk = _bench_pack(db, args.pack_repeat, root / "config.yaml")
    c, w = pk["cold"], pk["warm"]
    print(f"pack（文件库）重新检索 p50={c['p50']:.2f}ms p95={c['p95']:.2f}ms"
          f" p99={c['p99']:.2f}ms")
    print(f"pack（文件库）复用 D 段 p50={w['p50']:.2f}ms p95={w['p95']:.2f}ms"
          f" p99={w['p99']:.2f}ms  首轮={pk['cold_first']:.2f}ms")

    # ---- 对照组：直连
    print("\n[直连] 同一批请求，不经代理")
    t0 = time.perf_counter()
    direct = asyncio.run(drive(up_url, args.requests, args.concurrency))
    direct_s = time.perf_counter() - t0
    d_p50 = statistics.median(direct["ttfts"]) if direct["ttfts"] else 0
    d_p95 = pct(direct["ttfts"], 0.95)
    print(f"  p50={d_p50:.0f}ms p95={d_p95:.0f}ms 错误 {len(direct['errors'])}"
          f"  用时 {direct_s:.1f}s")

    # ---- 代理
    port = _free_port()
    env = dict(os.environ, HIPPO_LOG_LEVEL="info")
    logfile = open(root / "proxy.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "hippo.cli", "serve", "--dir", str(root),
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        stdout=logfile, stderr=subprocess.STDOUT, env=env,
    )
    base = f"http://127.0.0.1:{port}"
    print(f"\n[代理] pid={proc.pid}  {base}")
    try:
        if not _wait_health(base):
            print("代理没起来，日志：")
            print((root / "proxy.log").read_text(errors="ignore")[-2000:])
            return 1
        boot_rss = rss_now_kb(proc.pid)
        watch = _Watch(proc.pid)
        watch.start()
        t0 = time.perf_counter()
        via = asyncio.run(drive(base + "/v1/chat/completions",
                                args.requests, args.concurrency))
        wall = time.perf_counter() - t0
        watch.stop.set()
        watch.join(timeout=1)
        hwm = rss_peak_kb(proc.pid) or watch.peak
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        logfile.close()
        up.shutdown()

    tt = via["ttfts"]
    logtext = (root / "proxy.log").read_text(errors="ignore")
    locked = logtext.lower().count("database is locked")
    p50 = statistics.median(tt) if tt else 0.0
    p95, p99 = pct(tt, 0.95), pct(tt, 0.99)
    err_rate = len(via["errors"]) / max(1, args.requests) * 100
    print(f"  p50={p50:.0f}ms p95={p95:.0f}ms p99={p99:.0f}ms"
          f"  错误 {len(via['errors'])}（{err_rate:.1f}%）  用时 {wall:.1f}s")
    print(f"  RSS 峰值 {hwm / 1024:.0f}MB（启动时 {boot_rss / 1024:.0f}MB）"
          f"  database-locked {locked} 次")
    print(f"  代理增量 p50 {p50 - d_p50:+.0f}ms  p95 {p95 - d_p95:+.0f}ms")
    for e in via["errors"][:5]:
        print(f"  ! {e}")

    out = {
        "events": n, "db_mb": round(size_mb, 2), "seed_s": round(seed_s, 2),
        "pack_p50": round(c["p50"], 2), "pack_p95": round(c["p95"], 2),
        "pack_p99": round(c["p99"], 2), "pack_cold_first": round(pk["cold_first"], 2),
        "pack_warm_p50": round(w["p50"], 2), "pack_warm_p95": round(w["p95"], 2),
        "direct_p50": round(d_p50), "direct_p95": round(d_p95),
        "p50": round(p50), "p95": round(p95), "p99": round(p99),
        "errors": len(via["errors"]), "error_rate": round(err_rate, 2),
        "locked": locked, "rss_peak_mb": round(hwm / 1024),
        "wall_s": round(wall, 2),
        "concurrency": args.concurrency, "requests": args.requests,
    }
    print("\n" + json.dumps(out, ensure_ascii=False))
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    if not args.keep and not args.dir:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
    ok = (err_rate < 1.0 and p95 < 1200 and locked == 0
          and hwm / 1024 < 400)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
