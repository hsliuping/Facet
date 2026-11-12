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
        (self.path.parent / "freealign.jsonl").unlink(missing_ok=True)
        for bak in self.path.parent.glob(self.path.name + ".*.bak"):
            bak.unlink(missing_ok=True)

    def start(self, config: dict | None = None) -> str:
        raw = config if config is not None else base_config()
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(load_config(self.path), config_path=self.path)  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def call(self, base: str, method: str, path: str, body=None, key: str | None = None,
             admin: str | None = None):
        headers = {}
        if key:
            headers["Authorization"] = "Bearer " + key
        if admin is not None:
            headers["X-Admin-Key"] = admin
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

    def test_api_requires_admin_key_but_page_does_not(self):
        base = self.start(base_config(gateway_key="gw-secret"))
        status, _, _ = self.call(base, "GET", "/api/config")
        self.assertEqual(status, 401)
        status, _, _ = self.call(base, "GET", "/api/config", key="gw-secret")
        self.assertEqual(status, 401)
        status, _, _ = self.call(base, "GET", "/api/config", key="admin")
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
            # New channels must carry a vendor binding (see
            # test_planrouter_vendor_binding.py); pollinations/payg is a
            # builtin registry entry.
            "provider": "pollinations", "mode": "payg",
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
        config["routes"][0]["channels"].append("no-such-channel")  # unknown channel
        status, _, body = self.call(base, "POST", "/api/config", config)
        self.assertEqual(status, 400)
        self.assertIn(b"unknown channel", body)
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

    def test_guide_endpoint_serves_manual(self):
        base = self.start()
        status, _, body = self.call(base, "GET", "/api/guide")
        self.assertEqual(status, 200)
        guide = json.loads(body)
        self.assertTrue(guide["available"])
        self.assertIn("3.1 Pollinations", guide["markdown"])
        self.assertIn("5.4 Z.AI（智谱海外）", guide["markdown"])

    def test_guide_image_route_confines_to_docs(self):
        base = self.start()
        # A real file that exists in the repo but outside docs/ must be off-limits.
        status, _, _ = self.call(base, "GET", "/api/guide/img/..%2F..%2Fsrc%2Fplanrouter%2Fproviders.json")
        self.assertEqual(status, 404)
        status, _, _ = self.call(base, "GET", "/api/guide/img/images/api-key-guide/missing.png")
        self.assertEqual(status, 404)

    def test_freealign_logs_reads_jsonl_history(self):
        base = self.start()
        # Written by _record_free_align: the patch itself never hits the disk.
        entry = {"ts": "2026-11-09T10:00:00", "trigger": "scheduled",
                 "aligned": [{"channel": "plan-a", "models": ["m1"], "changed": True}],
                 "alerts": []}
        (self.path.parent / "freealign.jsonl").write_text(
            json.dumps(entry) + "\n", encoding="utf-8")
        status, _, body = self.call(base, "GET", "/api/freealign/logs")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["source"], "file")
        self.assertEqual(payload["logs"][0]["trigger"], "scheduled")
        self.assertNotIn("patch", payload["logs"][0])

    def test_backups_lists_bak_files_newest_first(self):
        base = self.start()
        old = self.path.with_name(self.path.name + ".maintenance.bak")
        new = self.path.with_name(self.path.name + ".freealign.bak")
        old.write_text("{}", encoding="utf-8")
        time.sleep(0.02)
        new.write_text("{}", encoding="utf-8")
        status, _, body = self.call(base, "GET", "/api/backups")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual([b["tag"] for b in payload["backups"]], ["freealign", "maintenance"])
        self.assertTrue(all(b["size"] >= 0 and b["mtime"] > 0 for b in payload["backups"]))
        # The live config itself is never listed as a backup.
        self.assertEqual(payload["config"], self.path.name)

    def test_maintenance_logs_limit_is_parsed(self):
        base = self.start()
        status, _, body = self.call(base, "GET", "/api/maintenance/logs/100")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["limit"], 100)

    # ------------------------------------------------------------------
    # /api/recommend — agent / dev model recommendations.
    # ------------------------------------------------------------------

    def test_recommend_requires_admin_key(self):
        base = self.start(base_config(gateway_key="gw-secret"))
        # Without admin header → 401.
        status, _, _ = self.call(base, "GET", "/api/recommend?purpose=agent")
        self.assertEqual(status, 401)
        # Gateway key alone is not an admin credential.
        status, _, _ = self.call(base, "GET", "/api/recommend", key="gw-secret")
        self.assertEqual(status, 401)
        # Default admin key works.
        status, _, _ = self.call(base, "GET", "/api/recommend?purpose=agent", admin="admin")
        self.assertEqual(status, 200)

    def test_recommend_agent_prefers_tool_call_then_free(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        # Catalogs: gpt-4o resolves to a registry key with tool_call=True;
        # model-a / model-b are unknown to the registry (tool_call=None).
        app.store.record_catalog("plan-a", ["gpt-4o", "model-a"], "http://a/v1")
        app.store.record_catalog("plan-b", ["model-b"], "http://b/v1")
        status, _, body = self.call(base, "GET", "/api/recommend?purpose=agent", admin="admin")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["purpose"], "agent")
        items = payload["items"]
        wires = [it["wire_model"] for it in items]
        # tool_call=True must come before unknown models.
        self.assertEqual(wires[0], "gpt-4o")
        # The known tool-call row is marked matched and tool_call=True.
        top = items[0]
        self.assertTrue(top["matched"])
        self.assertTrue(top["tool_call"])
        # Unknown models are counted in unknown_capability.
        self.assertGreaterEqual(payload["unknown_capability"], 1)

    def test_recommend_agent_excludes_tool_less_models(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        # aimlapi/gemma-2-27b-it is a registry entry with tool_call=False.
        app.store.record_catalog(
            "plan-a", ["aimlapi/gemma-2-27b-it", "gpt-4o"], "http://a/v1")
        status, _, body = self.call(base, "GET", "/api/recommend?purpose=agent",
                                    admin="admin")
        self.assertEqual(status, 200, body)
        wires = [it["wire_model"] for it in json.loads(body)["items"]]
        self.assertNotIn("aimlapi/gemma-2-27b-it", wires)
        self.assertIn("gpt-4o", wires)

    def test_recommend_skips_dead_and_blocked_channels(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        app.store.record_catalog("plan-a", ["gpt-4o", "model-a"], "http://a/v1")
        app.store.record_catalog("plan-b", ["model-b"], "http://b/v1")
        # Mark gpt-4o on plan-a as dead (probe-verified).
        from planrouter.maintenance import label_dead_model
        label_dead_model(app, "plan-a", "gpt-4o", ok=False, error="HTTP 500")
        status, _, body = self.call(base, "GET", "/api/recommend?purpose=agent", admin="admin")
        self.assertEqual(status, 200, body)
        items = json.loads(body)["items"]
        wires = {it["wire_model"]: [it["channel"] for it in items if it["wire_model"] == it["wire_model"]]
                 for it in items}
        # gpt-4o must be excluded entirely: dead on plan-a and not listed on plan-b.
        gpt_rows = [it for it in items if it["wire_model"] == "gpt-4o"]
        self.assertEqual(gpt_rows, [])
        # model-a still comes from plan-a, model-b from plan-b.
        self.assertIn("model-a", wires)
        self.assertIn("model-b", wires)

    def test_recommend_deduplicates_same_wire_across_channels(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        # Same wire model on two channels → one row, the best channel picked.
        app.store.record_catalog("plan-a", ["gpt-4o"], "http://a/v1")
        app.store.record_catalog("plan-b", ["gpt-4o"], "http://b/v1")
        status, _, body = self.call(base, "GET", "/api/recommend?purpose=agent", admin="admin")
        self.assertEqual(status, 200, body)
        items = json.loads(body)["items"]
        gpt_rows = [it for it in items if it["wire_model"] == "gpt-4o"]
        self.assertEqual(len(gpt_rows), 1)
        # Both channels offer the same registry match; pick whichever
        # _row_better prefers (free beats paid, then higher score).
        self.assertIn(gpt_rows[0]["channel"], ("plan-a", "plan-b"))


if __name__ == "__main__":
    unittest.main()
