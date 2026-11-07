"""Preemptive quota handling, subscription quota fields and monthly reports."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from planrouter.core import load_config, parse_config
from planrouter.quota import (parse_quota, quota_exhaustion_wait, quota_ratio,
                              quota_view)
from planrouter.report import build_report
from planrouter.server import App, Handler
from planrouter import subscriptions as subs


def sub_record(**extra):
    record = {
        "vendor": "火山引擎", "account": "账号 A", "plan": "coding plan",
        "price": 199, "currency": "CNY", "billing_cycle": "monthly",
        "started_at": "2026-10-07",
        "expires_at": (date.today() + timedelta(days=60)).isoformat(),
        "channels": ["plan-a"],
    }
    record.update(extra)
    return record


class QuotaUnitTests(unittest.TestCase):
    def test_openai_headers_and_duration_reset(self):
        quota = parse_quota({
            "x-ratelimit-remaining-requests": "12",
            "x-ratelimit-limit-requests": "60",
            "x-ratelimit-remaining-tokens": "800",
            "x-ratelimit-limit-tokens": "1000",
            "x-ratelimit-reset-requests": "1h30m",
        }, now=1000.0)
        self.assertEqual(quota["requests_remaining"], 12.0)
        self.assertEqual(quota["tokens_remaining"], 800.0)
        self.assertEqual(quota["reset_at"], 6400.0)
        self.assertAlmostEqual(quota_ratio(quota), 0.2)
        self.assertAlmostEqual(quota_view(quota, 1000.0)["ratio"], 0.2)

    def test_anthropic_unified_headers_and_iso_reset(self):
        quota = parse_quota({
            "anthropic-ratelimit-unified-requests-remaining": "0",
            "anthropic-ratelimit-unified-requests-limit": "50",
            "anthropic-ratelimit-unified-requests-reset": "2026-11-07T12:00:00Z",
        }, now=1000.0)
        self.assertEqual(quota["requests_remaining"], 0.0)
        self.assertGreater(quota["reset_at"], 1000.0)
        self.assertTrue(quota_view(quota, 1000.0)["exhausted"])
        self.assertEqual(quota_exhaustion_wait(quota, 1000.0, 18000, 7200), 7200.0)

    def test_missing_headers_and_stale_window(self):
        self.assertIsNone(parse_quota({}, now=1000.0))
        self.assertIsNone(quota_ratio(None))
        stale = parse_quota({"x-ratelimit-remaining-requests": "0",
                             "x-ratelimit-reset-requests": "10"}, now=1000.0)
        view = quota_view(stale, 1200.0)
        self.assertFalse(view["known"])
        self.assertTrue(view.get("stale"))

    def test_exhaustion_wait_is_conservative_without_reset(self):
        quota = parse_quota({"x-ratelimit-remaining-requests": "0"}, now=1000.0)
        self.assertEqual(quota_exhaustion_wait(quota, 1000.0, 18000, 7200), 300.0)
        healthy = parse_quota({"x-ratelimit-remaining-requests": "5"}, now=1000.0)
        self.assertIsNone(quota_exhaustion_wait(healthy, 1000.0, 18000, 7200))

    def test_preempt_flag_validation(self):
        raw = {
            "listen": {"host": "127.0.0.1", "port": 0},
            "channels": [
                {"name": "a", "protocol": "openai", "base_url": "http://127.0.0.1:9",
                 "models": {"auto": "m"}},
                {"name": "b", "protocol": "anthropic", "base_url": "http://127.0.0.1:9",
                 "models": {"auto": "m"}},
            ],
            "routes": [
                {"name": "default", "protocol": "openai", "channels": ["a"]},
                {"name": "default", "protocol": "anthropic", "channels": ["b"]},
            ],
        }
        raw["defaults"] = {"preempt_quota": "yes"}
        with self.assertRaisesRegex(ValueError, "preempt_quota"):
            parse_config(raw)
        raw["defaults"] = {"preempt_quota": False}
        self.assertFalse(parse_config(raw).defaults.preempt_quota)


class SubscriptionQuotaTests(unittest.TestCase):
    def test_quota_fields_and_enrichment(self):
        record = subs.normalize(sub_record(
            quota_limit=1000, quota_used=940, quota_unit="requests",
            quota_updated_at="2026-11-07",
        ))
        enriched = subs.enrich(record, date(2026, 11, 7))
        self.assertEqual(enriched["quota_remaining"], 60)
        self.assertAlmostEqual(enriched["quota_ratio"], 0.06)
        self.assertTrue(enriched["low_quota"])
        summary = subs.summarize([record], date(2026, 11, 7))
        self.assertEqual(len(summary["quota_alerts"]), 1)
        self.assertEqual(summary["quota_alerts"][0]["quota_remaining"], 60)

    def test_optional_quota_fields(self):
        record = subs.normalize(sub_record())
        self.assertIsNone(record["quota_limit"])
        self.assertEqual(record["quota_unit"], "")
        enriched = subs.enrich(record, date(2026, 11, 7))
        self.assertIsNone(enriched["quota_remaining"])
        self.assertFalse(enriched["low_quota"])

    def test_quota_validation(self):
        with self.assertRaisesRegex(ValueError, "quota_limit"):
            subs.normalize(sub_record(quota_limit=-1))
        with self.assertRaisesRegex(ValueError, "quota_unit"):
            subs.normalize(sub_record(quota_unit="bananas"))
        with self.assertRaisesRegex(ValueError, "quota_updated_at"):
            subs.normalize(sub_record(quota_updated_at="not-a-date"))

    def test_renew_resets_used_quota(self):
        record = subs.normalize(sub_record(quota_limit=1000, quota_used=940,
                                          quota_unit="requests"))
        renewed = subs.renew_record(record, on=date(2026, 11, 7))
        self.assertEqual(renewed["quota_used"], 0)
        self.assertEqual(renewed["quota_limit"], 1000)
        self.assertEqual(renewed["quota_updated_at"], "2026-11-07")

    def test_file_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subs.json"
            record = subs.normalize(sub_record(quota_limit=500, quota_used=100,
                                              quota_unit="tokens"))
            subs.save_file(path, [record])
            loaded = subs.load_file(path)
            self.assertEqual(loaded[0]["quota_limit"], 500)
            self.assertEqual(loaded[0]["quota_unit"], "tokens")


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            subs.normalize(sub_record(id="sub-a", price=199, channels=["plan-a"],
                                      quota_limit=1000, quota_used=900,
                                      quota_unit="requests")),
            subs.normalize(sub_record(id="sub-b", account="账号 B", plan="agent plan",
                                      price=299, channels=["free"])),
        ]
        self.days = {
            "2026-11-03": {"plan-a": {"requests": 10, "success": 9, "failed": 1, "tokens": 500},
                           "free": {"requests": 4, "success": 4, "failed": 0, "tokens": 80}},
            "2026-11-07": {"plan-a": {"requests": 6, "success": 6, "failed": 0, "tokens": 300}},
            "2026-10-31": {"plan-a": {"requests": 99, "success": 99, "failed": 0, "tokens": 9999}},
        }

    def test_month_filter_and_cost_attribution(self):
        report = build_report(self.records, self.days, "2026-11", date(2026, 11, 7))
        self.assertEqual(report["month"], "2026-11")
        self.assertEqual(report["active_days"], 2)
        self.assertEqual(report["total"]["requests"], 20)
        self.assertEqual(report["total"]["tokens"], 880)
        self.assertEqual(report["currency_totals"]["CNY"], 498.0)
        first = report["subscriptions"][0]
        self.assertEqual(first["usage"]["requests"], 16)
        self.assertEqual(first["usage"]["tokens"], 800)
        self.assertAlmostEqual(first["cost_per_1k_requests"], 199 / 16 * 1000, places=1)
        self.assertEqual(first["quota_remaining"], 100)
        channels = {c["name"]: c for c in report["channels"]}
        self.assertTrue(channels["plan-a"]["linked"])
        self.assertTrue(channels["free"]["linked"])
        self.assertEqual(len(report["vendors"]), 2)

    def test_unlinked_channel_and_invalid_month(self):
        report = build_report([self.records[0]], self.days, "2026-11", date(2026, 11, 7))
        free = next(c for c in report["channels"] if c["name"] == "free")
        self.assertFalse(free["linked"])
        with self.assertRaisesRegex(ValueError, "YYYY-MM"):
            build_report([], {}, "2026-13", date(2026, 11, 7))
        with self.assertRaisesRegex(ValueError, "YYYY-MM"):
            build_report([], {}, "bad", date(2026, 11, 7))


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.seen.append(self.path)  # type: ignore[attr-defined]
        data = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"total_tokens": 7},
        }).encode()
        self.send_response(200)
        mode = self.server.quota_mode  # type: ignore[attr-defined]
        if mode == "exhausted":
            self.send_header("x-ratelimit-remaining-requests", "0")
            self.send_header("x-ratelimit-limit-requests", "60")
            self.send_header("x-ratelimit-reset-requests", "3600")
        elif mode == "healthy":
            self.send_header("x-ratelimit-remaining-requests", "42")
            self.send_header("x-ratelimit-limit-requests", "60")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def start_upstream():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    httpd.seen = []  # type: ignore[attr-defined]
    httpd.quota_mode = "healthy"  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upstream = start_upstream()
        url = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        self.config_path = self.root / "router.json"
        self.config_path.write_text(json.dumps({
            "listen": {"host": "127.0.0.1", "port": 0},
            "channels": [
                {"name": "plan-a", "protocol": "openai", "base_url": url,
                 "models": {"auto": "wire-a"}, "priority": 10},
                {"name": "plan-b", "protocol": "openai", "base_url": url,
                 "models": {"auto": "wire-b"}, "priority": 20},
                {"name": "claude", "protocol": "anthropic", "base_url": url,
                 "models": {"auto": "wire-claude"}},
            ],
            "routes": [
                {"name": "default", "protocol": "openai", "channels": ["plan-a", "plan-b"]},
                {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
            ],
        }), encoding="utf-8")
        (self.root / "subscriptions.json").write_text(json.dumps({
            "subscriptions": [
                sub_record(id="sub-a", channels=["plan-a"], price=199,
                           quota_limit=1000, quota_used=940, quota_unit="requests"),
                sub_record(id="sub-b", account="账号 B", plan="agent plan",
                           channels=["free"], price=299),
            ],
        }), encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(  # type: ignore[attr-defined]
            load_config(self.config_path), config_path=self.config_path,
            subscriptions_file=self.root / "subscriptions.json",
            usage_file=self.root / "usage.json",
            audit_file=self.root / "requests.jsonl",
        )
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.upstream.seen.clear()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def completion(self):
        return self.request("POST", "/v1/chat/completions",
                            {"model": "facet-auto", "messages": []})

    def channel_state(self, name):
        payload = json.loads(self.request("GET", "/api/state")[2])
        return next(c for c in payload["channels"] if c["name"] == name)

    def test_exhausted_quota_preempts_next_request(self):
        self.upstream.quota_mode = "exhausted"
        status, hdrs, _ = self.completion()
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["X-Routed-Via"], "plan-a/wire-a")
        status, hdrs, _ = self.completion()
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["X-Routed-Via"], "plan-b/wire-b")
        state = self.channel_state("plan-a")
        self.assertTrue(state["quota"]["known"])
        self.assertTrue(state["quota"]["exhausted"])
        self.assertIn("quota-preempted", state["reason"])
        logs = self.httpd.app.audit.recent()  # type: ignore[union-attr]
        self.assertTrue(any(e.get("status") == "cooldown" and e.get("channel") == "plan-a"
                            for e in logs))

    def test_healthy_quota_is_recorded_without_cooling(self):
        self.upstream.quota_mode = "healthy"
        self.completion()
        _, hdrs, _ = self.completion()
        self.assertEqual(hdrs["X-Routed-Via"], "plan-a/wire-a")
        state = self.channel_state("plan-a")
        self.assertAlmostEqual(state["quota"]["ratio"], 0.7)
        self.assertFalse(state["cooling"])

    def test_preempt_can_be_disabled(self):
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["defaults"] = {"preempt_quota": False}
        self.httpd.app.apply_config(raw, persist=False)  # type: ignore[union-attr]
        self.upstream.quota_mode = "exhausted"
        self.completion()
        _, hdrs, _ = self.completion()
        self.assertEqual(hdrs["X-Routed-Via"], "plan-a/wire-a")
        self.assertTrue(self.channel_state("plan-a")["quota"]["exhausted"])

    def test_manual_reset_clears_quota_observation(self):
        self.upstream.quota_mode = "exhausted"
        self.completion()
        status, _, _ = self.request("POST", "/api/channels/plan-a/reset")
        self.assertEqual(status, 200)
        self.assertFalse(self.channel_state("plan-a")["quota"]["known"])

    def test_report_and_subscription_endpoints(self):
        app = self.httpd.app  # type: ignore[union-attr]
        app.record_usage("plan-a", True, 100)
        month = date.today().strftime("%Y-%m")
        status, _, raw = self.request("GET", "/api/report")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["month"], month)
        self.assertEqual(payload["total"]["requests"], 1)
        self.assertEqual(payload["currency_totals"]["CNY"], 498.0)
        self.assertEqual(len(payload["subscriptions"]), 2)
        status, _, raw = self.request("GET", f"/api/report/{month}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["month"], month)
        status, _, _ = self.request("GET", "/api/report/not-a-month")
        self.assertEqual(status, 400)
        status, _, raw = self.request("GET", "/api/subscriptions")
        self.assertEqual(status, 200)
        subs_payload = json.loads(raw)
        self.assertEqual(subs_payload["subscriptions"][0]["quota_remaining"], 60)
        self.assertEqual(len(subs_payload["summary"]["quota_alerts"]), 1)

    def test_console_exposes_quota_and_report(self):
        from planrouter.ui import STATIC_DIR
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="report-body"', html)
        self.assertIn('id="report-month"', html)
        self.assertIn("额度余量", html)
        self.assertIn("loadReport", js)
        self.assertIn("quota-low", js)


if __name__ == "__main__":
    unittest.main()
