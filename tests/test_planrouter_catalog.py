"""Tests for per-channel model catalogs: fetch, diff, persistence and the
soft validation applied to routing candidates."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from planrouter.core import ModelDecision, StateStore, load_config
from planrouter.persistence import StatePersistence, capture, restore
from planrouter.probe import models_path_for, parse_model_list, probe_models
from planrouter.server import App, Handler


def config_for(upstream_url: str) -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0, "admin_key": "adm"},
        "defaults": {"catalog_refresh_hours": 24},
        "channels": [
            {"name": "plan-a", "protocol": "openai", "base_url": upstream_url,
             "api_key": "key-a", "models": {"auto": "model-a"}, "priority": 10},
            {"name": "plan-b", "protocol": "openai", "base_url": upstream_url,
             "api_key": "key-b", "models": {"auto": "model-b"}, "priority": 20},
            {"name": "claude", "protocol": "anthropic", "base_url": upstream_url,
             "api_key": "key-c", "models": {"auto": "claude-x"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["plan-a", "plan-b"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
        ],
    }


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, payload) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self.server.seen.append({  # type: ignore[attr-defined]
            "method": "GET", "path": self.path,
            "auth": self.headers.get("Authorization"),
            "x-api-key": self.headers.get("x-api-key"),
            "anthropic-version": self.headers.get("anthropic-version"),
        })
        if "/unauthorized" in self.path:
            self._send(401, {"error": {"message": "invalid api key"}})
        elif "/empty" in self.path:
            self._send(200, {"data": []})
        elif "/shapes" in self.path:
            self._send(200, {"data": [{"id": "m-b"}, {"id": "m-a"}, {"id": "m-b"},
                                      {"name": "m-c"}]})
        else:
            self._send(200, {"data": [{"id": "model-a"}, {"id": "model-b"}]})


class CatalogUnitTests(unittest.TestCase):
    def test_models_path_for(self):
        self.assertEqual(models_path_for("http://host", None), "/v1/models")
        self.assertEqual(models_path_for("http://host/v1", None), "/models")
        self.assertEqual(models_path_for("http://host", "/v1/chat/completions"),
                         "/v1/models")
        self.assertEqual(models_path_for("http://host", "/chat/completions"),
                         "/models")
        self.assertEqual(models_path_for("http://host", "/v1/messages"), "/v1/models")
        self.assertEqual(models_path_for("http://host", "/custom/messages"),
                         "/custom/models")
        self.assertEqual(models_path_for("http://host", "/v1/responses"), "/v1/models")

    def test_parse_model_list_shapes(self):
        self.assertEqual(parse_model_list({"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}]}),
                         ["a", "b"])
        self.assertEqual(parse_model_list({"models": [{"name": "m1"}]}), ["m1"])
        self.assertEqual(parse_model_list(["x", "y"]), ["x", "y"])
        self.assertEqual(parse_model_list({}), [])
        self.assertEqual(parse_model_list("nope"), [])
        self.assertEqual(parse_model_list(None), [])

    def test_probe_models_against_upstream(self):
        with UpstreamServer() as up:
            result = probe_models({"name": "ch", "protocol": "openai",
                                   "base_url": up.url, "api_key": "key-1",
                                   "models": {"auto": "model-a"}})
            self.assertTrue(result["ok"])
            self.assertEqual(result["models"], ["model-a", "model-b"])
            self.assertEqual(result["path"], "/v1/models")
            self.assertEqual(result["count"], 2)
            seen = up.seen[-1]
            self.assertEqual(seen["path"], "/v1/models")
            self.assertEqual(seen["auth"], "Bearer key-1")

            versioned = probe_models({"name": "ch", "protocol": "openai",
                                      "base_url": up.url + "/v3",
                                      "models": {"auto": "m"}})
            self.assertEqual(versioned["path"], "/models")

            anthropic = probe_models({"name": "ch", "protocol": "anthropic",
                                      "base_url": up.url, "api_key": "key-2",
                                      "models": {"auto": "m"}})
            self.assertTrue(anthropic["ok"])
            seen = up.seen[-1]
            self.assertEqual(seen["x-api-key"], "key-2")
            self.assertEqual(seen["anthropic-version"], "2023-06-01")

            bad = probe_models({"name": "ch", "protocol": "openai",
                                "base_url": up.url + "/unauthorized",
                                "models": {"auto": "m"}})
            self.assertFalse(bad["ok"])
            self.assertEqual(bad["http_status"], 401)
            self.assertIn("密钥", bad["hint"])

    def test_probe_models_rejects_invalid_draft(self):
        result = probe_models({"name": "ch", "base_url": "ftp://host",
                               "models": {"auto": "m"}})
        self.assertFalse(result["ok"])
        self.assertIn("base_url", result["error"])


class UpstreamServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.httpd.daemon_threads = True
        self.httpd.seen = []  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def seen(self) -> list:
        return self.httpd.seen  # type: ignore[attr-defined]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class CatalogStoreTests(unittest.TestCase):
    def make_store(self) -> StateStore:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.path.write_text(json.dumps(config_for("http://127.0.0.1:1")),
                             encoding="utf-8")
        return StateStore(load_config(self.path))

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_record_catalog_diffs_against_previous(self):
        store = self.make_store()
        first = store.record_catalog("plan-a", ["model-a", "model-b"], "ep-1", now=100.0)
        self.assertEqual(first, {"added": ["model-a", "model-b"], "removed": []})
        second = store.record_catalog("plan-a", ["model-b", "model-c"], "ep-2", now=200.0)
        self.assertEqual(second, {"added": ["model-c"], "removed": ["model-a"],
                                  "at": 200.0})
        view = store.catalog_view("plan-a")
        self.assertEqual(view["models"], ["model-b", "model-c"])
        self.assertEqual(view["fetched_at"], 200.0)
        self.assertEqual(view["endpoint"], "ep-2")
        self.assertEqual(view["count"], 2)
        self.assertTrue(store.catalog_knows("model-c"))
        self.assertFalse(store.catalog_knows("model-a"))
        self.assertFalse(store.catalog_knows(""))

    def test_capture_restore_round_trips_catalogs(self):
        store = self.make_store()
        store.record_catalog("plan-a", ["model-a"], "ep-1", now=100.0)
        snap = capture(store, now=100.0)
        self.assertIn("catalogs", snap)
        self.assertEqual(snap["catalogs"]["plan-a"]["models"], ["model-a"])

        other = self.make_store()
        restored = restore(other, snap, now=100.0)
        self.assertEqual(restored, 3)
        self.assertEqual(other.catalog_view("plan-a")["models"], ["model-a"])
        self.assertEqual(other.catalog_view("plan-a")["endpoint"], "ep-1")

        # malformed entries are ignored, not raised
        restore(other, {"catalogs": {"plan-b": {"models": "bad"},
                                     "plan-c": {"models": [1, "m2"]}}}, now=100.0)
        self.assertEqual(other.catalog_view("plan-c")["models"], ["1", "m2"])

    def test_persistence_flush_writes_catalogs(self):
        store = self.make_store()
        state_file = self.path.with_name("state.json")
        persistence = StatePersistence(state_file, lambda: store)
        persistence.restore_from_disk()
        store.record_catalog("plan-a", ["model-a"], "ep-1", now=100.0)
        persistence.flush()
        data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(data["catalogs"]["plan-a"]["models"], ["model-a"])
        # unchanged snapshot -> no rewrite; changed catalog -> rewrite
        store.record_catalog("plan-a", ["model-a", "model-b"], "ep-1", now=110.0)
        persistence.flush()
        data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(data["catalogs"]["plan-a"]["models"],
                         ["model-a", "model-b"])
        state_file.unlink(missing_ok=True)


class CatalogAppTests(unittest.TestCase):
    def setUp(self):
        self.upstream = UpstreamServer()
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.path.write_text(json.dumps(config_for(self.upstream.url)),
                             encoding="utf-8")
        self.app = App(load_config(self.path), config_path=self.path)
        self.httpd = None

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.upstream.httpd.shutdown()
        self.upstream.httpd.server_close()
        self.path.unlink(missing_ok=True)
        Path(str(self.path) + ".tmp").unlink(missing_ok=True)
        (self.path.parent / "probe.jsonl").unlink(missing_ok=True)

    def test_probe_channel_models_records_saved_catalog(self):
        result = self.app.probe_channel_models({"name": "plan-a"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["models"], ["model-a", "model-b"])
        view = self.app.store.catalog_view("plan-a")
        self.assertEqual(view["models"], ["model-a", "model-b"])
        self.assertEqual(view["endpoint"], self.upstream.url + "/v1/models")
        self.assertEqual(self.upstream.seen[-1]["auth"], "Bearer key-a")

    def test_probe_channel_models_merges_masked_key(self):
        result = self.app.probe_channel_models({"name": "plan-a", "api_key": "***"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.upstream.seen[-1]["auth"], "Bearer key-a")

    def test_probe_channel_models_does_not_record_unsaved_draft(self):
        result = self.app.probe_channel_models({
            "name": "draft-x", "protocol": "openai",
            "base_url": self.upstream.url, "api_key": "key-d",
            "models": {},
        })
        self.assertTrue(result["ok"])
        self.assertNotIn("catalog", result)
        self.assertNotIn("draft-x", self.app.store.catalogs)

    def test_probe_channel_models_unknown_channel(self):
        result = self.app.probe_channel_models({"name": "ghost"})
        self.assertFalse(result["ok"])
        self.assertIn("找不到", result["error"])

    def test_refresh_all_catalogs_skips_disabled_and_keyless(self):
        import dataclasses
        self.app.config = dataclasses.replace(
            self.app.config,
            channels=[self.app.config.channels[0],
                      dataclasses.replace(self.app.config.channels[1],
                                          enabled=False)],
            routes=dict(self.app.config.routes),
        )
        results = self.app.refresh_all_catalogs()
        self.assertEqual([r["name"] for r in results], ["plan-a"])
        self.assertTrue(results[0]["ok"])

    def test_ui_state_carries_catalog_summary(self):
        self.app.probe_channel_models({"name": "plan-a"})
        snap = self.app.ui_state()
        item = next(c for c in snap["channels"] if c["name"] == "plan-a")
        self.assertEqual(item["catalog"]["count"], 2)
        self.assertIn("fetched_at", item["catalog"])
        self.assertEqual(snap["catalog_refresh_hours"], 24)

    def test_apply_catalog_filter_is_soft(self):
        self.app.store.record_catalog("plan-a", ["model-a"], "ep", now=1.0)
        self.app.store.record_catalog("plan-b", ["model-b"], "ep", now=1.0)
        plan_a = self.app.config.channel("plan-a")
        plan_b = self.app.config.channel("plan-b")
        exact = ModelDecision(mode="exact", hint="default", exposed="model-a",
                              requested="model-a", reason="",
                              exact_channels=("plan-a",))
        kept, skipped = self.app._apply_catalog_filter(
            [(plan_a, "model-a"), (plan_b, "model-a")], exact)
        self.assertEqual([c.name for c, _ in kept], ["plan-a"])
        self.assertEqual(skipped, [{"channel": "plan-b", "wire_model": "model-a",
                                    "reason": "model_not_in_catalog"}])

        fallback = ModelDecision(mode="fallback", hint="default", exposed="mystery",
                                 requested="mystery", reason="")
        kept, skipped = self.app._apply_catalog_filter(
            [(plan_a, "mystery"), (plan_b, "mystery")], fallback)
        self.assertEqual([c.name for c, _ in kept], [])
        self.assertEqual(len(skipped), 2)

        # soft rule: no recorded catalog -> nothing is skipped
        self.app.store.catalogs.clear()
        kept, skipped = self.app._apply_catalog_filter(
            [(plan_a, "mystery")], fallback)
        self.assertEqual([c.name for c, _ in kept], ["plan-a"])
        self.assertEqual(skipped, [])

    def test_apply_catalog_filter_exempts_auto_scheduling(self):
        self.app.store.record_catalog("plan-a", ["model-a"], "ep", now=1.0)
        plan_a = self.app.config.channel("plan-a")
        auto = ModelDecision(mode="auto", hint="default", exposed="auto",
                             requested="auto", reason="")
        kept, skipped = self.app._apply_catalog_filter([(plan_a, "model-a")], auto)
        self.assertEqual([c.name for c, _ in kept], ["plan-a"])
        self.assertEqual(skipped, [])


class CatalogEndpointTests(unittest.TestCase):
    def setUp(self):
        self.upstream = UpstreamServer()
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.path.write_text(json.dumps(config_for(self.upstream.url)),
                             encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(load_config(self.path), config_path=self.path)  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.upstream.httpd.shutdown()
        self.upstream.httpd.server_close()
        self.path.unlink(missing_ok=True)
        Path(str(self.path) + ".tmp").unlink(missing_ok=True)
        (self.path.parent / "probe.jsonl").unlink(missing_ok=True)

    def call(self, path: str, body, headers: dict | None = None):
        hdrs = dict(headers or {})
        hdrs["Content-Type"] = "application/json"
        req = Request(self.base + path, data=json.dumps(body).encode("utf-8"),
                      headers=hdrs, method="POST")
        try:
            with urlopen(req, timeout=8) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_endpoint_requires_admin_key(self):
        status, _ = self.call("/api/probe/models", {"channel": {"name": "plan-a"}})
        self.assertEqual(status, 401)

    def test_endpoint_fetches_and_records(self):
        admin = {"X-Admin-Key": "adm"}
        status, data = self.call("/api/probe/models",
                                 {"channel": {"name": "plan-a"}}, admin)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["catalog"]["models"], ["model-a", "model-b"])
        self.assertEqual(data["changes"], {"added": ["model-a", "model-b"],
                                           "removed": []})
        # second call reports no diff, but marks when the diff was computed
        status, data = self.call("/api/probe/models",
                                 {"channel": {"name": "plan-a"}}, admin)
        self.assertEqual(data["changes"]["added"], [])
        self.assertEqual(data["changes"]["removed"], [])
        self.assertIn("at", data["changes"])

        # invalid body shapes
        status, _ = self.call("/api/probe/models", {"channel": "plan-a"}, admin)
        self.assertEqual(status, 400)
        # ghost channel: local miss -> ok False with a clear hint
        status, data = self.call("/api/probe/models",
                                 {"channel": {"name": "ghost"}}, admin)
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()
