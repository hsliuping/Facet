"""请求详情排障：rid 请求关联、诊断包端点与前端接线。

审计条目新增 rid 字段后，同一次请求的多次 attempt 可以在控制台里
分组成一条时间线；/api/diagnostics 把已脱敏的配置、渠道状态与最近
审计打包给用户导出排障。
"""
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

from facet.gateway.audit import RequestLog
from facet.gateway.cli import main
from facet.gateway.core import load_config
from facet.gateway.server import App, Handler

STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "facet" / "gateway" / "static"


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        status = self.server.status  # type: ignore[attr-defined]
        self.send_response(status)
        if status != 200:
            data = b'{"error":{"message":"usage limit reached"}}'
            self.send_header("Retry-After", "7200")
        else:
            data = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"total_tokens": 10},
            }).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def start_upstream():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    httpd.seen = []  # type: ignore[attr-defined]
    httpd.status = 200  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def base_config(url: str) -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "plan-a", "protocol": "openai", "base_url": url,
             "api_key": "sk-SECRET-1", "models": {"auto": "wire-a"}, "priority": 10},
            {"name": "plan-b", "protocol": "openai", "base_url": url,
             "api_key": "sk-SECRET-2", "models": {"auto": "wire-b"}, "priority": 20},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["plan-a", "plan-b"]},
            {"name": "default", "protocol": "anthropic", "channels": ["plan-a"]},
        ],
    }


class AuditRidTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "requests.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_rid_passes_through_the_whitelist(self):
        log = RequestLog(self.path)
        log.record(rid="ab12cd34", model="facet-auto", status="ok")
        entry = log.recent()[0]
        self.assertEqual(entry["rid"], "ab12cd34")

    def test_entries_without_rid_stay_valid(self):
        log = RequestLog(self.path)
        log.record(model="facet-auto", status="ok")
        self.assertNotIn("rid", log.recent()[0])


class GatewayFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upstream = start_upstream()
        self.url = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        self.config_path = self.root / "router.json"
        self.audit_path = self.root / "requests.jsonl"
        self.httpd = None

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.tmp.cleanup()

    def start(self, config=None):
        self.config_path.write_text(
            json.dumps(config or base_config(self.url)), encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(  # type: ignore[attr-defined]
            load_config(self.config_path),
            config_path=self.config_path,
            usage_file=self.root / "usage.json",
            audit_file=self.audit_path,
        )
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.base + path, data=data,
                      headers={"Content-Type": "application/json"}, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def completion(self):
        return self.request("POST", "/v1/chat/completions", {
            "model": "facet-auto", "messages": [{"role": "user", "content": "hi"}]})


class AttemptRidTests(GatewayFixture):
    def test_attempts_of_one_request_share_a_rid(self):
        self.start()
        self.upstream.status = 429
        status, _ = self.completion()
        self.assertEqual(status, 503)
        entries = self.httpd.app.audit.recent()  # type: ignore[union-attr]
        self.assertEqual(len(entries), 2)
        rids = {e.get("rid") for e in entries}
        self.assertEqual(len(rids), 1)
        self.assertTrue(all(len(r) == 8 for r in rids))

    def test_successful_call_carries_a_rid(self):
        self.start()
        status, _ = self.completion()
        self.assertEqual(status, 200)
        entry = self.httpd.app.audit.recent()[0]  # type: ignore[union-attr]
        self.assertTrue(entry.get("rid"))


class DiagnosticsEndpointTests(GatewayFixture):
    def test_diagnostics_package_is_sanitized_and_complete(self):
        self.start()
        self.completion()
        status, raw = self.request("GET", "/api/diagnostics")
        self.assertEqual(status, 200)
        text = raw.decode("utf-8")
        self.assertNotIn("sk-SECRET", text)
        payload = json.loads(text)
        self.assertIn("generated_at", payload)
        self.assertIn("_version", payload["config"])
        self.assertIn("plan-a", payload["channels"])
        entries = payload["requests"]["entries"]
        self.assertTrue(entries)
        self.assertEqual(entries[0]["channel"], "plan-a")

    def test_diagnostics_without_audit_still_works(self):
        self.start()
        self.httpd.app.audit = None  # type: ignore[attr-defined]
        status, raw = self.request("GET", "/api/diagnostics")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["requests"]["entries"], [])


class StaticWiringTests(unittest.TestCase):
    def _read(self, name):
        return (STATIC_DIR / name).read_text(encoding="utf-8")

    def test_app_js_has_detail_and_filter_and_export(self):
        text = self._read("app.js")
        self.assertIn("openRequestDetail", text)
        self.assertIn("translateRequest", text)
        self.assertIn("requestFilter", text)
        self.assertIn("exportDiagnostics", text)

    def test_index_html_has_filter_and_export_controls(self):
        text = self._read("index.html")
        self.assertIn("requests-status-filter", text)
        self.assertIn("requests-range-filter", text)
        self.assertIn("btn-export-diagnostics", text)


if __name__ == "__main__":
    unittest.main()
