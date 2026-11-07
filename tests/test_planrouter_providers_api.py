"""Tests for the vendor registry console APIs: save / delete / maintain."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from planrouter import providers as providers_mod
from planrouter.core import load_config
from planrouter.server import App, Handler


def config_doc() -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0, "admin_key": "adm"},
        "channels": [
            {"name": "a", "protocol": "openai",
             "base_url": "http://127.0.0.1:9201", "api_key": "k",
             "models": {"auto": "m-a"}},
            {"name": "claude", "protocol": "anthropic",
             "base_url": "http://127.0.0.1:9204", "models": {"auto": "m-c"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["a"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
        ],
    }


def vendor_payload(pid: str = "acme") -> dict:
    return {
        "id": pid, "name": "Acme AI",
        "modes": {"payg": {
            "label": "按量付费", "protocol": "openai",
            "base_url": "https://api.acme.com/v1", "tier": "paid",
            "docs_url": "", "models_url": "https://api.acme.com/v1/models",
            "models": ["acme-small"],
        }},
    }


class SaveProviderAppTests(unittest.TestCase):
    """App-level behaviour: normalize, persist, reload and fall back."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.path.write_text(json.dumps(config_doc()), encoding="utf-8")
        self.app = App(load_config(self.path), config_path=self.path)

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        for suffix in (".tmp", ".bak"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        for name in ("providers.json", "providers.json.bak",
                     "providers.json.tmp", "maintenance.jsonl"):
            (self.path.parent / name).unlink(missing_ok=True)

    def test_save_provider_persists_and_reloads(self):
        entry = self.app.save_provider(vendor_payload())
        self.assertEqual(entry["id"], "acme")
        self.assertEqual(entry["modes"]["payg"]["models"], ["acme-small"])
        self.assertIn(entry, self.app.providers)
        data = json.loads((self.path.parent / "providers.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual([p["id"] for p in data["providers"]], ["acme"])

    def test_save_provider_merges_with_builtin_entry(self):
        payload = vendor_payload("zhipu")
        payload["modes"]["payg"]["docs_url"] = "https://docs.zhipu.example/payg"
        entry = self.app.save_provider(payload)
        merged = next(p for p in self.app.providers if p["id"] == "zhipu")
        self.assertIs(entry, merged)
        self.assertIn("payg", merged["modes"])

    def test_save_provider_normalizes_payload(self):
        payload = vendor_payload()
        payload["modes"]["payg"]["models"] = [" x ", ""]
        entry = self.app.save_provider(payload)
        self.assertEqual(entry["modes"]["payg"]["models"], ["x"])

    def test_save_provider_rejects_invalid(self):
        with self.assertRaises(ValueError):
            self.app.save_provider(vendor_payload(pid="Bad_Id"))
        self.assertFalse((self.path.parent / "providers.json").exists())

    def test_delete_provider_restores_builtin(self):
        self.app.save_provider(vendor_payload("zhipu"))
        remaining = self.app.delete_provider("zhipu")
        data = json.loads((self.path.parent / "providers.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(data["providers"], [])
        builtin = providers_mod.load_providers(None)
        by_id = {p["id"]: p for p in remaining}
        self.assertEqual(by_id["zhipu"],
                         next(p for p in builtin if p["id"] == "zhipu"))

    def test_maintain_providers_returns_report_and_passes_vendor_id(self):
        seen = {}

        def fake_refresh(app, report, vendor_id=None):
            seen["vendor_id"] = vendor_id
            report["applied"].append({"action": "update_provider_models"})

        with mock.patch("planrouter.maintenance.refresh_providers",
                        side_effect=fake_refresh):
            report = self.app.maintain_providers("acme")
            self.assertEqual(seen["vendor_id"], "acme")
            self.assertEqual(report["applied"],
                             [{"action": "update_provider_models"}])
            self.assertEqual(report["alerts"], [])
            self.app.maintain_providers()
            self.assertIsNone(seen["vendor_id"])


class ProviderEndpointTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.path.write_text(json.dumps(config_doc()), encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(load_config(self.path),
                             config_path=self.path)  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.admin = {"X-Admin-Key": "adm"}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.path.unlink(missing_ok=True)
        for suffix in (".tmp", ".bak"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        for name in ("providers.json", "providers.json.bak",
                     "providers.json.tmp", "maintenance.jsonl"):
            (self.path.parent / name).unlink(missing_ok=True)

    def send(self, path: str, body=None, headers: dict | None = None,
             method: str | None = None):
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            hdrs["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = Request(self.base + path, data=data, headers=hdrs,
                      method=method or ("POST" if body is not None else "GET"))
        try:
            with urlopen(req, timeout=8) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_get_marks_overlay_origin(self):
        status, data = self.send("/api/providers", headers=self.admin)
        self.assertEqual(status, 200)
        self.assertTrue(data["providers"])
        self.assertTrue(all(p["origin"] == "builtin"
                            for p in data["providers"]))

        status, data = self.send("/api/providers", vendor_payload(), self.admin)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["provider"]["id"], "acme")

        status, data = self.send("/api/providers", headers=self.admin)
        by_id = {p["id"]: p for p in data["providers"]}
        self.assertEqual(by_id["acme"]["origin"], "overlay")
        self.assertEqual(by_id["zhipu"]["origin"], "builtin")

    def test_get_requires_admin_key(self):
        status, _ = self.send("/api/providers")
        self.assertEqual(status, 401)

    def test_post_without_admin_key_401(self):
        status, _ = self.send("/api/providers", vendor_payload())
        self.assertEqual(status, 401)

    def test_post_invalid_payload_400(self):
        status, data = self.send("/api/providers",
                                 vendor_payload(pid="Bad!"), self.admin)
        self.assertEqual(status, 400)
        self.assertIn("小写字母", data["error"]["message"])

    def test_post_invalid_json_400(self):
        req = Request(self.base + "/api/providers", data=b"{ not json",
                      headers={"Content-Type": "application/json",
                               "X-Admin-Key": "adm"}, method="POST")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=8)
        self.assertEqual(ctx.exception.code, 400)

    def test_delete_restores_builtin(self):
        self.send("/api/providers", vendor_payload(), self.admin)
        status, data = self.send("/api/providers/acme", method="DELETE",
                                 headers=self.admin)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertNotIn("acme", [p["id"] for p in data["providers"]])
        status, data = self.send("/api/providers", headers=self.admin)
        self.assertTrue(all(p["origin"] == "builtin"
                            for p in data["providers"]))

    def test_maintain_returns_report(self):
        def fake_refresh(app, report, vendor_id=None):
            report["applied"].append({"vendor": vendor_id or "all"})

        with mock.patch("planrouter.maintenance.refresh_providers",
                        side_effect=fake_refresh):
            status, data = self.send("/api/providers/maintain", {}, self.admin)
            self.assertEqual(status, 200)
            self.assertEqual(data["applied"], [{"vendor": "all"}])
            status, data = self.send("/api/providers/maintain",
                                     {"id": "zhipu"}, self.admin)
            self.assertEqual(status, 200)
            self.assertEqual(data["applied"], [{"vendor": "zhipu"}])

    def test_maintain_without_admin_key_401(self):
        status, _ = self.send("/api/providers/maintain", {})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
