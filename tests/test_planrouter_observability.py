"""Routing audit trail, streamed usage, count_tokens and management hardening."""
from __future__ import annotations

import contextlib
import io
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

from planrouter.audit import RequestLog
from planrouter.cli import main
from planrouter.core import load_config, parse_config
from planrouter.server import App, Handler

OPENAI_SSE = [
    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
    b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n',
    b'data: [DONE]\n\n',
]
ANTHROPIC_SSE = [
    b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":11,"output_tokens":0}}}\n\n',
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen.append(body)  # type: ignore[attr-defined]
        status = self.server.status  # type: ignore[attr-defined]
        self.send_response(status)
        if status != 200:
            data = b'{"error":{"message":"usage limit reached"}}'
            self.send_header("Retry-After", "7200")
            content_type = "application/json"
        elif body.get("stream"):
            data = b"".join(self.server.stream_events)  # type: ignore[attr-defined]
            content_type = "text/event-stream"
        else:
            data = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"total_tokens": 42},
            }).encode()
            content_type = "application/json"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def start_upstream():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    httpd.seen = []  # type: ignore[attr-defined]
    httpd.status = 200  # type: ignore[attr-defined]
    httpd.stream_events = OPENAI_SSE  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def base_config(url: str) -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "plan-a", "protocol": "openai", "base_url": url,
             "models": {"auto": "wire-a"}, "priority": 10},
            {"name": "plan-b", "protocol": "openai", "base_url": url,
             "models": {"auto": "wire-b"}, "priority": 20},
            {"name": "claude", "protocol": "anthropic", "base_url": url,
             "models": {"auto": "claude-sonnet-4-5"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["plan-a", "plan-b"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
        ],
    }


class AuditUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "requests.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_routing_metadata_is_written(self):
        log = RequestLog(self.path)
        log.record(model="facet-auto", channel="plan-a", status="ok", ms=3.5,
                   messages=[{"role": "user", "content": "SECRET-PROMPT"}],
                   api_key="sk-secret", prompt="SECRET-PROMPT")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET-PROMPT", text)
        self.assertNotIn("sk-secret", text)
        entry = log.recent()[0]
        self.assertEqual(entry["model"], "facet-auto")
        self.assertEqual(entry["ms"], 3.5)
        self.assertNotIn("messages", entry)
        self.assertIn("ts", entry)

    def test_recent_is_reverse_chronological(self):
        log = RequestLog(self.path)
        for index in range(5):
            log.record(model=f"m{index}", status="ok")
        self.assertEqual([e["model"] for e in log.recent(3)], ["m4", "m3", "m2"])

    def test_log_is_bounded(self):
        log = RequestLog(self.path, max_lines=10, max_bytes=1024)
        for index in range(100):
            log.record(model="facet-auto", status="ok", tokens=index)
        self.assertLessEqual(len(log.recent(500)), 10)
        self.assertLessEqual(self.path.stat().st_size, 64 * 1024)

    def test_track_stream_usage_validation(self):
        raw = base_config("http://127.0.0.1:9000")
        raw["defaults"] = {"track_stream_usage": "false"}
        with self.assertRaisesRegex(ValueError, "track_stream_usage"):
            parse_config(raw)
        raw["defaults"] = {"track_stream_usage": False}
        self.assertFalse(parse_config(raw).defaults.track_stream_usage)


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
        self.upstream.seen.clear()
        self.upstream.status = 200
        self.upstream.stream_events = OPENAI_SSE
        self.config_path.write_text(json.dumps(config or base_config(self.url)), encoding="utf-8")
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

    def request(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"} if data else {}
        hdrs.update(headers or {})
        req = Request(self.base + path, data=data, headers=hdrs, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def completion(self, path="/v1/chat/completions", **extra):
        body = {"model": "facet-auto", "messages": [{"role": "user", "content": "hi"}]}
        body.update(extra)
        return self.request("POST", path, body)


class EndpointTests(GatewayFixture):
    def test_count_tokens_is_local_and_logged(self):
        self.start()
        status, hdrs, raw = self.request("POST", "/v1/messages/count_tokens", {
            "model": "facet-auto", "messages": [{"role": "user", "content": "hello world"}],
        })
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(raw)["input_tokens"], 0)
        self.assertEqual(hdrs.get("X-Token-Estimate"), "local-heuristic")
        self.assertEqual(self.upstream.seen, [])
        entry = self.httpd.app.audit.recent()[0]  # type: ignore[union-attr]
        self.assertEqual(entry["status"], "local")
        self.assertEqual(entry["mode"], "local")

    def test_openai_stream_usage_is_counted(self):
        self.start()
        status, _, raw = self.completion(stream=True)
        self.assertEqual(status, 200)
        self.assertIn(b"hi", raw)
        self.assertIn(b"[DONE]", raw)
        self.assertEqual(self.upstream.seen[-1]["stream_options"], {"include_usage": True})
        stats = json.loads(self.request("GET", "/stats")[2])["channels"]
        self.assertEqual(stats["plan-a"]["total_tokens"], 15)
        entry = self.httpd.app.audit.recent()[0]  # type: ignore[union-attr]
        self.assertEqual(entry["tokens"], 15)
        self.assertTrue(entry["stream"])

    def test_anthropic_stream_usage_is_counted(self):
        self.start()
        self.upstream.stream_events = ANTHROPIC_SSE
        status, _, raw = self.completion("/v1/messages", stream=True)
        self.assertEqual(status, 200)
        self.assertIn(b"message_delta", raw)
        stats = json.loads(self.request("GET", "/stats")[2])["channels"]
        self.assertEqual(stats["claude"]["total_tokens"], 18)
        self.assertEqual(self.httpd.app.audit.recent()[0]["tokens"], 18)  # type: ignore[union-attr]

    def test_stream_usage_tracking_can_be_disabled(self):
        config = base_config(self.url)
        config["defaults"] = {"track_stream_usage": False}
        self.start(config)
        status, _, _ = self.completion(stream=True)
        self.assertEqual(status, 200)
        self.assertNotIn("stream_options", self.upstream.seen[-1])

    def test_audit_api_exposes_metadata_only(self):
        self.start()
        self.completion()
        status, _, raw = self.request("GET", "/api/requests/10")
        self.assertEqual(status, 200)
        text = raw.decode("utf-8")
        self.assertNotIn("SECRET", text)
        self.assertNotIn('"messages"', text)
        payload = json.loads(raw)
        entry = payload["entries"][0]
        self.assertEqual(entry["model"], "facet-auto")
        self.assertEqual(entry["channel"], "plan-a")
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["tokens"], 42)
        self.assertEqual(entry["attempt"], 0)
        self.assertIn("ms", entry)

    def test_failed_attempts_are_logged_with_cooldown(self):
        self.start()
        self.upstream.status = 429
        status, _, _ = self.completion()
        self.assertEqual(status, 503)
        entries = self.httpd.app.audit.recent()  # type: ignore[union-attr]
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["status"] == "upstream_error" for e in entries))
        self.assertTrue(all(e["cooldown_seconds"] >= 7200 for e in entries))

    def test_no_candidate_is_logged(self):
        self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        now = time.time()
        for name in ("plan-a", "plan-b"):
            app.store.states[name].mark_cooldown(3600, "manual", now)
        status, _, _ = self.completion()
        self.assertEqual(status, 503)
        self.assertEqual(app.audit.recent()[0]["status"], "no_candidate")


class SecurityTests(GatewayFixture):
    def test_admin_key_is_separate_from_gateway_key(self):
        config = base_config(self.url)
        config["listen"] = {"host": "127.0.0.1", "port": 0,
                            "api_key": "tool-key", "admin_key": "admin-secret"}
        self.start(config)
        self.assertEqual(self.request("GET", "/api/config")[0], 401)
        self.assertEqual(self.request("GET", "/api/config",
                                      headers={"Authorization": "Bearer tool-key"})[0], 401)
        status, _, raw = self.request("GET", "/api/config",
                                      headers={"X-Admin-Key": "admin-secret"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["listen"]["admin_key"], "***")
        self.assertEqual(self.request("GET", "/health",
                                      headers={"Authorization": "Bearer tool-key"})[0], 200)
        self.assertEqual(self.request("GET", "/")[0], 200)

    def test_masked_admin_key_survives_config_save(self):
        config = base_config(self.url)
        config["listen"] = {"host": "127.0.0.1", "port": 0, "admin_key": "admin-secret"}
        self.start(config)
        admin = {"X-Admin-Key": "admin-secret"}
        _, _, raw = self.request("GET", "/api/config", headers=admin)
        visible = json.loads(raw)
        self.assertEqual(visible["listen"]["admin_key"], "***")
        status, _, _ = self.request("POST", "/api/config", visible, headers=admin)
        self.assertEqual(status, 200)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["listen"]["admin_key"], "admin-secret")

    def test_cross_site_writes_are_rejected(self):
        self.start()
        self.assertEqual(self.request("POST", "/api/reload",
                                      headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/reload",
                                      headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        port = self.httpd.server_address[1]  # type: ignore[union-attr]
        same = {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"}
        status, _, _ = self.request("POST", "/api/reload", headers=same)
        self.assertEqual(status, 200)


class CliAndConsoleTests(unittest.TestCase):
    def test_check_reports_security_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            path.write_text(json.dumps(base_config("http://127.0.0.1:9000")), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--config", str(path), "--check"])
            text = out.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("audit log:", text)
            self.assertIn("WARNING: listen.api_key is unset", text)
            self.assertIn("WARNING: listen.admin_key is unset", text)

    def test_console_exposes_request_log_and_admin_key(self):
        from planrouter.ui import STATIC_DIR
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="requests-body"', html)
        self.assertIn('id="btn-admin-key"', html)
        self.assertIn("loadRequests", js)
        self.assertIn("X-Admin-Key", js)


if __name__ == "__main__":
    unittest.main()
