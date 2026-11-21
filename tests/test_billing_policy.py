"""免费/付费策略预设（第一阶段项 2）。

覆盖：
1. core：defaults.paid_budget_tokens 解析；
2. audit：billing 字段进白名单并落盘；
3. server：_billing_label（free/plan/payg）、_apply_paid_budget_filter、
   _paid_month_tokens、_note_billing（免费→付费切换检测）、runtime_alerts
   的预算与切换告警；
4. 前端/接线静态断言。
"""
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from facet.gateway.audit import RequestLog
from facet.gateway.core import parse_config
from facet.gateway.server import App

SRC = Path(__file__).resolve().parents[1] / "src" / "facet" / "gateway"


def _raw(**defaults):
    raw = {
        "listen": {"host": "127.0.0.1", "port": 8787},
        "channels": [
            {"name": "free-ch", "protocol": "openai", "tier": "free",
             "base_url": "http://127.0.0.1:9", "models": {"auto": "m1"}},
            {"name": "paid-ch", "protocol": "openai",
             "base_url": "http://127.0.0.1:9", "models": {"auto": "m2"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai",
             "channels": ["free-ch", "paid-ch"]},
            {"name": "default", "protocol": "anthropic",
             "channels": ["free-ch"]},
        ],
    }
    if defaults:
        raw["defaults"] = defaults
    return raw


class _Tmp:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup = None

    def path(self, name):
        return Path(self.dir.name) / name


class DefaultsBudgetTests(unittest.TestCase):
    def test_paid_budget_tokens_defaults_to_zero(self):
        config = parse_config(_raw())
        self.assertEqual(config.defaults.paid_budget_tokens, 0)

    def test_paid_budget_tokens_parsed(self):
        config = parse_config(_raw(paid_budget_tokens=5000))
        self.assertEqual(config.defaults.paid_budget_tokens, 5000)


class AuditBillingTests(unittest.TestCase):
    def test_billing_field_persisted(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        log = RequestLog(Path(td.name) / "requests.jsonl")
        log.record(status="ok", channel="a", billing="free")
        entry = log.recent(1)[0]
        self.assertEqual(entry["billing"], "free")


class BillingLabelTests(unittest.TestCase):
    def _app(self, raw=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return App(parse_config(raw or _raw()),
                   usage_file=Path(td.name) / "usage.json",
                   subscriptions_file=Path(td.name) / "subs.json",
                   audit_file=Path(td.name) / "requests.jsonl")

    def test_free_tier_channel(self):
        app = self._app()
        channel = app.config.channel("free-ch")
        self.assertEqual(app._billing_label(channel), "free")

    def test_linked_subscription_is_plan(self):
        app = self._app()
        app.subscriptions = [{"channels": ["paid-ch"], "vendor": "v",
                              "account": "a", "plan": "p"}]
        channel = app.config.channel("paid-ch")
        self.assertEqual(app._billing_label(channel), "plan")

    def test_plain_paid_channel_is_payg(self):
        app = self._app()
        channel = app.config.channel("paid-ch")
        self.assertEqual(app._billing_label(channel), "payg")


class PaidBudgetTests(unittest.TestCase):
    def _app(self, budget=1000, seed_usage=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        raw = _raw(paid_budget_tokens=budget)
        app = App(parse_config(raw),
                  usage_file=Path(td.name) / "usage.json",
                  subscriptions_file=Path(td.name) / "subs.json",
                  audit_file=Path(td.name) / "requests.jsonl")
        if seed_usage:
            for day, channel, tokens in seed_usage:
                bucket = app.usage.data["days"].setdefault(day, {})
                bucket.setdefault(channel, {})["tokens"] = tokens
        return app

    def _candidates(self, app):
        return [(app.config.channel("paid-ch"), "m2"),
                (app.config.channel("free-ch"), "m1")]

    def test_month_tokens_counts_paid_only_current_month(self):
        today = date.today().isoformat()
        last_month = (date.today() - timedelta(days=40)).isoformat()
        app = self._app(seed_usage=[
            (today, "paid-ch", 500),
            (today, "free-ch", 900),
            (last_month, "paid-ch", 7000),
        ])
        self.assertEqual(app._paid_month_tokens(), 500)

    def test_budget_zero_is_noop(self):
        app = self._app(budget=0)
        kept, skipped = app._apply_paid_budget_filter(self._candidates(app))
        self.assertEqual(len(kept), 2)
        self.assertEqual(skipped, [])

    def test_budget_exhausted_drops_paid_keeps_free(self):
        today = date.today().isoformat()
        app = self._app(budget=1000,
                        seed_usage=[(today, "paid-ch", 1500)])
        kept, skipped = app._apply_paid_budget_filter(self._candidates(app))
        self.assertEqual([c.name for c, _ in kept], ["free-ch"])
        self.assertEqual([s["channel"] for s in skipped], ["paid-ch"])
        self.assertEqual(skipped[0]["reason"], "paid_budget_exhausted")

    def test_budget_exhausted_alert(self):
        today = date.today().isoformat()
        app = self._app(budget=1000,
                        seed_usage=[(today, "paid-ch", 1500)])
        alerts = [a for a in app.runtime_alerts()
                  if a["kind"] == "paid_budget_exhausted"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("paid-ch", alerts[0]["message"])

    def test_budget_ok_no_alert(self):
        today = date.today().isoformat()
        app = self._app(budget=100000,
                        seed_usage=[(today, "paid-ch", 500)])
        alerts = [a for a in app.runtime_alerts()
                  if a["kind"] == "paid_budget_exhausted"]
        self.assertEqual(alerts, [])


class BillingSwitchTests(unittest.TestCase):
    def _app(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return App(parse_config(_raw()),
                   usage_file=Path(td.name) / "usage.json",
                   subscriptions_file=Path(td.name) / "subs.json",
                   audit_file=Path(td.name) / "requests.jsonl")

    def test_note_billing_free_then_paid_records_switch(self):
        import time
        app = self._app()
        app._note_billing("free")
        self.assertEqual(app._billing_state["prev"], None)
        app._note_billing("payg")
        self.assertEqual(app._billing_state["prev"], "free")
        self.assertGreater(app._billing_state["at"], 0)

    def test_switch_alert_active_window_only(self):
        import time
        app = self._app()
        app._billing_state = {"prev": "free", "at": time.time()}
        alerts = [a for a in app.runtime_alerts()
                  if a["kind"] == "billing_switch"]
        self.assertEqual(len(alerts), 1)
        app._billing_state = {"prev": "free", "at": time.time() - 3600}
        alerts = [a for a in app.runtime_alerts()
                  if a["kind"] == "billing_switch"]
        self.assertEqual(alerts, [])

    def test_no_switch_no_alert(self):
        app = self._app()
        alerts = [a for a in app.runtime_alerts()
                  if a["kind"] == "billing_switch"]
        self.assertEqual(alerts, [])


class PolicyWiringTests(unittest.TestCase):
    def test_server_applies_budget_filter_and_billing_labels(self):
        src = (SRC / "server.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count("_apply_paid_budget_filter"), 2)
        self.assertGreaterEqual(src.count('"billing": self._billing_label'), 3)
        self.assertIn("def _note_billing", src)
        self.assertIn("paid_budget_exhausted", src)
        self.assertIn("billing_switch", src)

    def test_audit_whitelist_has_billing(self):
        src = (SRC / "audit.py").read_text(encoding="utf-8")
        self.assertIn('"billing"', src)

    def test_console_has_preset_ui(self):
        html = (SRC / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("route-presets", html)
        self.assertIn("paid-budget", html)
        js = (SRC / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("data-preset", js)
        self.assertIn("BILLING_LABELS", js)
        self.assertIn("route-preview", js)


if __name__ == "__main__":
    unittest.main()
