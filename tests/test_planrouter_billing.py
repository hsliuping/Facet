"""Billing-aware ordering for explicit model requests.

A request that names a model (deepseek-flash, glm-4.6, ...) must drain quota
that would otherwise expire before spending cash: monthly plans first (soonest
subscription expiry first; among plans lapsing the same day, the one with more
remaining quota per remaining day goes first), then free-tier channels, then
pay-as-you-go. Internal aliases (auto/free) keep the console chain order.
"""
from __future__ import annotations

import unittest

from facet.gateway.core import parse_config
from facet.gateway.server import App


def billing_config() -> dict:
    key = "sk-fixture"
    models = {"deepseek-flash": "ds-flash", "auto": "auto"}
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "payg-a", "base_url": "https://a.example/v1",
             "api_key": key, "models": dict(models)},
            {"name": "plan-late", "base_url": "https://b.example/v1",
             "api_key": key, "business_line": "coding",
             "models": dict(models)},
            {"name": "plan-soon", "base_url": "https://c.example/v1",
             "api_key": key, "business_line": "agent",
             "models": dict(models)},
            {"name": "plan-undated", "base_url": "https://d.example/v1",
             "api_key": key, "business_line": "agent",
             "models": dict(models)},
            {"name": "free-c", "base_url": "https://e.example/v1",
             "api_key": key, "tier": "free", "models": dict(models)},
            {"name": "anthro-a", "protocol": "anthropic",
             "base_url": "https://f.example/v1", "api_key": key,
             "models": {"claude-x": "claude-x"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai",
             "channels": ["payg-a", "plan-late", "plan-soon",
                          "plan-undated", "free-c"]},
            {"name": "default", "protocol": "anthropic",
             "channels": ["anthro-a"]},
        ],
    }


def subscription(plan: str, expires: str, channels: list[str]) -> dict:
    # Field set mirrors what the subscriptions page stores; price/currency
    # stay None because summarize() reads them for its dashboard alerts.
    return {"id": plan, "plan": plan, "tier": "paid", "price": None,
            "currency": None, "account": "", "expires_at": expires,
            "channels": channels}


def app_with(records: list[dict]) -> App:
    app = App(parse_config(billing_config()))
    app.subscriptions = records
    app._refresh_billing_ranks()
    return app


def picked(app: App, model: str) -> list[str]:
    pairs, _route = app.store.pick("openai", "", model, None,
                                   now=1_800_000_000.0)
    return [channel.name for channel, _wire in pairs]


class BillingOrderTests(unittest.TestCase):
    def test_exact_request_drains_soonest_plan_first(self):
        app = app_with([
            subscription("soon", "2026-11-20", ["plan-soon"]),
            subscription("late", "2027-03-01", ["plan-late"]),
        ])
        self.assertEqual(picked(app, "deepseek-flash"),
                         ["plan-soon", "plan-late", "plan-undated",
                          "free-c", "payg-a"])

    def test_expired_subscription_leaves_plan_undated(self):
        app = app_with([
            subscription("soon", "2026-11-20", ["plan-soon"]),
            subscription("old", "2026-01-01", ["plan-late"]),
        ])
        self.assertEqual(app.store.billing_ranks["plan-late"], (1, 0.0, 0.0))
        self.assertEqual(picked(app, "deepseek-flash"),
                         ["plan-soon", "plan-late", "plan-undated",
                          "free-c", "payg-a"])

    def test_linked_subscription_marks_channel_monthly(self):
        app = app_with([subscription("p", "2027-01-01", ["payg-a"])])
        self.assertEqual(app.store.billing_ranks["payg-a"][0], 0)
        self.assertEqual(picked(app, "deepseek-flash")[0], "payg-a")

    def test_auto_alias_keeps_chain_order(self):
        app = app_with([
            subscription("soon", "2026-11-20", ["plan-soon"]),
            subscription("late", "2027-03-01", ["plan-late"]),
        ])
        self.assertEqual(picked(app, "auto"),
                         ["payg-a", "plan-late", "plan-soon",
                          "plan-undated", "free-c"])

    def test_free_tier_beats_payg_but_not_plans(self):
        ranks = app_with([]).store.billing_ranks
        self.assertEqual(ranks["free-c"], (2, 0.0, 0.0))
        self.assertEqual(ranks["payg-a"], (3, 0.0, 0.0))
        self.assertEqual(picked(app_with([]), "deepseek-flash"),
                         ["plan-late", "plan-soon", "plan-undated",
                          "free-c", "payg-a"])

    def test_same_expiry_fuller_plan_drains_first(self):
        soon = subscription("soon", "2026-11-20", ["plan-soon"])
        tied = subscription("tied", "2026-11-20", ["plan-late"])
        # Both plans lapse on the same day, but plan-late still holds 90% of
        # its quota while plan-soon holds 10%: the fuller plan is the one at
        # risk of going unused, so it drains first within the shared day.
        soon["quota_limit"], soon["quota_used"] = 100, 90
        tied["quota_limit"], tied["quota_used"] = 100, 10
        app = app_with([soon, tied])
        self.assertLess(app.store.billing_ranks["plan-late"],
                        app.store.billing_ranks["plan-soon"])
        self.assertEqual(picked(app, "deepseek-flash")[:2],
                         ["plan-late", "plan-soon"])

    def test_ui_state_exposes_billing(self):
        app = app_with([subscription("soon", "2026-11-20", ["plan-soon"])])
        items = {c["name"]: c for c in app.ui_state()["channels"]}
        self.assertEqual(items["plan-soon"]["billing"]["group"], 0)
        self.assertEqual(items["plan-soon"]["billing"]["expiry"], "2026-11-20")
        self.assertEqual(items["payg-a"]["billing"]["group"], 3)
        self.assertEqual(items["payg-a"]["billing"]["expiry"], "")


if __name__ == "__main__":
    unittest.main()
