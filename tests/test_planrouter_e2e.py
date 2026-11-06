"""End-to-end test: a quota-exhausted channel is cooled and the next one serves."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from planrouter.core import load_config
from planrouter.server import App, Handler


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.server.mode == "quota":
            data = b'{"error":{"message":"usage limit reached"}}'
            self.send_response(429)
            self.send_header("Retry-After", "7200")
        else:
            data = json.dumps({
                "id": "chatcmpl-test", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
            }).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def start(mode: str):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    httpd.mode = mode  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.quota = start("quota")
        self.healthy = start("ok")
        config = {
            "listen": {"host": "127.0.0.1", "port": 0},
            "channels": [
                {"name": "quota-plan", "protocol": "openai",
                 "base_url": f"http://127.0.0.1:{self.quota.server_address[1]}",
                 "models": {"auto": "model-a"}, "priority": 10},
                {"name": "backup-plan", "protocol": "openai",
                 "base_url": f"http://127.0.0.1:{self.healthy.server_address[1]}",
                 "models": {"auto": "model-b"}, "priority": 20},
            ],
            "routes": [
                {"name": "default", "protocol": "openai", "channels": ["quota-plan", "backup-plan"]},
                {"name": "default", "protocol": "anthropic", "channels": ["messages-plan"]},
            ],
        }
        config["channels"].append({
            "name": "messages-plan", "protocol": "anthropic",
            "base_url": f"http://127.0.0.1:{self.healthy.server_address[1]}",
            "models": {"auto": "claude-sonnet-4-5"},
        })
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, tmp)
        tmp.close()
        self.config_path = Path(tmp.name)
        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.gateway.daemon_threads = True
        self.gateway.app = App(load_config(self.config_path))  # type: ignore[attr-defined]
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.gateway.server_address[1]}/v1/chat/completions"

    def tearDown(self):
        for server in (self.gateway, self.quota, self.healthy):
            server.shutdown()
            server.server_close()
        self.config_path.unlink(missing_ok=True)

    def _post(self):
        req = Request(
            self.url,
            data=json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urlopen(req, timeout=5)

    def test_failover_then_cooling(self):
        with self._post() as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("X-Routed-Via"), "backup-plan/model-b")
            self.assertEqual(resp.headers.get("X-Failover-Count"), "1")
            self.assertEqual(json.loads(resp.read())["choices"][0]["message"]["content"], "ok")

        with urlopen(f"http://127.0.0.1:{self.gateway.server_address[1]}/health", timeout=5) as resp:
            health = json.loads(resp.read())
        self.assertIn("backup-plan", health["available"])
        cooling = health["channels"]["quota-plan"]
        self.assertTrue(cooling["cooling"])
        self.assertAlmostEqual(cooling["cooling_seconds"], 7200, delta=5)

        # while cooling, the exhausted channel is not contacted at all
        with self._post() as resp:
            self.assertEqual(resp.headers.get("X-Failover-Count"), "0")

        with urlopen(f"http://127.0.0.1:{self.gateway.server_address[1]}/stats", timeout=5) as resp:
            stats = json.loads(resp.read())["channels"]
        self.assertEqual(stats["backup-plan"]["total_tokens"], 84)
        self.assertEqual(stats["quota-plan"]["failed_requests"], 1)

    def test_no_healthy_channel_returns_503(self):
        # force both channels into a long cooldown through the public state API
        app = self.gateway.app  # type: ignore[attr-defined]
        import time
        now = time.time()
        for name in ("quota-plan", "backup-plan"):
            app.store.states[name].mark_cooldown(3600, "manual", now)
        with self.assertRaises(HTTPError) as ctx:
            self._post()
        self.assertEqual(ctx.exception.code, 503)
        self.assertIn("no healthy channel", ctx.exception.read().decode())


if __name__ == "__main__":
    unittest.main()
