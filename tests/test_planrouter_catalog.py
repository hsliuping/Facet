"""Tests for per-channel model catalogs: fetch, diff, persistence and the
soft validation applied to routing candidates."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from facet.gateway.core import ModelDecision, StateStore, load_config
from facet.gateway.persistence import StatePersistence, capture, restore
from facet.gateway.probe import models_path_for, parse_model_list, probe_models
from facet.gateway.server import App, Handler


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

    def test_capture_restore_round_trips_latency_ewma(self):
        store = self.make_store()
        store.states["plan-a"].record_latency(1.5)
        snap = capture(store, now=100.0)
        other = self.make_store()
        restore(other, snap, now=100.0)
        self.assertAlmostEqual(other.states["plan-a"].latency_ewma, 1.5)

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

    def test_probe_channel_models_uses_registry_plan_list(self):
        self.app.providers = [{
            "id": "volcengine", "name": "火山引擎方舟",
            "modes": {"coding": {"label": "Coding 套餐",
                                 "base_url": "https://ark.example.com/api/coding/v3",
                                 "models": ["glm-5.3", "kimi-k3"]}},
        }]
        result = self.app.probe_channel_models(
            {"name": "plan-a", "provider": "volcengine", "mode": "coding"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "registry")
        self.assertEqual(result["endpoint"], "registry:volcengine/coding")
        self.assertEqual(result["models"], ["glm-5.3", "kimi-k3"])
        # plan list is recorded as the channel catalog; upstream not contacted
        view = self.app.store.catalog_view("plan-a")
        self.assertEqual(view["endpoint"], "registry:volcengine/coding")
        self.assertEqual(self.upstream.seen, [])

    def test_probe_channel_models_filters_registry_free_models(self):
        self.app.providers = [{
            "id": "hunyuan", "name": "腾讯混元",
            "modes": {"payg": {"label": "按量付费", "models": [],
                               "free_models": ["hy4-flash", "hy3"]}},
        }]
        full = {"ok": True, "endpoint": "https://tokenhub.tencentmaas.com/v1/models",
                "models": ["hy3", "hy-image-v3.5-preview", "hy4-flash",
                           "kling-video-v4", "hy4-preview"]}
        with mock.patch("facet.gateway.server.probe_models", return_value=full):
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "hunyuan", "mode": "payg"},
                allow_ark_quota=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "free-quota")
        self.assertEqual(result["models"], ["hy4-flash", "hy3"])
        self.assertEqual(result["count"], 2)
        self.assertIn("已按免费额度过滤", result["hint"])
        # catalogs persist alphabetically; the pull order is free_ids order
        self.assertEqual(self.app.store.catalog_view("plan-a")["models"],
                         ["hy3", "hy4-flash"])

    def test_probe_channel_models_free_subset_stale_falls_back_to_full(self):
        self.app.providers = [{
            "id": "hunyuan", "name": "腾讯混元",
            "modes": {"payg": {"label": "按量付费", "models": [],
                               "free_models": ["retired-model"]}},
        }]
        full = {"ok": True, "endpoint": "https://tokenhub.tencentmaas.com/v1/models",
                "models": ["hy4-flash", "hy3"]}
        with mock.patch("facet.gateway.server.probe_models", return_value=full):
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "hunyuan", "mode": "payg"},
                allow_ark_quota=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["models"], ["hy4-flash", "hy3"])
        self.assertNotIn("source", result)
        self.assertIn("全量清单", result["hint"])

    def test_probe_channel_models_free_quota_without_creds_falls_back(self):
        # TokenHub now declares a management-plane CLI; a free pull with no
        # SecretId/SecretKey must fall back to the registry subset (not fail)
        # and point the user at the credentials that unlock the live list.
        self.app.providers = [{
            "id": "hunyuan", "name": "腾讯混元",
            "modes": {"payg": {
                "label": "按量付费", "models": [],
                "free_quota_cmd": ["facet-tokenhub-quota", "--output", "json"],
                "credential_envs": [
                    {"env": "TENCENT_SECRET_ID", "label": "SecretId"},
                    {"env": "TENCENT_SECRET_KEY", "label": "SecretKey"}],
                "free_models": ["hy4-flash", "hy3"]}},
        }]
        full = {"ok": True,
                "endpoint": "https://tokenhub.tencentmaas.com/v1/models",
                "models": ["hy3", "hy-image-v3.5-preview", "hy4-flash"]}
        env = {k: v for k, v in os.environ.items()
               if k not in ("TENCENT_SECRET_ID", "TENCENT_SECRET_KEY")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("facet.gateway.server.probe_models",
                           return_value=full):
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "hunyuan", "mode": "payg",
                 "tier": "free"},
                allow_ark_quota=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "free-quota")
        self.assertEqual(result["models"], ["hy4-flash", "hy3"])
        self.assertIn("未配置管理面凭据", result["hint"])
        self.assertIn("TENCENT_SECRET_ID", result["hint"])

    def test_probe_channel_models_background_sweep_keeps_full_catalog(self):
        self.app.providers = [{
            "id": "hunyuan", "name": "腾讯混元",
            "modes": {"payg": {"label": "按量付费", "models": [],
                               "free_models": ["hy4-flash"]}},
        }]
        with mock.patch("facet.gateway.server.probe_models", return_value={
                "ok": True, "endpoint": "e", "models": ["hy4-flash", "video-x"]}) as pm:
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "hunyuan", "mode": "payg"})
        self.assertEqual(result["models"], ["hy4-flash", "video-x"])
        pm.assert_called_once()

    def test_probe_channel_models_payg_without_credentials_refuses(self):
        self.app.providers = [{
            "id": "volcengine", "name": "火山引擎方舟",
            "modes": {"payg": {"label": "按量付费", "models": ["x-y"]}},
        }]
        with mock.patch("facet.gateway.server.probe_models") as pm:
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "volcengine", "mode": "payg"},
                allow_ark_quota=True)
        # No AK/SK -> the pull is refused, never falls back to the
        # data-plane paid catalog dump.
        self.assertFalse(result["ok"])
        self.assertIn("未配置 IAM AK/SK", result["error"])
        pm.assert_not_called()

    def test_probe_channel_models_ark_quota_management_plane(self):
        import dataclasses
        self.app.providers = [{
            "id": "volcengine", "name": "火山引擎方舟",
            "modes": {"payg": {"label": "按量付费", "models": ["x-y"]}},
        }]
        self.app.config = dataclasses.replace(
            self.app.config,
            credentials={"volcengine": {"VOLC_ACCESSKEY": "ak-ui",
                                        "VOLC_SECRETKEY": "sk-ui"}})
        items = [
            {"FoundationModelName": "doubao-seed-1-6", "State": "Available",
             "InitialInferenceFreeUsage": {"Total": 500000, "Consumed": 100000}},
            {"FoundationModelName": "doubao-seed-1-6-flash", "State": "Available",
             "InitialInferenceFreeUsage": {"Total": 2000, "Consumed": 2000}},
        ]
        with mock.patch("facet.gateway.server.ark_quota.list_items",
                        return_value=items) as li, \
             mock.patch("facet.gateway.server.probe_models",
                        return_value={"ok": True, "endpoint": "https://u/models",
                                      "models": ["doubao-seed-1-6",
                                                 "kimi-k3-1-pro-261024"]}) as pm:
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "volcengine", "mode": "payg"},
                allow_ark_quota=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "ark-quota")
        # flash has zero remaining quota -> not offered; a callable kimi name
        # without a matching management family is also not offered.
        self.assertEqual(result["models"], ["doubao-seed-1-6"])
        self.assertTrue(str(result["endpoint"]).startswith("management:"))
        li.assert_called_once_with("ak-ui", "sk-ui")
        pm.assert_called_once()

    def test_probe_channel_models_ark_quota_resolves_version_suffix(self):
        import dataclasses
        self.app.providers = [{
            "id": "volcengine", "name": "火山引擎方舟",
            "modes": {"payg": {"label": "按量付费", "models": ["x-y"]}},
        }]
        self.app.config = dataclasses.replace(
            self.app.config,
            credentials={"volcengine": {"VOLC_ACCESSKEY": "ak-ui",
                                        "VOLC_SECRETKEY": "sk-ui"}})
        items = [
            {"FoundationModelName": "kimi-k3-1-pro", "State": "Available",
             "InitialInferenceFreeUsage": {"Total": 500000, "Consumed": 32}},
        ]
        with mock.patch("facet.gateway.server.ark_quota.list_items",
                        return_value=items), \
             mock.patch("facet.gateway.server.probe_models",
                        return_value={"ok": True, "endpoint": "https://u/models",
                                      "models": ["kimi-k3-1-pro-261024",
                                                 "doubao-seed-evolving"]}):
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "volcengine", "mode": "payg"},
                allow_ark_quota=True)
        self.assertTrue(result["ok"])
        # family name resolves to its versioned callable id
        self.assertEqual(result["models"], ["kimi-k3-1-pro-261024"])

    def test_probe_channel_models_ark_quota_draft_credentials_win(self):
        import dataclasses
        self.app.config = dataclasses.replace(
            self.app.config,
            credentials={"volcengine": {"VOLC_ACCESSKEY": "ak-saved",
                                        "VOLC_SECRETKEY": "sk-saved"}})
        draft = {"name": "plan-a", "provider": "volcengine", "mode": "payg",
                 "credentials": {"volcengine": {"VOLC_ACCESSKEY": "ak-draft",
                                                "VOLC_SECRETKEY": "sk-draft"}}}
        with mock.patch("facet.gateway.server.ark_quota.list_items",
                        return_value=[]) as li, \
             mock.patch("facet.gateway.server.probe_models",
                        return_value={"ok": True, "endpoint": "https://u/models",
                                      "models": ["a"]}):
            result = self.app.probe_channel_models(draft, allow_ark_quota=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 0)
        self.assertIn("免费额度", result["hint"])
        li.assert_called_once_with("ak-draft", "sk-draft")

    def test_probe_channel_models_ark_quota_failure_reported(self):
        import dataclasses
        self.app.config = dataclasses.replace(
            self.app.config,
            credentials={"volcengine": {"VOLC_ACCESSKEY": "ak-bad",
                                        "VOLC_SECRETKEY": "sk-bad"}})
        with mock.patch("facet.gateway.server.ark_quota.list_items",
                        side_effect=RuntimeError("HTTP 401：InvalidCredentials")):
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "volcengine", "mode": "payg"},
                allow_ark_quota=True)
        self.assertFalse(result["ok"])
        self.assertIn("401", result["error"])
        self.assertIn("IAM", result["hint"])

    def test_probe_channel_models_ark_quota_without_credentials_refuses(self):
        import os
        self.app.providers = [{
            "id": "volcengine", "name": "火山引擎方舟",
            "modes": {"payg": {"label": "按量付费", "models": ["x-y"]}},
        }]
        # No saved credentials; blank out any ambient env AK/SK too.
        env = dict(os.environ)
        env["VOLC_ACCESSKEY"] = ""
        env["VOLC_SECRETKEY"] = ""
        with mock.patch.dict(os.environ, env, clear=True):
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "volcengine", "mode": "payg"},
                allow_ark_quota=True)
        # Refuse instead of falling back to the data-plane paid dump.
        self.assertFalse(result["ok"])
        self.assertIn("未配置 IAM AK/SK", result["error"])
        self.assertEqual(self.upstream.seen, [])

    def _bailian_providers(self):
        self.app.providers = [{
            "id": "bailian", "name": "阿里云百炼",
            "modes": {"payg": {
                "label": "按量付费", "models": [],
                "free_quota_cmd": ["bl", "usage", "free", "--output", "json"],
                "credential_envs": [
                    {"env": "BAILIAN_ACCESS_KEY_ID", "label": "AccessKey ID（AK）"},
                    {"env": "BAILIAN_ACCESS_KEY_SECRET", "label": "AccessKey Secret（SK）"},
                ],
            }},
        }]

    def test_probe_channel_models_free_quota_cli_filters_models(self):
        import dataclasses
        self._bailian_providers()
        self.app.config = dataclasses.replace(
            self.app.config,
            credentials={"bailian": {"BAILIAN_ACCESS_KEY_ID": "ak-ui",
                                     "BAILIAN_ACCESS_KEY_SECRET": "sk-ui"}})
        rows = [
            {"model": "deepseek-v4.1-flash", "type": "Text", "remaining": 900000,
             "total": 1000000, "expires": "2099-12-31"},
            {"model": "qwen3.8-max-0902", "type": "Text", "remaining": 0,
             "total": 1000000, "expires": "2099-12-31"},
            {"model": "wanx-video", "type": "Video", "remaining": 500,
             "total": 500, "expires": "2099-12-31"},
            {"model": "qwen-old", "type": "Text", "remaining": 10,
             "total": 10, "expires": "2000-01-01"},
        ]
        with mock.patch("facet.gateway.server.freealign_mod.run_vendor_cmd",
                        return_value=json.dumps({"data": rows})) as rc, \
             mock.patch("facet.gateway.server.probe_models") as pm:
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "bailian", "mode": "payg",
                 "tier": "free"},
                allow_ark_quota=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "free-quota")
        self.assertEqual(result["endpoint"], "cmd:bl usage free --output json")
        # only rows that are Text, still have quota and are not expired
        self.assertEqual(result["models"], ["deepseek-v4.1-flash"])
        self.assertEqual(result["count"], 1)
        # the paid /models dump is never consulted for a free channel
        pm.assert_not_called()
        self.assertEqual(self.upstream.seen, [])
        rc.assert_called_once()
        self.assertEqual(rc.call_args.kwargs.get("extra_env"),
                         {"BAILIAN_ACCESS_KEY_ID": "ak-ui",
                          "BAILIAN_ACCESS_KEY_SECRET": "sk-ui"})
        view = self.app.store.catalog_view("plan-a")
        self.assertEqual(view["endpoint"], "cmd:bl usage free --output json")

    def test_probe_channel_models_free_quota_without_credentials_refuses(self):
        import os
        self._bailian_providers()
        env = dict(os.environ)
        env["BAILIAN_ACCESS_KEY_ID"] = ""
        env["BAILIAN_ACCESS_KEY_SECRET"] = ""
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("facet.gateway.server.probe_models") as pm:
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "bailian", "mode": "payg",
                 "tier": "free"},
                allow_ark_quota=True)
        # Refuse instead of falling back to the full data-plane catalog.
        self.assertFalse(result["ok"])
        self.assertIn("未配置免费额度查询凭据", result["error"])
        self.assertIn("BAILIAN_ACCESS_KEY_ID", result["error"])
        pm.assert_not_called()
        self.assertEqual(self.upstream.seen, [])

    def test_probe_channel_models_free_quota_draft_credentials_win(self):
        import dataclasses
        self._bailian_providers()
        self.app.config = dataclasses.replace(
            self.app.config,
            credentials={"bailian": {"BAILIAN_ACCESS_KEY_ID": "ak-saved",
                                     "BAILIAN_ACCESS_KEY_SECRET": "sk-saved"}})
        draft = {"name": "plan-a", "provider": "bailian", "mode": "payg",
                 "tier": "free",
                 "credentials": {"bailian": {
                     "BAILIAN_ACCESS_KEY_ID": "ak-draft",
                     "BAILIAN_ACCESS_KEY_SECRET": "sk-draft"}}}
        with mock.patch("facet.gateway.server.freealign_mod.run_vendor_cmd",
                        return_value="[]") as rc:
            result = self.app.probe_channel_models(draft, allow_ark_quota=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 0)
        self.assertIn("免费额度", result["hint"])
        self.assertEqual(rc.call_args.kwargs.get("extra_env"),
                         {"BAILIAN_ACCESS_KEY_ID": "ak-draft",
                          "BAILIAN_ACCESS_KEY_SECRET": "sk-draft"})

    def test_probe_channel_models_free_quota_command_failure_reported(self):
        import dataclasses
        self._bailian_providers()
        self.app.config = dataclasses.replace(
            self.app.config,
            credentials={"bailian": {"BAILIAN_ACCESS_KEY_ID": "ak-bad",
                                     "BAILIAN_ACCESS_KEY_SECRET": "sk-bad"}})
        with mock.patch("facet.gateway.server.freealign_mod.run_vendor_cmd",
                        side_effect=RuntimeError(
                            "命令退出码 3：No console access token found.")) as rc, \
             mock.patch("facet.gateway.server.probe_models") as pm:
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "bailian", "mode": "payg",
                 "tier": "free"},
                allow_ark_quota=True)
        self.assertFalse(result["ok"])
        self.assertIn("免费额度命令执行失败", result["error"])
        self.assertIn("No console access token", result["error"])
        rc.assert_called_once()
        pm.assert_not_called()

    def test_probe_channel_models_free_quota_ignores_paid_tier(self):
        import dataclasses
        self._bailian_providers()
        self.app.config = dataclasses.replace(
            self.app.config,
            credentials={"bailian": {"BAILIAN_ACCESS_KEY_ID": "ak-ui",
                                     "BAILIAN_ACCESS_KEY_SECRET": "sk-ui"}})
        # same vendor/mode but not a free-tier channel -> plain /models pull
        with mock.patch("facet.gateway.server.freealign_mod.run_vendor_cmd") as rc:
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "bailian", "mode": "payg"},
                allow_ark_quota=True)
        self.assertTrue(result["ok"])
        self.assertNotEqual(result.get("source"), "free-quota")
        rc.assert_not_called()

    def test_probe_channel_models_free_quota_requires_console_flag(self):
        import dataclasses
        self._bailian_providers()
        self.app.config = dataclasses.replace(
            self.app.config,
            credentials={"bailian": {"BAILIAN_ACCESS_KEY_ID": "ak-ui",
                                     "BAILIAN_ACCESS_KEY_SECRET": "sk-ui"}})
        # the scheduled catalog sweep has no console flag -> stays on /models;
        # quota alignment for free channels belongs to the free-align sweep
        with mock.patch("facet.gateway.server.freealign_mod.run_vendor_cmd") as rc:
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "bailian", "mode": "payg",
                 "tier": "free"})
        self.assertTrue(result["ok"])
        self.assertNotEqual(result.get("source"), "free-quota")
        self.assertEqual(result["models"], ["model-a", "model-b"])
        rc.assert_not_called()

    def test_background_catalog_sweep_keeps_data_plane(self):
        import dataclasses
        self.app.config = dataclasses.replace(
            self.app.config,
            credentials={"volcengine": {"VOLC_ACCESSKEY": "ak-ui",
                                        "VOLC_SECRETKEY": "sk-ui"}})
        with mock.patch("facet.gateway.server.ark_quota.list_items") as li:
            result = self.app.probe_channel_models(
                {"name": "plan-a", "provider": "volcengine", "mode": "payg"})
        li.assert_not_called()
        self.assertNotIn("source", result)
        self.assertEqual(result["models"], ["model-a", "model-b"])

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
