"""Branded model names, exact-match priority and internal fallback scheduling."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from planrouter.core import load_config, parse_config, resolve_model
from planrouter.server import App, Handler


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen.append(body.get("model"))  # type: ignore[attr-defined]
        data = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"total_tokens": 7},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def start_upstream():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    httpd.seen = []  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def config_for(url: str) -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "plan-a", "protocol": "openai", "base_url": url,
             "models": {"auto": "wire-a-auto", "task-model": "wire-exact"}, "priority": 10},
            {"name": "plan-b", "protocol": "openai", "base_url": url,
             "models": {"auto": "wire-b-auto", "*": "*"}, "priority": 20},
            {"name": "free", "protocol": "openai", "base_url": url,
             "models": {"auto": "wire-free"}, "priority": 30},
            {"name": "claude", "protocol": "anthropic", "base_url": url,
             "models": {"auto": "claude-sonnet-4-5"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["plan-a", "plan-b", "free"]},
            {"name": "cheap", "protocol": "openai", "channels": ["free", "plan-b"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
        ],
        "models": {
            "branded": {"my-team-model": "default"},
            "fallback_unknown": True,
            "fallback_route": "default",
        },
    }


class ModelPolicyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = parse_config(config_for("http://127.0.0.1:9000"))

    def test_defaults_include_branded_names(self):
        self.assertEqual(self.cfg.models.branded["facet-auto"], "default")
        self.assertEqual(self.cfg.models.branded["facet-cheap"], "cheap")
        self.assertTrue(self.cfg.models.fallback_unknown)

    def test_reserved_names_are_internal(self):
        names = ("auto", "auto:cheap", "facet", "facet:cheap", "facet-auto",
                 "facet-cheap", "my-team-model")
        for name in names:
            decision = resolve_model(self.cfg, "openai", name)
            self.assertEqual(decision.mode, "auto", name)
            self.assertEqual(decision.exposed, "auto", name)
        self.assertEqual(resolve_model(self.cfg, "openai", "facet-cheap").hint, "cheap")

    def test_exact_match_precedes_internal_schedule(self):
        decision = resolve_model(self.cfg, "openai", "task-model")
        self.assertEqual(decision.mode, "exact")
        self.assertEqual(decision.exposed, "task-model")
        self.assertEqual(decision.exact_channels, ("plan-a",))

    def test_unknown_name_falls_back(self):
        decision = resolve_model(self.cfg, "openai", "gpt-5.1-codex")
        self.assertEqual(decision.mode, "fallback")
        self.assertEqual(decision.hint, "default")
        self.assertEqual(decision.reason, "unknown-model-internal-schedule")

    def test_header_hint_wins(self):
        decision = resolve_model(self.cfg, "openai", "gpt-unknown", "cheap")
        self.assertEqual(decision.mode, "fallback")
        self.assertEqual(decision.hint, "cheap")

    def test_disabled_fallback_rejects_unknown_only(self):
        raw = config_for("http://127.0.0.1:9000")
        raw["models"]["fallback_unknown"] = False
        cfg = parse_config(raw)
        self.assertEqual(resolve_model(cfg, "openai", "gpt-unknown").mode, "rejected")
        self.assertEqual(resolve_model(cfg, "openai", "facet-auto").mode, "auto")
        self.assertEqual(resolve_model(cfg, "openai", "task-model").mode, "exact")

    def test_blocked_channel_is_not_an_exact_match(self):
        decision = resolve_model(self.cfg, "openai", "task-model", blocked={"plan-a"})
        self.assertEqual(decision.mode, "fallback")

    def test_policy_validation(self):
        raw = config_for("http://127.0.0.1:9000")
        raw["models"]["branded"] = {"auto:custom": "default"}
        with self.assertRaisesRegex(ValueError, "reserved"):
            parse_config(raw)
        raw["models"]["branded"] = {"task-model": "default"}
        with self.assertRaisesRegex(ValueError, "clashes"):
            parse_config(raw)
        raw["models"]["branded"] = {}
        raw["models"]["fallback_unknown"] = "yes"
        with self.assertRaisesRegex(ValueError, "fallback_unknown"):
            parse_config(raw)
        raw["models"] = "nope"
        with self.assertRaisesRegex(ValueError, "models policy"):
            parse_config(raw)


class ModelGatewayTests(unittest.TestCase):
    def setUp(self):
        self.upstream = start_upstream()
        url = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config_for(url), tmp)
        tmp.close()
        self.config_path = Path(tmp.name)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(  # type: ignore[attr-defined]
            load_config(self.config_path), config_path=self.config_path,
        )
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.upstream.seen.clear()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.config_path.unlink(missing_ok=True)
        Path(str(self.config_path) + ".tmp").unlink(missing_ok=True)

    def request(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"} if data else {}
        hdrs.update(headers or {})
        req = Request(self.base + path, data=data, headers=hdrs, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, json.loads(resp.read())
        except HTTPError as exc:
            return exc.code, exc.headers, json.loads(exc.read())

    def completion(self, model, headers=None):
        return self.request("POST", "/v1/chat/completions", {
            "model": model, "messages": [{"role": "user", "content": "hi"}],
        }, headers)

    def test_branded_model_uses_internal_schedule(self):
        status, hdrs, _ = self.completion("facet-auto")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["X-Routed-Via"], "plan-a/wire-a-auto")
        self.assertEqual(hdrs["X-Routed-Model-Mode"], "auto")
        self.assertEqual(hdrs["X-Routed-Requested-Model"], "facet-auto")
        self.assertEqual(self.upstream.seen[-1], "wire-a-auto")

    def test_custom_brand_and_cheap_alias(self):
        status, hdrs, _ = self.completion("my-team-model")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["X-Routed-Via"], "plan-a/wire-a-auto")
        status, hdrs, _ = self.completion("facet-cheap")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["X-Route"], "cheap")
        self.assertEqual(hdrs["X-Routed-Via"], "free/wire-free")

    def test_unknown_model_is_scheduled_internally(self):
        status, hdrs, _ = self.completion("gpt-5.1-codex")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["X-Routed-Model-Mode"], "fallback")
        self.assertEqual(hdrs["X-Routed-Model-Reason"], "unknown-model-internal-schedule")
        self.assertEqual(hdrs["X-Routed-Requested-Model"], "gpt-5.1-codex")
        self.assertEqual(self.upstream.seen[-1], "wire-a-auto")

    def test_missing_model_behaves_like_auto(self):
        status, hdrs, _ = self.completion("")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["X-Routed-Model-Mode"], "auto")
        self.assertEqual(hdrs["X-Routed-Requested-Model"], "auto")

    def test_exact_model_is_not_rewritten(self):
        status, hdrs, _ = self.completion("task-model")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["X-Routed-Model-Mode"], "exact")
        self.assertEqual(hdrs["X-Routed-Via"], "plan-a/wire-exact")
        self.assertEqual(self.upstream.seen, ["wire-exact"])

    def test_exact_channel_cooling_degrades_to_internal_schedule(self):
        app = self.httpd.app  # type: ignore[attr-defined]
        app.store.states["plan-a"].mark_cooldown(3600, "quota", time.time())
        status, hdrs, _ = self.completion("task-model")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["X-Routed-Via"], "plan-b/wire-b-auto")
        self.assertEqual(hdrs["X-Routed-Model-Mode"], "fallback")
        self.assertEqual(hdrs["X-Routed-Model-Reason"], "exact-channels-unavailable")
        self.assertEqual(hdrs["X-Failover-Count"], "0")
        self.assertEqual(self.upstream.seen, ["wire-b-auto"])

    def test_route_header_with_unknown_model(self):
        status, hdrs, _ = self.completion("tool-default-model", {"X-Route": "cheap"})
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["X-Route"], "cheap")
        self.assertEqual(hdrs["X-Routed-Via"], "free/wire-free")

    def test_fallback_disabled_returns_400(self):
        app = self.httpd.app  # type: ignore[attr-defined]
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["models"]["fallback_unknown"] = False
        app.apply_config(raw, persist=False)
        status, hdrs, payload = self.completion("gpt-unknown")
        self.assertEqual(status, 400)
        self.assertEqual(hdrs["X-Routed-Model-Mode"], "rejected")
        self.assertIn("facet-auto", payload["models"])
        self.assertEqual(self.completion("facet-auto")[0], 200)
        self.assertEqual(self.completion("task-model")[0], 200)

    def test_models_endpoint_advertises_branded_and_exact_names(self):
        status, _, payload = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        ids = [item["id"] for item in payload["data"]]
        self.assertEqual(ids[0], "facet-auto")
        for name in ("facet-cheap", "my-team-model", "auto", "auto:cheap",
                     "facet:cheap", "task-model"):
            self.assertIn(name, ids)
        modes = {item["id"]: item["mode"] for item in payload["data"]}
        self.assertEqual(modes["facet-auto"], "auto")
        self.assertEqual(modes["task-model"], "exact")
        self.assertTrue(payload["policy"]["fallback_unknown"])

    def test_config_api_round_trips_model_policy(self):
        status, _, config = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(config["models"]["branded"]["facet-auto"], "default")
        config["models"]["branded"]["team-x"] = "cheap"
        status, _, _ = self.request("POST", "/api/config", config)
        self.assertEqual(status, 200)
        status, _, saved = self.request("GET", "/api/config")
        self.assertEqual(saved["models"]["branded"]["team-x"], "cheap")
        status, hdrs, _ = self.completion("team-x")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["X-Route"], "cheap")
        self.assertEqual(hdrs["X-Routed-Via"], "free/wire-free")

    def test_header_injection_is_sanitized(self):
        status, hdrs, _ = self.completion("evil\r\nX-Injected: 1")
        self.assertEqual(status, 200)
        self.assertIsNone(hdrs.get("X-Injected"))
        self.assertNotIn("\n", hdrs.get("X-Routed-Requested-Model") or "")

    def test_anthropic_endpoint_uses_its_own_chain(self):
        status, hdrs, _ = self.request("POST", "/v1/messages", {
            "model": "facet-auto", "messages": [], "max_tokens": 10,
        })
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["X-Routed-Via"], "claude/claude-sonnet-4-5")


class ModelConsoleTests(unittest.TestCase):
    def test_console_exposes_model_policy_controls(self):
        from planrouter.ui import STATIC_DIR
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-tab="models"', html)
        self.assertIn('id="panel-models"', html)
        self.assertIn('id="set-branded"', html)
        self.assertIn("facet:cheap", html)
        self.assertIn("function renderModels()", js)
        self.assertIn("syncModelPolicy", js)


if __name__ == "__main__":
    unittest.main()
