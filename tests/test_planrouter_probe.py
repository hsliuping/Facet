"""Tests for the console's channel / subscription connectivity probes."""
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

from planrouter.core import load_config, upstream_path_for
from planrouter.probe import describe_key, pick_model, validate_channel
from planrouter.server import App, Handler


def config_for(upstream_url: str, gateway_key=None, admin_key=None,
               key: str = "top-secret") -> dict:
    listen = {"host": "127.0.0.1", "port": 0}
    if gateway_key:
        listen["api_key"] = gateway_key
    if admin_key:
        listen["admin_key"] = admin_key
    return {
        "listen": listen,
        "channels": [
            {"name": "plan-a", "protocol": "openai", "base_url": upstream_url,
             "api_key": key, "models": {"auto": "model-a"}, "priority": 10},
            {"name": "claude", "protocol": "anthropic", "base_url": upstream_url,
             "api_key": key, "models": {"auto": "claude-x"}, "priority": 20},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["plan-a"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
        ],
    }


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen.append({  # type: ignore[attr-defined]
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "x-api-key": self.headers.get("x-api-key"),
            "anthropic-version": self.headers.get("anthropic-version"),
            "body": body,
        })
        model = str(body.get("model") or "")
        if model == "unauthorized":
            code, payload = 401, {"error": {"message": "invalid api key"}}
        elif model == "notfound":
            code, payload = 404, {"error": {"message": "unknown route"}}
        elif model == "limited":
            code, payload = 429, {"error": {"message": "quota exceeded"}}
        elif self.path.endswith("/messages"):
            code, payload = 200, {
                "content": [{"type": "text", "text": "pong-anthropic"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        else:
            code, payload = 200, {
                "choices": [{"message": {"role": "assistant", "content": "pong-openai"}}],
                "usage": {"total_tokens": 2},
            }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ProbeUnitTests(unittest.TestCase):
    def test_pick_model(self):
        self.assertEqual(pick_model({"auto": "wire-a", "gpt": "wire-b"}), "wire-a")
        self.assertEqual(pick_model({"gpt": "wire-b"}), "wire-b")
        self.assertIsNone(pick_model({"*": "*", "auto": "*"}))
        self.assertIsNone(pick_model({"auto": "REPLACE_WITH_A_MODEL"}))
        self.assertEqual(pick_model({"exposed": "wire"}, "exposed"), "wire")
        self.assertEqual(pick_model({}, "raw-name"), "raw-name")

    def test_validate_channel(self):
        with self.assertRaises(ValueError):
            validate_channel({"name": "x"})
        with self.assertRaises(ValueError):
            validate_channel({"name": "x", "base_url": "ftp://host"})
        with self.assertRaises(ValueError):
            validate_channel({"name": "x", "base_url": "http://host",
                              "protocol": "unknown"})
        with self.assertRaises(ValueError):
            validate_channel({"name": "x", "base_url": "http://host",
                              "upstream_path": "chat/completions"})
        with self.assertRaises(ValueError):
            validate_channel({"name": "x", "base_url": "http://host",
                              "models": {"auto": "m"},
                              "proxy": "socks5://127.0.0.1:7890"})
        good = validate_channel({
            "name": "x", "base_url": "http://host/", "api_key": "***",
            "models": {"auto": "m"}, "upstream_path": "/chat/completions/",
        })
        self.assertEqual(good["base_url"], "http://host")
        self.assertIsNone(good["api_key"])
        self.assertEqual(good["upstream_path"], "/chat/completions")
        self.assertEqual(good["proxy"], "")
        proxied = validate_channel({
            "name": "x", "base_url": "http://host", "api_key": "***",
            "models": {"auto": "m"}, "proxy": "http://127.0.0.1:7890",
        })
        self.assertEqual(proxied["proxy"], "http://127.0.0.1:7890")

    def test_upstream_path_for_avoids_duplicate_version(self):
        self.assertEqual(upstream_path_for("http://host", None, "openai"),
                         "/v1/chat/completions")
        self.assertEqual(upstream_path_for("http://host", None, "anthropic"),
                         "/v1/messages")
        for versioned in ("http://host/v1", "http://host/api/v3",
                          "https://ark.cn-beijing.volces.com/api/coding/v3",
                          "http://host/v1beta", "http://host/v4"):
            self.assertEqual(upstream_path_for(versioned, None, "openai"),
                             "/chat/completions")
            self.assertEqual(upstream_path_for(versioned, None, "anthropic"),
                             "/messages")
        self.assertEqual(upstream_path_for("http://host", "/custom", "openai"),
                         "/custom")

    def test_describe_key_explains_env_prefix_misuse(self):
        # repo rule: key material lives in env vars / config references only —
        # fixtures are runtime-built so no key-shaped string lands in source
        # (keeps secret scanners permanently quiet).
        bad = describe_key({"api_key": "env:" + "not-".join(["a", "valid-env-var", "name"]) + "-9f3a"})
        self.assertEqual(bad["source"], "env-ref")
        self.assertFalse(bad["env_var_valid_name"])
        self.assertFalse(bad["resolved"])
        self.assertIn("去掉 env:", bad["note"])
        self.assertNotIn("not-a", bad["raw"])

        missing = describe_key({"api_key": "env:PLANROUTER_TEST_UNSET_VAR_9F3A"})
        self.assertTrue(missing["env_var_valid_name"])
        self.assertFalse(missing["env_var_present"])
        self.assertFalse(missing["resolved"])

        plain = describe_key({"api_key": "sk-" + "x" * 16})
        self.assertEqual(plain["source"], "literal")
        self.assertTrue(plain["resolved"])
        self.assertNotIn("x" * 16, plain["raw"])

        for empty in ({}, {"api_key": ""}, {"api_key": "***"}):
            self.assertFalse(describe_key(empty)["resolved"])


class ProbeGatewayTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream.daemon_threads = True
        self.upstream.seen = []  # type: ignore[attr-defined]
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.upstream_url = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.httpd = None

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.path.unlink(missing_ok=True)
        Path(str(self.path) + ".tmp").unlink(missing_ok=True)
        (self.path.parent / "probe.jsonl").unlink(missing_ok=True)

    def start(self, config: dict | None = None) -> str:
        raw = config if config is not None else config_for(self.upstream_url)
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(load_config(self.path), config_path=self.path)  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def call(self, base: str, path: str, body, headers: dict | None = None):
        hdrs = dict(headers or {})
        hdrs["Content-Type"] = "application/json"
        req = Request(base + path, data=json.dumps(body).encode("utf-8"),
                      headers=hdrs, method="POST")
        try:
            with urlopen(req, timeout=8) as resp:
                return resp.status, resp.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def probe(self, base: str, channels, model=None, headers=None):
        body = {"channels": channels}
        if model is not None:
            body["model"] = model
        status, raw = self.call(base, "/api/test/channels", body, headers)
        return status, json.loads(raw)

    def draft(self, **kwargs) -> dict:
        channel = {
            "name": "draft", "protocol": "openai",
            "base_url": self.upstream_url, "api_key": "draft-key",
            "models": {"auto": "model-a"},
        }
        channel.update(kwargs)
        return channel

    def test_openai_draft_probe(self):
        base = self.start()
        status, data = self.probe(base, [self.draft()])
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        result = data["results"][0]
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["path"], "/v1/chat/completions")
        self.assertEqual(result["model"], "model-a")
        self.assertEqual(result["reply"], "pong-openai")
        self.assertIn("latency_ms", result)
        self.assertIn("不计入冷却", data["note"])
        seen = self.upstream.seen[-1]
        self.assertEqual(seen["auth"], "Bearer draft-key")
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["body"]["max_tokens"], 16)
        self.assertIs(seen["body"]["stream"], False)

    def test_anthropic_probe_with_custom_path(self):
        base = self.start()
        status, data = self.probe(base, [self.draft(
            protocol="anthropic", upstream_path="/custom/messages",
            models={"auto": "claude-x"}, api_key="anthropic-key",
        )])
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        result = data["results"][0]
        self.assertEqual(result["path"], "/custom/messages")
        self.assertEqual(result["reply"], "pong-anthropic")
        seen = self.upstream.seen[-1]
        self.assertEqual(seen["x-api-key"], "anthropic-key")
        self.assertEqual(seen["anthropic-version"], "2023-06-01")
        self.assertIsNone(seen["auth"])

    def test_explicit_model_overrides_auto(self):
        base = self.start()
        _, data = self.probe(base, [self.draft(models={"gpt-x": "wire-x"})], model="gpt-x")
        self.assertEqual(data["results"][0]["model"], "wire-x")
        self.assertEqual(self.upstream.seen[-1]["body"]["model"], "wire-x")

    def test_masked_secret_is_restored_from_saved_config(self):
        base = self.start()
        status, data = self.probe(base, [self.draft(name="plan-a", api_key="***")])
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(self.upstream.seen[-1]["auth"], "Bearer top-secret")

    def test_subscription_bare_names_resolve_from_saved_config(self):
        base = self.start()
        status, data = self.probe(base, ["plan-a", "claude"])
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual([r["name"] for r in data["results"]], ["plan-a", "claude"])
        self.assertEqual({seen["path"] for seen in self.upstream.seen},
                         {"/v1/chat/completions", "/v1/messages"})

    def test_upstream_errors_include_actionable_hints(self):
        base = self.start()
        for model, code, word, message in (
            ("unauthorized", 401, "密钥", "invalid api key"),
            ("notfound", 404, "路径", "unknown route"),
            ("limited", 429, "额度", "quota exceeded"),
        ):
            _, data = self.probe(base, [self.draft(models={"auto": model})])
            result = data["results"][0]
            self.assertFalse(result["ok"])
            self.assertEqual(result["http_status"], code)
            self.assertIn(word, result["hint"])
            self.assertEqual(result["error"], message)

    def test_network_failure_is_reported(self):
        base = self.start()
        _, data = self.probe(base, [self.draft(base_url="http://127.0.0.1:1")])
        result = data["results"][0]
        self.assertFalse(result["ok"])
        self.assertIn("无法连接", result["hint"])

    def test_no_usable_model_is_reported(self):
        base = self.start()
        _, data = self.probe(base, [self.draft(models={"*": "*"})])
        result = data["results"][0]
        self.assertFalse(result["ok"])
        self.assertIn("没有可用的测试模型", result["error"])

    def test_invalid_draft_is_reported_per_channel(self):
        base = self.start()
        status, data = self.probe(base, [self.draft(base_url="ftp://host")])
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("base_url", data["results"][0]["error"])

    def test_probe_does_not_change_live_channel_state(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        state = app.store.states["plan-a"]
        self.probe(base, ["plan-a"])
        self.assertEqual(state.total_requests, 0)
        self.assertEqual(state.failed_requests, 0)
        self.assertEqual(state.consecutive_errors, 0)
        self.assertEqual(state.cooldown_until, 0.0)
        self.assertEqual(state.quota, {})

    def test_401_hint_distinguishes_missing_key_from_invalid_key(self):
        base = self.start()
        status, data = self.probe(
            base, [self.draft(models={"auto": "unauthorized"}, api_key="")])
        self.assertEqual(status, 200)
        result = data["results"][0]
        self.assertEqual(result["http_status"], 401)
        self.assertEqual(result["auth"], "none")
        self.assertIn("没有携带密钥", result["hint"])
        self.assertTrue(result["url"].endswith("/v1/chat/completions"))
        self.assertIsNone(self.upstream.seen[-1]["auth"])

        _, data = self.probe(base, [self.draft(models={"auto": "unauthorized"})])
        result = data["results"][0]
        self.assertEqual(result["auth"], "bearer")
        self.assertIn("密钥已发送", result["hint"])
        self.assertEqual(self.upstream.seen[-1]["auth"], "Bearer draft-key")

    def test_probe_log_records_masked_request_and_response(self):
        base = self.start()
        status, data = self.probe(base, [self.draft()])
        self.assertEqual(status, 200)
        result = data["results"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["auth"], "bearer")
        self.assertEqual(result["key"]["source"], "literal")
        request = result["debug"]["request"]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["body"]["model"], "model-a")
        self.assertIn("Bearer", request["headers"]["Authorization"])
        self.assertNotIn("draft-key", request["headers"]["Authorization"])
        self.assertEqual(result["debug"]["response"]["http_status"], 200)

        log = (self.path.parent / "probe.jsonl").read_text(encoding="utf-8")
        self.assertIn('"request"', log)
        self.assertNotIn("draft-key", log)
        self.assertNotIn("top-secret", log)

    def test_probe_and_forward_share_versioned_base_path(self):
        versioned = self.upstream_url + "/api/coding/v3"
        base = self.start(config_for(versioned))
        _, data = self.probe(base, [self.draft(base_url=versioned)])
        result = data["results"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "/chat/completions")
        self.assertTrue(result["url"].endswith("/api/coding/v3/chat/completions"))

        req = Request(base + "/v1/chat/completions",
                      data=json.dumps({"model": "model-a",
                                       "messages": [{"role": "user", "content": "hi"}]}).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=8) as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(self.upstream.seen[-1]["path"],
                         "/api/coding/v3/chat/completions")

    def test_endpoint_requires_admin_key(self):
        base = self.start(config_for(self.upstream_url, gateway_key="gw",
                                     admin_key="adm"))
        self.assertEqual(self.probe(base, ["plan-a"])[0], 401)
        self.assertEqual(self.probe(base, ["plan-a"],
                                    headers={"X-Admin-Key": "wrong"})[0], 401)
        self.assertEqual(self.probe(base, ["plan-a"],
                                    headers={"X-Admin-Key": "adm"})[0], 200)

    def test_cross_site_post_is_rejected(self):
        base = self.start(config_for(self.upstream_url, gateway_key="gw",
                                     admin_key="adm"))
        status, _ = self.probe(base, ["plan-a"], headers={
            "X-Admin-Key": "adm", "Origin": "http://evil.example",
        })
        self.assertEqual(status, 403)

    def test_payload_limits(self):
        base = self.start()
        self.assertEqual(self.probe(base, [])[0], 400)
        self.assertEqual(self.probe(base, "plan-a")[0], 400)
        self.assertEqual(self.probe(base, ["plan-a"] * 9)[0], 400)
        self.assertEqual(self.probe(base, [42])[0], 400)

    def test_repeated_auto_saves_keep_masked_secret(self):
        """The console now saves on every edit; masked keys must survive."""
        raw = config_for(self.upstream_url)
        base = self.start(raw)
        for _ in range(3):
            payload = json.loads(json.dumps(raw))
            for channel in payload["channels"]:
                channel["api_key"] = "***"
            status, _ = self.call(base, "/api/config", payload)
            self.assertEqual(status, 200)
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(disk["channels"][0]["api_key"], "top-secret")
        self.assertEqual(disk["channels"][1]["api_key"], "top-secret")


class ConsoleAssetsTests(unittest.TestCase):
    def test_console_ships_both_test_buttons(self):
        root = Path(__file__).resolve().parent.parent / "src" / "planrouter" / "static"
        js = (root / "app.js").read_text(encoding="utf-8")
        css = (root / "style.css").read_text(encoding="utf-8")
        self.assertIn('id="editor-test"', js)
        self.assertIn('id="sub-test"', js)
        self.assertIn("testChannelEditor", js)
        self.assertIn("testSubscriptionEditor", js)
        self.assertIn("/api/test/channels", js)
        self.assertIn("upstream_path", js)
        self.assertIn(".test-result", css)
        html = (root / "index.html").read_text(encoding="utf-8")
        # Edits persist on their own; the old "you must click save" hint is gone.
        self.assertIn("改动会自动保存", html)
        self.assertNotIn("改动后点右上角", html)
        self.assertIn("scheduleSave", js)
        self.assertIn("restoreDrafts()", js)
        self.assertIn("stashDraft(DRAFT_CONFIG", js)
        self.assertIn("holdDirty()", js)
        self.assertIn("holdSubDirty()", js)
        self.assertIn(">保存</button>", js)
        # A 401 must show the real URL and whether an auth header was sent.
        self.assertIn("r.auth", js)
        self.assertIn(".test-meta", css)
        self.assertIn("test-debug", js)
        self.assertIn(".test-debug", css)
        self.assertIn("直接粘贴明文 API Key", js)
        self.assertIn("VAR 是变量名", html)
        self.assertIn("留空自动选择", js)
        self.assertIn("/v3/v1/", html)
        # Subscriptions pick channels instead of typing comma-separated names.
        self.assertIn("function channelPicker(", js)
        self.assertIn('name="channels"', js)
        self.assertIn("pickedChannels(f)", js)
        self.assertIn("关联渠道（勾选，可多选）", js)
        self.assertIn(".channel-picker", css)
        self.assertNotIn("关联渠道（逗号分隔）", js)
        # Picking a channel pre-fills vendor / plan without overwriting user edits.
        self.assertIn("function autofillFromChannels(", js)
        self.assertIn("function inferVendor(", js)
        self.assertIn("dataset.autofill", js)
        self.assertIn("自动带出厂家与套餐名称", js)
        self.assertIn(".pick-note", css)

    def test_console_never_calls_native_dialogs(self):
        # Embedded/IDE webviews throw "prompt() is not supported." on 401, which
        # used to surface as "请求失败" when clicking the probe buttons.
        root = Path(__file__).resolve().parent.parent / "src" / "planrouter" / "static"
        js = (root / "app.js").read_text(encoding="utf-8")
        css = (root / "style.css").read_text(encoding="utf-8")
        for banned in ("window.prompt(", "window.confirm(", "window.alert("):
            self.assertNotIn(banned, js)
        self.assertIn("function askText(", js)
        self.assertIn("function askConfirm(", js)
        self.assertIn("promptForAuth(isApi)", js)
        self.assertIn("ask-backdrop", css)
        self.assertIn("readStoredKey('admin')", js)
        self.assertIn("在本机记住", js)
        self.assertIn("请点右上角「管理密钥」", js)
        self.assertIn("await restoreDrafts()", js)


if __name__ == "__main__":
    unittest.main()
