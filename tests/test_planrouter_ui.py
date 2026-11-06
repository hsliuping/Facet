"""Tests for the browser console and its configuration API."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from planrouter.core import load_config
from planrouter.server import App, Handler


def base_config(gateway_key: str | None = None) -> dict:
    listen = {"host": "127.0.0.1", "port": 0}
    if gateway_key:
        listen["api_key"] = gateway_key
    return {
        "_readme": "test config",
        "listen": listen,
        "channels": [
            {
                "name": "plan-a", "protocol": "openai", "base_url": "http://127.0.0.1:9001",
                "api_key": "top-secret", "models": {"auto": "model-a"}, "priority": 10,
            },
            {
                "name": "plan-b", "protocol": "openai", "base_url": "http://127.0.0.1:9002",
                "api_key": "env:PLAN_B_KEY", "models": {"auto": "model-b"}, "priority": 20,
            },
            {
                "name": "claude", "protocol": "anthropic", "base_url": "http://127.0.0.1:9003",
                "models": {"auto": "claude-sonnet-4-5"},
            },
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["plan-a", "plan-b"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
        ],
    }


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.httpd = None

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.path.unlink(missing_ok=True)
        Path(str(self.path) + ".tmp").unlink(missing_ok=True)

    def start(self, config: dict | None = None) -> str:
        raw = config if config is not None else base_config()
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(load_config(self.path), config_path=self.path)  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def call(self, base: str, method: str, path: str, body=None, key: str | None = None):
        headers = {}
        if key:
            headers["Authorization"] = "Bearer " + key
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = Request(base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_page_and_static_assets(self):
        base = self.start()
        status, _, body = self.call(base, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"PlanRouter", body)
        self.assertIn(b"/static/app.js", body)
        status, headers, body = self.call(base, "GET", "/static/style.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get("Content-Type"))
        self.assertIn(b":root", body)
        status, _, _ = self.call(base, "GET", "/static/%2e%2e%2fserver.py")
        self.assertEqual(status, 404)

    def test_config_masks_plaintext_secrets(self):
        base = self.start()
        _, _, body = self.call(base, "GET", "/api/config")
        config = json.loads(body)
        channels = {c["name"]: c for c in config["channels"]}
        self.assertEqual(channels["plan-a"]["api_key"], "***")
        self.assertEqual(channels["plan-b"]["api_key"], "env:PLAN_B_KEY")
        self.assertEqual(config["_readme"], "test config")

    def test_api_requires_gateway_key_but_page_does_not(self):
        base = self.start(base_config(gateway_key="gw-secret"))
        status, _, _ = self.call(base, "GET", "/api/config")
        self.assertEqual(status, 401)
        status, _, _ = self.call(base, "GET", "/api/config", key="gw-secret")
        self.assertEqual(status, 200)
        status, _, _ = self.call(base, "GET", "/")
        self.assertEqual(status, 200)

    def test_save_hot_reloads_and_preserves_secrets_and_state(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        app.store.states["plan-a"].mark_cooldown(3600, "quota", time.time())

        _, _, body = self.call(base, "GET", "/api/config")
        config = json.loads(body)
        config["channels"].append({
            "name": "plan-c", "protocol": "openai", "base_url": "http://127.0.0.1:9004",
            "api_key": "env:PLAN_C_KEY", "models": {"auto": "model-c"}, "priority": 30,
        })
        config["routes"][0]["channels"].append("plan-c")
        status, _, body = self.call(base, "POST", "/api/config", config, key="")
        self.assertEqual(status, 200, body.decode())
        self.assertEqual(json.loads(body)["channels"], 4)

        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        keys = {c["name"]: c.get("api_key") for c in on_disk["channels"]}
        self.assertEqual(keys["plan-a"], "top-secret")
        self.assertEqual(keys["plan-c"], "env:PLAN_C_KEY")
        self.assertNotIn("api_key", on_disk["listen"])

        _, _, state = self.call(base, "GET", "/api/state")
        payload = json.loads(state)
        names = [c["name"] for c in payload["channels"]]
        self.assertIn("plan-c", names)
        self.assertEqual(payload["routes"]["openai:default"], ["plan-a", "plan-b", "plan-c"])
        self.assertTrue(next(c for c in payload["channels"] if c["name"] == "plan-a")["cooling"])

    def test_invalid_config_is_rejected_without_touching_disk(self):
        base = self.start()
        before = self.path.read_bytes()
        _, _, body = self.call(base, "GET", "/api/config")
        config = json.loads(body)
        config["routes"][0]["channels"].append("claude")  # anthropic channel in an openai route
        status, _, body = self.call(base, "POST", "/api/config", config)
        self.assertEqual(status, 400)
        self.assertIn(b"never converted", body)
        self.assertEqual(self.path.read_bytes(), before)

    def test_reset_cooldown_and_reload_from_disk(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        app.store.states["plan-b"].mark_cooldown(7200, "quota", time.time())
        status, _, body = self.call(base, "POST", "/api/channels/plan-b/reset")
        self.assertEqual(status, 200)
        _, _, state = self.call(base, "GET", "/api/state")
        self.assertFalse(next(c for c in json.loads(state)["channels"] if c["name"] == "plan-b")["cooling"])

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["channels"][0]["priority"] = 99
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        status, _, _ = self.call(base, "POST", "/api/reload")
        self.assertEqual(status, 200)
        _, _, body = self.call(base, "GET", "/api/config")
        self.assertEqual(json.loads(body)["channels"][0]["priority"], 99)


if __name__ == "__main__":
    unittest.main()
