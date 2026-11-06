"""Expired-plan channel blocking, usage trends and renewal notifications."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from planrouter import subscriptions as subs
from planrouter.core import load_config
from planrouter.notifier import ReminderService, alert_message
from planrouter.server import App, Handler
from planrouter.usage import UsageRecorder


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        data = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"total_tokens": 21},
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
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def subscription(expires_delta: int, channels: list[str], plan: str = "coding plan") -> dict:
    return {
        "vendor": "火山引擎", "account": "账号 A", "plan": plan,
        "price": 199, "currency": "CNY", "billing_cycle": "monthly",
        "started_at": (date.today() - timedelta(days=30)).isoformat(),
        "expires_at": (date.today() + timedelta(days=expires_delta)).isoformat(),
        "remind_days": 7, "channels": channels,
    }


class OpsFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upstream = start_upstream()
        self.config_path = self.root / "router.json"
        self.subs_path = self.root / "subscriptions.json"
        self.config_path.write_text(json.dumps({
            "listen": {"host": "127.0.0.1", "port": 0},
            "channels": [
                {"name": "plan-a", "protocol": "openai",
                 "base_url": f"http://127.0.0.1:{self.upstream.server_address[1]}",
                 "models": {"auto": "model-a", "exposed-a": "model-a"}},
                {"name": "claude", "protocol": "anthropic",
                 "base_url": f"http://127.0.0.1:{self.upstream.server_address[1]}",
                 "models": {"auto": "model-b"}},
            ],
            "routes": [
                {"name": "default", "protocol": "openai", "channels": ["plan-a"]},
                {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
            ],
        }), encoding="utf-8")
        self.httpd = None

    def write_subscriptions(self, records, settings=None):
        subs.save_file(self.subs_path, subs.normalize_all(records), settings)

    def start(self, records=None, settings=None):
        if records is not None:
            self.write_subscriptions(records, settings)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(  # type: ignore[attr-defined]
            load_config(self.config_path),
            config_path=self.config_path,
            subscriptions_file=self.subs_path,
            usage_file=self.root / "usage.json",
            notifications_file=self.root / "notifications.json",
        )
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        return self.httpd.app  # type: ignore[union-attr]

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, json.loads(resp.read())
        except HTTPError as exc:
            return exc.code, exc.headers, json.loads(exc.read())

    def completion(self):
        return self.call("POST", "/v1/chat/completions", {
            "model": "auto", "messages": [{"role": "user", "content": "hi"}],
        })


class SettingsAndBlockingTests(OpsFixture):
    def test_settings_round_trip_and_validation(self):
        self.write_subscriptions([], {"block_expired_channels": False, "interval_minutes": 15})
        settings = subs.load_settings(self.subs_path)
        self.assertFalse(settings["block_expired_channels"])
        self.assertEqual(settings["interval_minutes"], 15)
        with self.assertRaisesRegex(ValueError, "webhook_url"):
            subs.normalize_settings({"webhook_url": "ftp://bad"})
        with self.assertRaisesRegex(ValueError, "interval_minutes"):
            subs.normalize_settings({"interval_minutes": 5000})

    def test_masked_webhook_is_preserved_on_save(self):
        app = self.start([], {"webhook_url": "https://example.com/hook"})
        app.save_subscription_settings({
            "webhook_url": "***", "notify_enabled": True, "interval_minutes": 30,
        })
        self.assertEqual(app.subscription_settings["webhook_url"], "https://example.com/hook")
        self.assertEqual(subs.load_settings(self.subs_path)["webhook_url"],
                         "https://example.com/hook")

    def test_expired_subscription_blocks_its_channels(self):
        app = self.start([
            subscription(-2, ["plan-a"]),
            subscription(20, ["claude"]),
        ])
        blocked = app.blocked_channels()
        self.assertIn("plan-a", blocked)
        self.assertIn("订阅已过期", blocked["plan-a"])
        self.assertNotIn("claude", blocked)

        status, _, payload = self.completion()
        self.assertEqual(status, 503)
        self.assertIn("plan-a", payload["error"]["blocked_channels"])

        status, _, health = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertNotIn("plan-a", health["available"])
        self.assertIn("plan-a", health["blocked_channels"])
        status, _, models = self.call("GET", "/v1/models")
        self.assertNotIn("exposed-a", [m["id"] for m in models["data"]])

    def test_any_live_subscription_keeps_channel_usable(self):
        self.start([
            subscription(-2, ["plan-a"], plan="old plan"),
            subscription(20, ["plan-a"], plan="renewed plan"),
        ])
        status, _, _ = self.completion()
        self.assertEqual(status, 200)

    def test_switch_disables_blocking(self):
        self.start([subscription(-2, ["plan-a"])], {"block_expired_channels": False})
        status, _, _ = self.completion()
        self.assertEqual(status, 200)

    def test_blocked_flag_reported_in_state(self):
        self.start([subscription(-2, ["plan-a"])])
        _, _, payload = self.call("GET", "/api/state")
        channel = next(c for c in payload["channels"] if c["name"] == "plan-a")
        self.assertTrue(channel["blocked"])
        self.assertIn("订阅已过期", channel["blocked_reason"])


class UsageTests(OpsFixture):
    def test_recorder_series_and_persistence(self):
        path = self.root / "usage.json"
        recorder = UsageRecorder(path)
        today = date.today()
        recorder.record("plan-a", True, 100, on=today)
        recorder.record("plan-a", False, 0, on=today)
        recorder.record("claude", True, 50, on=today - timedelta(days=1))
        series = recorder.series(7, on=today)
        self.assertEqual(len(series["days"]), 7)
        self.assertEqual(series["days"][-1]["requests"], 2)
        self.assertEqual(series["days"][-1]["tokens"], 100)
        self.assertEqual(series["channels"][0]["name"], "plan-a")
        recorder.flush()
        self.assertEqual(UsageRecorder(path).series(7, on=today)["days"][-1]["requests"], 2)

    def test_api_usage_and_request_recording(self):
        app = self.start([])
        status, _, payload = self.call("GET", "/api/usage/7")
        self.assertEqual(status, 200)
        self.assertEqual(payload["range"], 7)
        self.assertEqual(len(payload["days"]), 7)
        self.assertEqual(self.completion()[0], 200)
        self.assertEqual(app.usage.series(7)["days"][-1]["requests"], 1)
        self.assertEqual(app.usage.series(7)["days"][-1]["tokens"], 21)

    def test_page_contains_chart_and_notification_controls(self):
        self.start([])
        with urlopen(self.base + "/", timeout=5) as resp:
            html = resp.read().decode()
        self.assertIn("chart-body", html)
        self.assertIn("btn-test-notify", html)
        self.assertIn("set-block", html)


class NotifierTests(OpsFixture):
    def test_dedup_and_payload(self):
        sent = []

        def sender(request):
            sent.append(json.loads(request.data.decode("utf-8"))["text"]["content"])

        records = subs.normalize_all([subscription(3, [])])
        service = ReminderService(
            self.subs_path, self.root / "notifications.json",
            lambda: records,
            lambda: {"notify_enabled": True, "webhook_url": "https://example.com/hook",
                     "interval_minutes": 60},
            sender=sender,
        )
        first = service.check()
        self.assertEqual(first["sent"], 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("火山引擎", sent[0])
        self.assertIn("coding plan", sent[0])
        self.assertEqual(service.check()["sent"], 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(service.check(force=True)["sent"], 1)

    def test_disabled_does_not_send_and_status_masks_url(self):
        def sender(request):
            raise AssertionError("should not send")

        service = ReminderService(
            self.subs_path, self.root / "notifications.json",
            lambda: subs.normalize_all([subscription(3, [])]),
            lambda: {"notify_enabled": False, "webhook_url": "https://secret.example/hook",
                     "interval_minutes": 60},
            sender=sender,
        )
        self.assertEqual(service.check()["skipped"], "disabled")
        self.assertEqual(service.status()["settings"]["webhook_url"], "***")
        with self.assertRaisesRegex(ValueError, "webhook_url"):
            ReminderService(
                None, None, lambda: [], lambda: {"notify_enabled": True, "webhook_url": ""},
                sender=sender,
            ).send_test()

    def test_alert_message_contains_expiry_and_price(self):
        alert = subs.summarize(subs.normalize_all([subscription(3, [])]))["alerts"][0]
        message = alert_message(alert)
        self.assertIn("199", message)
        self.assertIn(alert["expires_at"], message)

    def test_notifications_api(self):
        self.start([subscription(3, [])])
        status, _, payload = self.call("GET", "/api/notifications")
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        self.assertEqual(len(payload["pending"]), 1)

        status, _, payload = self.call("POST", "/api/notifications", {
            "settings": {
                "block_expired_channels": False, "notify_enabled": False,
                "webhook_url": "env:MY_HOOK", "interval_minutes": 25,
            },
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["settings"]["webhook_url"], "env:MY_HOOK")
        settings = subs.load_settings(self.subs_path)
        self.assertFalse(settings["block_expired_channels"])
        self.assertEqual(settings["interval_minutes"], 25)

        status, _, payload = self.call("POST", "/api/notifications/check")
        self.assertEqual(status, 200)
        self.assertEqual(payload["sent"], 0)

    def test_bad_settings_return_400(self):
        self.start([])
        status, _, payload = self.call("POST", "/api/notifications", {
            "settings": {"webhook_url": "not-a-url"},
        })
        self.assertEqual(status, 400)
        self.assertIn("webhook_url", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main()
