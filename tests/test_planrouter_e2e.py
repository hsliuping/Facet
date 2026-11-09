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
        self.server.hits += 1
        if self.server.mode == "quota":
            data = b'{"error":{"message":"usage limit reached"}}'
            self.send_response(429)
            self.send_header("Retry-After", "7200")
        elif self.server.mode == "notfound":
            data = b'{"error":{"message":"model not found"}}'
            self.send_response(404)
        elif self.server.mode == "neterror":
            # Drop the connection without any response bytes: the gateway
            # sees URLError/OSError, i.e. the transient_network path.
            self.close_connection = True
            raise RuntimeError("simulated connection drop")
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
    httpd.hits = 0  # type: ignore[attr-defined]
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
        self.gateway.app = App(load_config(self.config_path), config_path=self.config_path)  # type: ignore[attr-defined]
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

    def _repoint_primary(self, upstream) -> None:
        """Point the quota-plan channel at another fake upstream, hot-reload."""
        app = self.gateway.app  # type: ignore[attr-defined]
        raw = json.loads(json.dumps(app.raw))
        for ch in raw["channels"]:
            if ch["name"] == "quota-plan":
                ch["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
        app.apply_config(raw)

    def test_not_found_cools_long_without_probe(self):
        notfound = start("notfound")
        try:
            self._repoint_primary(notfound)
            with self._post() as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("X-Routed-Via"), "backup-plan/model-b")

            with urlopen(f"http://127.0.0.1:{self.gateway.server_address[1]}/health",
                         timeout=5) as resp:
                cooling = json.loads(resp.read())["channels"]["quota-plan"]
            self.assertTrue(cooling["cooling"])
            self.assertAlmostEqual(cooling["cooling_seconds"], 86400, delta=5)
            self.assertEqual(cooling["reason"], "http_404_not_found")

            # cooling without half_open: the dead model is not probed again
            with self._post() as resp:
                self.assertEqual(resp.headers.get("X-Failover-Count"), "0")
            self.assertEqual(notfound.hits, 1)
        finally:
            notfound.shutdown()
            notfound.server_close()

    def test_network_error_retried_then_failover(self):
        neterr = start("neterror")
        try:
            self._repoint_primary(neterr)
            with self._post() as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("X-Routed-Via"), "backup-plan/model-b")
            # default network_retries=1: the same channel is hit twice
            self.assertEqual(neterr.hits, 2)

            with urlopen(f"http://127.0.0.1:{self.gateway.server_address[1]}/health",
                         timeout=5) as resp:
                cooling = json.loads(resp.read())["channels"]["quota-plan"]
            self.assertTrue(cooling["cooling"])
            self.assertEqual(cooling["reason"], "transient_network")
        finally:
            neterr.shutdown()
            neterr.server_close()

    def test_network_error_retry_disabled(self):
        neterr = start("neterror")
        try:
            app = self.gateway.app  # type: ignore[attr-defined]
            raw = json.loads(json.dumps(app.raw))
            raw["defaults"] = {"network_retries": 0}
            for ch in raw["channels"]:
                if ch["name"] == "quota-plan":
                    ch["base_url"] = f"http://127.0.0.1:{neterr.server_address[1]}"
            app.apply_config(raw)
            with self._post() as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("X-Routed-Via"), "backup-plan/model-b")
            self.assertEqual(neterr.hits, 1)
        finally:
            neterr.shutdown()
            neterr.server_close()

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

    def test_disabled_channel_hidden_from_health(self):
        # /health's headline list must only name channels that can serve;
        # a disabled one is neither cooling nor blocked, so it would show
        # up without the explicit enabled filter.
        app = self.gateway.app  # type: ignore[attr-defined]
        raw = json.loads(json.dumps(app.raw))
        for ch in raw["channels"]:
            if ch["name"] == "quota-plan":
                ch["enabled"] = False
        app.apply_config(raw)

        with urlopen(f"http://127.0.0.1:{self.gateway.server_address[1]}/health", timeout=5) as resp:
            health = json.loads(resp.read())
        self.assertNotIn("quota-plan", health["available"])
        self.assertIn("backup-plan", health["available"])
        self.assertEqual(health["status"], "ok")

        # routing skips the disabled channel entirely: no wasted failover hop
        with self._post() as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("X-Failover-Count"), "0")
            self.assertEqual(resp.headers.get("X-Routed-Via"), "backup-plan/model-b")

    def test_disabled_route_reports_channel_status(self):
        # Seeded templates ship disabled: the 503 must name the channel and
        # point at the console instead of a bare "no healthy channel".
        app = self.gateway.app  # type: ignore[attr-defined]
        raw = json.loads(json.dumps(app.raw))
        for ch in raw["channels"]:
            if ch["name"] == "messages-plan":
                ch["enabled"] = False
        app.apply_config(raw)
        url = self.url.replace("/v1/chat/completions", "/v1/messages")
        req = Request(url, data=json.dumps(
            {"model": "auto", "max_tokens": 8,
             "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 503)
        body = json.loads(ctx.exception.read().decode())
        self.assertIn("no enabled channel", body["error"]["message"])
        self.assertEqual(body["error"]["channel_status"], {"messages-plan": "disabled"})


if __name__ == "__main__":
    unittest.main()
