"""极简 DeepSeek 桩，用于端到端 smoke（不参与 pytest）。"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")

        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()

            def send(payload: bytes):
                self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                self.wfile.flush()

            for i in range(8):
                chunk = {"id": "chatcmpl-smoke", "object": "chat.completion.chunk",
                         "created": 1, "model": body.get("model", "deepseek-chat"),
                         "choices": [{"index": 0, "delta": {"content": f"字{i}"},
                                      "finish_reason": None}]}
                send(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                time.sleep(0.12)

            usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                     "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}
            tail = {"id": "chatcmpl-smoke", "object": "chat.completion.chunk",
                    "choices": [], "usage": usage}
            send(f"data: {json.dumps(tail)}\n\n".encode())
            send(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        resp = {
            "id": "chatcmpl-smoke", "object": "chat.completion", "created": 1,
            "model": body.get("model", "deepseek-chat"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "我是阿岚，记得你。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                      "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20},
        }
        data = json.dumps(resp, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
