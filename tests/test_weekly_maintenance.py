"""Tests for tools/weekly_maintenance.py — stdlib unittest, run from repo root:

    python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import weekly_maintenance as wm  # noqa: E402


TABLE = {"schema_version": 1, "models": {
    "openai/gpt-big": {"provider": "openai", "context_window": 400000,
                       "tool_call": True, "cost": {"output_per_mtok": 8.0}},
    "openai/gpt-small": {"provider": "openai", "context_window": 32000,
                         "tool_call": True, "cost": {"output_per_mtok": 1.0}},
    "openai/gpt-nocall": {"provider": "openai", "context_window": 200000,
                          "tool_call": False},
    "zhipu/glm-5.3": {"provider": "zhipu", "context_window": 200000,
                      "tool_call": True, "quota_tier": "free_daily",
                      "cost": {"output_per_mtok": 0.0}, "aliases": ["glm5.3"]},
    "vendor/orphan-free": {"provider": "vendor", "quota_tier": "free_forever"},
}}

CONFIG = {"channels": [
    {"name": "free-a", "protocol": "openai", "tier": "free", "enabled": True,
     "models": {"auto": "GLM-5.3"}},                # folded identity match
    {"name": "pay-big", "protocol": "openai", "tier": "paid", "enabled": True,
     "models": {"auto": "gpt-big"}},
    {"name": "pay-small", "protocol": "openai", "tier": "paid", "enabled": True,
     "models": {"auto": "gpt-small"}},
    {"name": "no-tool", "protocol": "openai", "tier": "free", "enabled": True,
     "models": {"auto": "gpt-nocall"}},
    {"name": "ghost", "protocol": "openai", "tier": "free", "enabled": True,
     "models": {"auto": "no-such-model"}},
    {"name": "off", "protocol": "openai", "tier": "free", "enabled": False,
     "models": {"auto": "gpt-big"}},
    {"name": "claudy", "protocol": "anthropic", "tier": "free", "enabled": True,
     "models": {"auto": "glm5.3"}},                 # alias match
], "routes": [
    {"name": "default", "protocol": "openai", "channels": ["pay-big"]},
    {"name": "cheap", "protocol": "openai", "channels": ["pay-small"]},
    {"name": "default", "protocol": "anthropic", "channels": ["claudy"]},
]}


class FlagshipTests(unittest.TestCase):
    def test_auto_mapping_wins(self):
        self.assertEqual(wm.flagship_wire_model({"auto": "m1", "m2": "w2"}), "m1")

    def test_empty_auto_falls_to_first_explicit(self):
        self.assertEqual(wm.flagship_wire_model({"auto": "", "m2": "w2", "m3": "w3"}), "w2")

    def test_star_and_empty_never_count(self):
        self.assertIsNone(wm.flagship_wire_model({"auto": "*", "m2": "*"}))
        self.assertIsNone(wm.flagship_wire_model({}))


class CrosscheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resolve = staticmethod(wm.build_resolver(TABLE))
        cls.report = wm.crosscheck(CONFIG, TABLE, cls.resolve)

    def test_unknown_wire_names_reported(self):
        self.assertEqual(self.report["unknown_channel_models"], {"ghost": ["no-such-model"]})

    def test_folded_identity_resolves_case_and_hyphens(self):
        self.assertEqual(self.resolve("GLM-5.3")[0], "zhipu/glm-5.3")
        self.assertEqual(self.resolve("glm5.3")[0], "zhipu/glm-5.3")  # alias

    def test_flagships_resolved_or_none(self):
        self.assertEqual(self.report["flagships"]["pay-big"]["registry_key"], "openai/gpt-big")
        self.assertIsNone(self.report["flagships"]["ghost"]["registry_key"])

    def test_disabled_channel_not_scanned(self):
        self.assertNotIn("off", self.report["flagships"])

    def test_unexposed_free_models(self):
        self.assertIn("vendor/orphan-free", self.report["unexposed_free_models"])
        # exposed via free-a's auto mapping -> not stale
        self.assertNotIn("zhipu/glm-5.3", self.report["unexposed_free_models"])


class SuggestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resolve = staticmethod(wm.build_resolver(TABLE))
        cls.doc = wm.suggest_routes(CONFIG, TABLE, cls.resolve)

    def test_auto_chain_free_first_and_eligibility(self):
        s = self.doc["suggestions"]["openai"]["facet-auto"]
        # pay-small (32k context), no-tool (no tool_call), ghost (unresolved) excluded
        self.assertEqual(s["suggested"], ["free-a", "pay-big"])

    def test_auto_criteria_skips_are_categorized(self):
        self.assertIn("no-tool", self.doc["skipped"]["no_tool_call"])
        self.assertIn("pay-small", self.doc["skipped"]["small_context"])
        self.assertIn("ghost", self.doc["skipped"]["unresolved_flagship"])

    def test_cheap_chain_orders_by_cost_within_tier(self):
        s = self.doc["suggestions"]["openai"]["facet-cheap"]
        # free first, then cheaper output price; unknown cost sinks within its tier
        self.assertEqual(s["suggested"], ["free-a", "no-tool", "pay-small", "pay-big"])

    def test_anthropic_chain_and_current_echo(self):
        s = self.doc["suggestions"]["anthropic"]["facet-auto"]
        self.assertEqual(s["suggested"], ["claudy"])
        self.assertEqual(s["current"], ["claudy"])

    def test_current_chain_echoed_for_diff_review(self):
        self.assertEqual(self.doc["suggestions"]["openai"]["facet-auto"]["current"], ["pay-big"])

    def test_evidence_carries_registry_facts(self):
        ev = self.doc["suggestions"]["openai"]["facet-auto"]["evidence"]["pay-big"]
        self.assertEqual(ev["model"], "openai/gpt-big")
        self.assertEqual(ev["context_window"], 400000)
        self.assertEqual(ev["output_per_mtok"], 8.0)
        self.assertEqual(ev["tier"], "paid")

    def test_report_only_policy_note(self):
        self.assertTrue(self.doc["policy"]["note"].startswith("report-only"))


if __name__ == "__main__":
    unittest.main()
