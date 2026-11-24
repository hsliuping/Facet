"""Billing-aware ordering for explicit model requests.

A request that names a model (deepseek-flash, glm-4.6, ...) must drain quota
that would otherwise expire before spending cash: monthly plans first (soonest
subscription expiry first; among plans lapsing the same day, the one with more
remaining quota per remaining day goes first), then free-tier channels, then
pay-as-you-go. Internal aliases (auto/free) keep the console chain order.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from facet.gateway.core import parse_config
from facet.gateway.server import App

# Expiry fixtures are relative to today: hardcoded calendar dates rot as the
# clock passes them (2026-11-20 expired one day after the suite last ran
# green and took the whole billing class red under every runner).
SOON_ISO = (date.today() + timedelta(days=3)).isoformat()
LATE_ISO = (date.today() + timedelta(days=130)).isoformat()
OLD_ISO = (date.today() - timedelta(days=90)).isoformat()
PAYG_ISO = (date.today() + timedelta(days=40)).isoformat()


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
            subscription("soon", SOON_ISO, ["plan-soon"]),
            subscription("late", LATE_ISO, ["plan-late"]),
        ])
        self.assertEqual(picked(app, "deepseek-flash"),
                         ["plan-soon", "plan-late", "plan-undated",
                          "free-c", "payg-a"])

    def test_expired_subscription_leaves_plan_undated(self):
        app = app_with([
            subscription("soon", SOON_ISO, ["plan-soon"]),
            subscription("old", OLD_ISO, ["plan-late"]),
        ])
        self.assertEqual(app.store.billing_ranks["plan-late"], (1, 0.0, 0.0))
        self.assertEqual(picked(app, "deepseek-flash"),
                         ["plan-soon", "plan-late", "plan-undated",
                          "free-c", "payg-a"])

    def test_linked_subscription_marks_channel_monthly(self):
        app = app_with([subscription("p", PAYG_ISO, ["payg-a"])])
        self.assertEqual(app.store.billing_ranks["payg-a"][0], 0)
        self.assertEqual(picked(app, "deepseek-flash")[0], "payg-a")

    def test_auto_alias_keeps_chain_order(self):
        app = app_with([
            subscription("soon", SOON_ISO, ["plan-soon"]),
            subscription("late", LATE_ISO, ["plan-late"]),
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
        soon = subscription("soon", SOON_ISO, ["plan-soon"])
        tied = subscription("tied", SOON_ISO, ["plan-late"])
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

    def test_openrouter_wire_splits_free_and_payg(self):
        # 聚合渠道（OpenRouter）整渠 tier=free，但只有 ":free" 后缀的 wire
        # 属于免费组；收费款 wire 落到按量组——渠道级排序下它会抢在
        # 真正的免费渠道前面。渠道级键保留（控制台徽标语义不变）。
        raw = billing_config()
        raw["channels"] = [
            {"name": "agg", "base_url": "https://or.example/v1",
             "api_key": "sk", "tier": "free", "provider": "openrouter",
             "models": {"auto": "a",
                        "deepseek/deepseek-chat-v3:free": "deepseek/deepseek-chat-v3:free",
                        "aion-labs/aion-2.0": "aion-labs/aion-2.0"}},
            {"name": "freehost", "base_url": "https://fh.example/v1",
             "api_key": "sk", "tier": "free",
             "models": {"auto": "a",
                        "deepseek/deepseek-chat-v3:free": "deepseek/deepseek-chat-v3:free",
                        "aion-labs/aion-2.0": "aion-labs/aion-2.0"}},
            {"name": "anthro-a", "protocol": "anthropic",
             "base_url": "https://f.example/v1", "api_key": "sk",
             "models": {"claude-x": "claude-x"}},
        ]
        raw["routes"][0]["channels"] = ["agg", "freehost"]
        app = App(parse_config(raw))
        app._refresh_billing_ranks()
        ranks = app.store.billing_ranks
        self.assertEqual(ranks["agg"], (2, 0.0, 0.0))
        self.assertEqual(ranks["agg|deepseek/deepseek-chat-v3:free"], (2, 0.0, 0.0))
        self.assertEqual(ranks["agg|aion-labs/aion-2.0"], (3, 0.0, 0.0))
        self.assertEqual(picked(app, "aion-labs/aion-2.0"), ["freehost", "agg"])
        self.assertEqual(picked(app, "deepseek/deepseek-chat-v3:free"),
                         ["agg", "freehost"])

    def test_manual_tier_flips_payg_wire_into_free_group(self):
        # 手工 tiers 同样驱动 per-wire 键：按量渠道的某个模型被标成 free，
        # 排序就按该模型进免费组，渠道级键仍是按量组。（端到端顺序效果由
        # 上一条聚合渠道用例验证；这里 payg-a 字母序恒在 free-c 之后，
        # 顺序断言没有区分度。）
        raw = billing_config()
        raw["channels"][0]["tiers"] = {"ds-flash": "free"}
        app = App(parse_config(raw))
        app._refresh_billing_ranks()
        self.assertEqual(app.store.billing_ranks["payg-a"], (3, 0.0, 0.0))
        self.assertEqual(app.store.billing_ranks["payg-a|ds-flash"], (2, 0.0, 0.0))

    def test_ui_state_exposes_billing(self):
        app = app_with([subscription("soon", SOON_ISO, ["plan-soon"])])
        items = {c["name"]: c for c in app.ui_state()["channels"]}
        self.assertEqual(items["plan-soon"]["billing"]["group"], 0)
        self.assertEqual(items["plan-soon"]["billing"]["expiry"], SOON_ISO)
        self.assertEqual(items["payg-a"]["billing"]["group"], 3)
        self.assertEqual(items["payg-a"]["billing"]["expiry"], "")


if __name__ == "__main__":
    unittest.main()
