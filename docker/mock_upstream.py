"""Tiny OpenAI/Anthropic-compatible stub, used only for container smoke tests.

Compose starts it as a sidecar so a fresh `docker compose up` can exercise
forwarding, streaming, SSE usage, rate-limit headers and failover without any
real provider credentials. It is not part of the gateway itself.
"""
from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MOCK_PORT", "9000"))
USAGE = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
TEXT = "hello from the mock upstream"


def prompt_text(body: dict) -> str:
    parts: list[str] = []
    for msg in body.get("messages") or []:
        if isinstance(msg, dict):
            parts.append(str(msg.get("content") or ""))
    for item in body.get("input") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or ""))
    return " ".join(parts)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[mock] " + (fmt % args) + "\n")

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def common_headers(self, extra: dict | None = None) -> None:
        self.send_header("Content-Type", "application/json")
        self.send_header("x-ratelimit-limit-requests", "1000")
        self.send_header("x-ratelimit-remaining-requests", "999")
        self.send_header("x-ratelimit-reset-requests", "60s")
        for key, value in (extra or {}).items():
            self.send_header(key, value)

    def send_json(self, status: int, payload: dict, extra: dict | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.common_headers(extra)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self.send_json(200, {"status": "ok"})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        try:
            body = self.read_body()
        except (ValueError, OSError):
            self.send_json(400, {"error": "bad json"})
            return

        # /backup/* is the failover channel and always succeeds. Putting "fail"
        # in a user message makes the primary channel answer 429 with a short
        # Retry-After, so cooldown + failover can be observed in one request.
        backup = self.path.startswith("/backup/")
        model = str(body.get("model") or "")
        if not backup and "fail" in (prompt_text(body) + " " + model).lower():
            self.send_json(429, {"error": {"message": "quota exceeded"}},
                           {"Retry-After": "2"})
            return

        if self.path.endswith("/messages"):
            self.anthropic(body)
        else:
            self.openai(body)

    def openai(self, body: dict) -> None:
        model = body.get("model") or "mock-model"
        if body.get("stream"):
            self.sse(model)
            return
        self.send_json(200, {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": TEXT},
                "finish_reason": "stop",
            }],
            "usage": dict(USAGE),
        })

    def sse(self, model: str) -> None:
        now = int(time.time())

        def chunk(delta: dict, finish=None, usage=None) -> dict:
            payload = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": now,
                "model": model,
                "choices": ([] if usage else
                            [{"index": 0, "delta": delta, "finish_reason": finish}]),
            }
            if usage:
                payload["usage"] = usage
            return payload

        parts = [
            b"data: " + json.dumps(chunk({"role": "assistant", "content": ""})).encode() + b"\n\n",
            b"data: " + json.dumps(chunk({"content": TEXT})).encode() + b"\n\n",
            b"data: " + json.dumps(chunk({}, "stop")).encode() + b"\n\n",
            b"data: " + json.dumps(chunk({}, usage=dict(USAGE))).encode() + b"\n\n",
            b"data: [DONE]\n\n",
        ]
        data = b"".join(parts)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("x-ratelimit-remaining-requests", "999")
        self.end_headers()
        for part in parts:
            self.wfile.write(part)
            self.wfile.flush()

    def anthropic(self, body: dict) -> None:
        self.send_json(200, {
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "model": body.get("model") or "mock-claude",
            "content": [{"type": "text", "text": TEXT}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 8},
        })


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"mock upstream listening on :{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
