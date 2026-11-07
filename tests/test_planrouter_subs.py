"""Vendor plan subscriptions: validation, renewal math, reminders and API."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from planrouter import cli
from planrouter import subscriptions as subs
from planrouter.core import load_config
from planrouter.server import App, Handler

TODAY = date(2026, 11, 7)


def volcengine_records() -> list[dict]:
    return subs.normalize_all([
        {
            "vendor": "火山引擎", "account": "账号 A", "plan": "coding plan", "tier": "Pro",
            "price": 199, "currency": "CNY", "billing_cycle": "monthly",
            "started_at": "2026-10-07", "expires_at": "2026-11-10",
            "remind_days": 7, "channels": ["volcengine-a"],
        },
        {
            "vendor": "火山引擎", "account": "账号 B", "plan": "coding plan", "tier": "Pro",
            "price": 199, "currency": "CNY", "billing_cycle": "monthly",
            "started_at": "2026-10-15", "expires_at": "2026-11-15",
        },
        {
            "vendor": "火山引擎", "account": "账号 A", "plan": "agent plan", "tier": "标准版",
            "price": 299, "currency": "CNY", "billing_cycle": "monthly",
            "started_at": "2026-11-01", "expires_at": "2026-12-01",
            "auto_renew": True,
        },
    ])


def api_records(on: date | None = None) -> list[dict]:
    """Same vendor/account/plan shape, with dates relative to the running day."""
    on = on or date.today()
    records = volcengine_records()
    for record, days_left in zip(records, (3, 8, 24)):
        record["expires_at"] = (on + timedelta(days=days_left)).isoformat()
        record["started_at"] = (on - timedelta(days=30)).isoformat()
    return subs.normalize_all(records)


def config_dict() -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "a", "protocol": "openai", "base_url": "http://127.0.0.1:9101",
             "models": {"auto": "model-a"}},
            {"name": "b", "protocol": "anthropic", "base_url": "http://127.0.0.1:9102",
             "models": {"auto": "model-b"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["a"]},
            {"name": "default", "protocol": "anthropic", "channels": ["b"]},
        ],
    }


class SubscriptionMathTests(unittest.TestCase):
    def test_month_end_and_leap_year(self):
        self.assertEqual(subs.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(subs.add_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(subs.add_months(date(2026, 11, 30), 3), date(2027, 2, 28))
        self.assertEqual(subs.add_months(date(2026, 11, 7), 12), date(2027, 11, 7))

    def test_multi_account_multi_plan_summary(self):
        records = volcengine_records()
        summary = subs.summarize(records, on=TODAY)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["counts"], {"active": 2, "expiring": 1, "expired": 0, "unknown": 0})
        self.assertEqual(summary["monthly_cost"], {"CNY": 697.0})
        self.assertEqual(len(summary["alerts"]), 1)
        self.assertEqual(summary["alerts"][0]["account"], "账号 A")
        self.assertEqual(summary["alerts"][0]["days_left"], 3)

    def test_expired_and_unknown_status(self):
        records = subs.normalize_all([
            {"vendor": "v", "plan": "p", "price": 10, "expires_at": "2026-11-01"},
            {"vendor": "v", "plan": "p2", "price": 10},
            {"vendor": "v", "plan": "p3", "price": 10, "expires_at": "2026-12-31", "remind_days": 0},
        ])
        states = [subs.enrich(r, TODAY)["status"] for r in records]
        self.assertEqual(states, ["expired", "unknown", "active"])

    def test_yearly_and_quarterly_monthly_equivalent(self):
        yearly = subs.normalize({"vendor": "v", "plan": "p", "price": 1200, "billing_cycle": "yearly"})
        quarterly = subs.normalize({"vendor": "v", "plan": "p", "price": 300, "billing_cycle": "quarterly"})
        self.assertEqual(subs.monthly_equivalent(yearly)["amount"], 100.0)
        self.assertEqual(subs.monthly_equivalent(quarterly)["amount"], 100.0)

    def test_renew_extends_by_cycle_and_resets_start(self):
        record = volcengine_records()[0]
        renewed = subs.renew_record(record, {}, on=TODAY)
        self.assertEqual(renewed["expires_at"], "2026-12-10")
        self.assertEqual(renewed["started_at"], "2026-11-07")
        self.assertEqual(renewed["id"], record["id"])
        self.assertEqual(renewed["price"], 199)

    def test_renew_can_change_price_and_tier(self):
        record = volcengine_records()[2]
        renewed = subs.renew_record(
            record, {"price": 399, "tier": "高级版", "expires_at": "2027-01-01"}, on=TODAY,
        )
        self.assertEqual(renewed["price"], 399)
        self.assertEqual(renewed["tier"], "高级版")
        self.assertEqual(renewed["expires_at"], "2027-01-01")

    def test_validation_rejects_bad_records(self):
        bad = [
            {"plan": "p"},
            {"vendor": "v", "plan": "p", "started_at": "2026/11/07"},
            {"vendor": "v", "plan": "p", "price": -1},
            {"vendor": "v", "plan": "p", "currency": "BTC"},
            {"vendor": "v", "plan": "p", "started_at": "2026-11-07", "expires_at": "2026-11-01"},
        ]
        for record in bad:
            with self.assertRaises(ValueError):
                subs.normalize(record)
        duplicated = [
            {"id": "same", "vendor": "v", "plan": "p"},
            {"id": "same", "vendor": "v", "plan": "p2"},
        ]
        with self.assertRaisesRegex(ValueError, "id 重复"):
            subs.normalize_all(duplicated)

    def test_channels_accept_comma_separated_string(self):
        record = subs.normalize({"vendor": "v", "plan": "p", "channels": "ch-a, ch-b"})
        self.assertEqual(record["channels"], ["ch-a", "ch-b"])

    def test_models_url_and_models_normalization(self):
        record = subs.normalize({
            "vendor": "v", "plan": "p",
            "models_url": " https://example.com/docs ",
            "models": "Doubao-Seed-2.1-Pro, glm-5.3\n\ndoubao-seed-evolving\n",
        })
        self.assertEqual(record["models_url"], "https://example.com/docs")
        self.assertEqual(record["models"],
                         ["Doubao-Seed-2.1-Pro", "glm-5.3", "doubao-seed-evolving"])

    def test_models_accept_list_and_default_to_empty(self):
        record = subs.normalize({"vendor": "v", "plan": "p", "models": ["a-b", " c.d ", ""]})
        self.assertEqual(record["models"], ["a-b", "c.d"])
        plain = subs.normalize({"vendor": "v", "plan": "p"})
        self.assertEqual((plain["models_url"], plain["models"]), ("", []))

    def test_models_url_must_be_http(self):
        for bad in ("ftp://example.com/x", "example.com/docs"):
            with self.assertRaisesRegex(ValueError, "models_url 必须是 http"):
                subs.normalize({"vendor": "v", "plan": "p", "models_url": bad})

    def test_enrich_keeps_plan_model_fields(self):
        record = subs.normalize({
            "vendor": "v", "plan": "p",
            "models_url": "https://e.com/d", "models": ["a-b"],
        })
        enriched = subs.enrich(record, TODAY)
        self.assertEqual(enriched["models_url"], "https://e.com/d")
        self.assertEqual(enriched["models"], ["a-b"])

    def test_file_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subscriptions.json"
            subs.save_file(path, volcengine_records())
            self.assertEqual(len(subs.load_file(path)), 3)
            self.assertEqual(subs.load_file(Path(tmp) / "missing.json"), [])


class SubscriptionApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "router.json"
        self.subs_path = self.root / "subscriptions.json"
        self.config_path.write_text(json.dumps(config_dict()), encoding="utf-8")
        subs.save_file(self.subs_path, api_records())
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(  # type: ignore[attr-defined]
            load_config(self.config_path),
            config_path=self.config_path,
            subscriptions_file=self.subs_path,
        )
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_page_has_subscription_tab(self):
        req = Request(self.base + "/", method="GET")
        with urlopen(req, timeout=5) as resp:
            html = resp.read().decode()
        self.assertIn("套餐订阅", html)
        self.assertIn("panel-subscriptions", html)

    def test_list_summary_and_health_alerts(self):
        status, payload = self.call("GET", "/api/subscriptions")
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["counts"]["expiring"], 1)
        self.assertEqual(len(payload["subscriptions"]), 3)
        status, health = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(len(health["subscriptions"]["alerts"]), 1)

    def test_save_rejects_bad_date_without_writing(self):
        before = self.subs_path.read_bytes()
        status, payload = self.call("POST", "/api/subscriptions", {
            "subscriptions": [{"vendor": "v", "plan": "p", "expires_at": "2026/12/01"}],
        })
        self.assertEqual(status, 400)
        self.assertIn("YYYY-MM-DD", payload["error"]["message"])
        self.assertEqual(self.subs_path.read_bytes(), before)

    def test_save_and_renew_clear_the_reminder(self):
        status, payload = self.call("GET", "/api/subscriptions")
        records = payload["subscriptions"]
        expiring = next(r for r in records if r["status"] == "expiring")
        subscription_id = expiring["id"]

        status, payload = self.call("POST", "/api/subscriptions", {
            "subscriptions": records + [{
                "vendor": "火山引擎", "account": "账号 C", "plan": "agent plan",
                "price": 99, "expires_at": (date.today() + timedelta(days=43)).isoformat(),
            }],
        })
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["summary"]["total"], 4)
        self.assertEqual(len(subs.load_file(self.subs_path)), 4)

        status, payload = self.call("POST", f"/api/subscriptions/{subscription_id}/renew", {
            "price": 219, "tier": "Pro+",
        })
        self.assertEqual(status, 200)
        expected_expiry = subs.add_months(date.today() + timedelta(days=3), 1).isoformat()
        self.assertEqual(payload["subscription"]["expires_at"], expected_expiry)
        self.assertEqual(payload["subscription"]["price"], 219)
        self.assertEqual(payload["subscription"]["status"], "active")

        status, payload = self.call("GET", "/api/subscriptions")
        self.assertEqual(payload["summary"]["counts"]["expiring"], 0)

    def test_cli_check_prints_renewal_reminder(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--config", str(self.config_path), "--check"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("RENEW", text)
        self.assertIn("coding plan", text)
        self.assertIn("697", text)


if __name__ == "__main__":
    unittest.main()
