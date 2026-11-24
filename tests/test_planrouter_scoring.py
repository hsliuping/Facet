"""Per-(channel, model) stability scores and aggregated model states."""
from __future__ import annotations

import time
import unittest

from facet.gateway.core import StateStore, parse_config
from facet.gateway.persistence import capture, restore
from facet.gateway.server import App


def scoring_config() -> dict:
    key = "sk-fixture"
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        # PickOrderingTests 用 "whatever" 这个无映射名字当载体测排序：
        # 显式开启 fallback_unknown 保持按链调度，不依赖已翻转的默认值。
        "models": {"fallback_unknown": True},
        "channels": [
            # "auto" drives internal scheduling; "m1"/"m2" are explicit
            # mappings visible to the model-state aggregation.
            {"name": "free-a", "base_url": "https://a.example/v1", "api_key": key,
             "priority": 10, "tier": "free",
             "models": {"auto": "auto-a", "m1": "m1a"}},
            {"name": "free-b", "base_url": "https://b.example/v1", "api_key": key,
             "priority": 20, "tier": "free",
             "models": {"auto": "auto-b", "m1": "m1b"}},
            # Disabled: its model is "no_channel" in the aggregate view.
            {"name": "paid-off", "base_url": "https://c.example/v1", "api_key": key,
             "tier": "paid", "enabled": False, "models": {"m3": "m3"}},
            {"name": "anthro-a", "protocol": "anthropic",
             "base_url": "https://d.example/v1", "api_key": key,
             "models": {"claude-x": "claude-x"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai",
             "channels": ["free-a", "free-b"]},
            {"name": "default", "protocol": "anthropic",
             "channels": ["anthro-a"]},
        ],
    }


class ScoreBookkeepingTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(parse_config(scoring_config()))

    def test_success_adds_one_failure_drops_five(self):
        self.store.record_score("free-a", "auto-a", 1, now=1000.0)
        self.store.record_score("free-a", "auto-a", 1, now=1000.0)
        self.store.record_score("free-a", "auto-a", -5, now=1000.0)
        self.assertEqual(self.store.score("free-a", "auto-a", now=1000.0), -3)

    def test_unknown_pair_is_zero(self):
        self.assertEqual(self.store.score("free-a", "never-seen"), 0)

    def test_scores_for_filters_by_channel(self):
        self.store.record_score("free-a", "auto-a", 2, now=1000.0)
        self.store.record_score("free-b", "auto-b", -5, now=1000.0)
        self.assertEqual(self.store.scores_for("free-a", now=1000.0),
                         {"auto-a": 2})
        self.assertEqual(self.store.scores_for("free-b", now=1000.0),
                         {"auto-b": -5})


class DecayTests(unittest.TestCase):
    HALF = 12 * 3600.0  # score_halflife_hours=12 default

    def setUp(self):
        self.store = StateStore(parse_config(scoring_config()))

    def test_score_decays_toward_zero(self):
        self.store.record_score("free-a", "auto-a", -5, now=0.0)
        self.assertAlmostEqual(
            self.store.score("free-a", "auto-a", now=self.HALF), -2.5)

    def test_read_is_idempotent(self):
        self.store.record_score("free-a", "auto-a", -5, now=0.0)
        at = 3600.0
        self.assertEqual(self.store.score("free-a", "auto-a", now=at),
                         self.store.score("free-a", "auto-a", now=at))

    def test_record_refreshes_after_decay(self):
        self.store.record_score("free-a", "auto-a", -5, now=0.0)
        self.store.record_score("free-a", "auto-a", 1, now=self.HALF)
        # -5 * 0.5 + 1 = -1.5, timestamp refreshed so no further decay at t=HALF.
        self.assertAlmostEqual(
            self.store.score("free-a", "auto-a", now=self.HALF), -1.5)

    def test_zero_halflife_disables_decay(self):
        raw = scoring_config()
        raw["defaults"] = {"score_halflife_hours": 0}
        store = StateStore(parse_config(raw))
        store.record_score("free-a", "auto-a", -5, now=0.0)
        self.assertEqual(
            store.score("free-a", "auto-a", now=1_000_000.0), -5.0)

    def test_reset_all_one_channel_one_wire(self):
        self.store.record_score("free-a", "auto-a", -5, now=1000.0)
        self.store.record_score("free-a", "m1a", 3, now=1000.0)
        self.store.record_score("free-b", "auto-b", -2, now=1000.0)
        self.assertEqual(self.store.reset_scores("free-a", "auto-a"), 1)
        self.assertEqual(self.store.scores_for("free-a", now=1000.0),
                         {"m1a": 3})
        self.assertEqual(self.store.reset_scores("free-a"), 1)
        self.assertEqual(self.store.scores_for("free-a"), {})
        self.assertEqual(self.store.reset_scores(), 1)
        self.assertEqual(self.store.scores, {})


class PickOrderingTests(unittest.TestCase):
    def setUp(self):
        self.config = parse_config(scoring_config())
        self.store = StateStore(self.config)

    def candidates(self, session_id=None):
        pairs, route = self.store.pick("openai", "default", "whatever", session_id)
        return [channel.name for channel, _ in pairs]

    def test_equal_score_keeps_chain_order(self):
        self.store.record_score("free-a", "auto-a", 2, now=0.0)
        self.store.record_score("free-b", "auto-b", 2, now=0.0)
        self.assertEqual(self.candidates(), ["free-a", "free-b"])

    def test_sticky_channel_stays_first_despite_lower_score(self):
        self.store.record_score("free-a", "auto-a", -5)
        self.store.bind_session("sess-1", "free-a", time.time())
        self.assertEqual(self.candidates("sess-1"), ["free-a", "free-b"])

    def test_chain_order_wins_over_higher_score(self):
        # Editing the route order in the console must take effect immediately;
        # a healthy score never outranks the operator's chain position.
        self.store.record_score("free-a", "auto-a", -2)
        self.store.record_score("free-b", "auto-b", 3)
        self.assertEqual(self.candidates(), ["free-a", "free-b"])

    def test_demoted_pair_sinks_behind_healthy_and_untested(self):
        # -5 is a fresh failure: the pair drops to the back so it does not add
        # a failed hop to every request, while free-b keeps its chain position.
        self.store.record_score("free-a", "auto-a", -5)
        self.assertEqual(self.candidates(), ["free-b", "free-a"])

    def test_untested_pair_keeps_its_chain_position(self):
        self.store.record_score("free-b", "auto-b", 3)
        self.assertEqual(self.candidates(), ["free-a", "free-b"])


class PersistenceTests(unittest.TestCase):
    def test_capture_restore_roundtrip(self):
        store = StateStore(parse_config(scoring_config()))
        store.record_score("free-a", "auto-a", 4, now=1000.0)
        store.record_score("free-b", "auto-b", -5, now=1000.0)
        data = capture(store)
        other = StateStore(parse_config(scoring_config()))
        restore(other, data)
        self.assertEqual(other.score("free-a", "auto-a", now=1000.0), 4)
        self.assertEqual(other.score("free-b", "auto-b", now=1000.0), -5)

    def test_restore_drops_unknown_channels(self):
        store = StateStore(parse_config(scoring_config()))
        restore(store, {"scores": {"gone|model": 7, "free-a|auto-a": 2,
                                   "no-pipe": 1, "free-a|auto-a": "x"}})
        # "gone|model" names a missing channel, "no-pipe" lacks the pipe
        # separator and "free-a|auto-a" maps to a non-int: all dropped.
        self.assertEqual(store.scores, {})

    def test_restore_legacy_int_scores_kept_as_recent(self):
        store = StateStore(parse_config(scoring_config()))
        restore(store, {"scores": {"free-a|auto-a": 2}}, now=1000.0)
        # Bare int (no timestamp) is treated as just-recorded at `now`.
        self.assertEqual(store.scores, {"free-a|auto-a": [2.0, 1000.0]})
        self.assertEqual(store.score("free-a", "auto-a", now=1000.0), 2.0)


class AppScoringTests(unittest.TestCase):
    def setUp(self):
        self.app = App(parse_config(scoring_config()))

    def test_swap_preserves_scores_and_drops_deleted_channels(self):
        self.app.store.record_score("free-a", "auto-a", 3, now=1000.0)
        raw = scoring_config()
        raw["channels"] = [c for c in raw["channels"] if c["name"] != "free-b"]
        raw["routes"][0]["channels"] = ["free-a"]
        self.app._swap(parse_config(raw))
        self.assertEqual(self.app.store.score("free-a", "auto-a", now=1000.0), 3)
        self.assertEqual(self.app.store.scores_for("free-b"), {})

    def test_ui_state_exposes_scores(self):
        self.app.store.record_score("free-a", "auto-a", 2)
        item = next(c for c in self.app.ui_state()["channels"]
                    if c["name"] == "free-a")
        self.assertEqual(item["scores"], {"auto-a": 2})

    def test_model_states_free_paid_and_no_channel(self):
        states = {row["model"]: row["status"]
                  for row in self.app.ui_state()["model_states"]}
        # Both enabled free channels map m1; the disabled paid channel's m3
        # is reachable by nobody.
        self.assertEqual(states["m1"], "free_ok")
        self.assertEqual(states["m3"], "no_channel")

    def test_model_states_paid_only(self):
        raw = scoring_config()
        for channel in raw["channels"]:
            if channel.get("tier") == "free":
                channel["tier"] = "paid"
        app = App(parse_config(raw))
        states = {row["model"]: row["status"]
                  for row in app.ui_state()["model_states"]}
        self.assertEqual(states["m1"], "paid_only")

    def test_model_states_untiered_channel_is_available(self):
        # Enabled channel with no tier annotation (no linked subscription):
        # usable, but neither free nor paid.
        raw = scoring_config()
        raw["channels"][0]["tier"] = ""
        app = App(parse_config(raw))
        states = {row["model"]: row["status"]
                  for row in app.ui_state()["model_states"]}
        self.assertEqual(states["m1"], "free_ok")  # free-b still serves it
        row = next(r for r in app.ui_state()["model_states"]
                   if r["model"] == "m1")
        tiers = {entry["name"]: entry["tier"] for entry in row["channels"]}
        self.assertEqual(tiers["free-a"], "")
        self.assertEqual(tiers["free-b"], "free")

    def test_model_states_all_untiered_is_available(self):
        raw = scoring_config()
        for channel in raw["channels"]:
            if channel.get("tier") == "free":
                channel["tier"] = ""
        app = App(parse_config(raw))
        states = {row["model"]: row["status"]
                  for row in app.ui_state()["model_states"]}
        self.assertEqual(states["m1"], "available")

    def test_model_states_free_exhausted_from_catalog(self):
        # The upstream still advertises the model, but no mapping remains:
        # exactly what freealign pruning looks like.
        self.app.store.record_catalog("free-a", ["gone-model"], "https://a.example/v1")
        states = {row["model"]: row["status"]
                  for row in self.app.ui_state()["model_states"]}
        self.assertEqual(states["gone-model"], "free_exhausted")
        row = next(r for r in self.app.ui_state()["model_states"]
                   if r["model"] == "gone-model")
        self.assertEqual(row["channels"][0]["name"], "free-a")

    def test_manual_model_tiers_override_channel_tier(self):
        raw = scoring_config()
        raw["channels"][0]["tiers"] = {"m1": "paid"}  # per-model override
        app = App(parse_config(raw))
        states = {row["model"]: row["status"]
                  for row in app.ui_state()["model_states"]}
        # free-a's m1 is billed, but free-b still serves it for free.
        self.assertEqual(states["m1"], "free_ok")
        row = next(r for r in app.ui_state()["model_states"] if r["model"] == "m1")
        tiers = {entry["name"]: entry["tier"] for entry in row["channels"]}
        self.assertEqual(tiers["free-a"], "paid")
        self.assertEqual(tiers["free-b"], "free")

    @staticmethod
    def _openrouter_raw():
        raw = scoring_config()
        raw["channels"] = [{
            "name": "or", "protocol": "openai", "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-x",
            # 向导会把整个渠道标成 tier=free；但 OpenRouter 只有 ":free"
            # 后缀的模型真免费，其余映射按账号计费。
            "tier": "free",
            "models": {
                "auto": "deepseek/deepseek-chat-v3:free",
                "deepseek/deepseek-chat-v3:free": "deepseek/deepseek-chat-v3:free",
                "aion-labs/aion-2.0": "aion-labs/aion-2.0",
            },
        }, {
            "name": "cla", "protocol": "anthropic",
            "base_url": "https://cla.example/v1", "api_key": "k",
            "models": {"auto": "claude-x"},
        }]
        raw["routes"][0]["channels"] = ["or"]
        raw["routes"][1]["channels"] = ["cla"]
        return raw

    def test_model_states_openrouter_paid_suffix_not_free(self):
        app = App(parse_config(self._openrouter_raw()))
        states = {row["model"]: row["status"]
                  for row in app.ui_state()["model_states"]}
        self.assertEqual(states["deepseek/deepseek-chat-v3:free"], "free_ok")
        self.assertEqual(states["aion-labs/aion-2.0"], "paid_only")
        row = next(r for r in app.ui_state()["model_states"]
                   if r["model"] == "aion-labs/aion-2.0")
        self.assertEqual(row["channels"][0]["tier"], "paid")

    def test_model_states_openrouter_manual_override_wins(self):
        # 渠道级 tiers 手工档位仍然最高：把 ":free" 模型标成 paid 要尊重。
        raw = self._openrouter_raw()
        raw["channels"][0]["tiers"] = {"deepseek/deepseek-chat-v3:free": "paid"}
        app = App(parse_config(raw))
        states = {row["model"]: row["status"]
                  for row in app.ui_state()["model_states"]}
        self.assertEqual(states["deepseek/deepseek-chat-v3:free"], "paid_only")

    def test_model_states_openrouter_catalog_free_suffix_only(self):
        # 目录里 openrouter 也会列出收费款：未映射时不应被当成
        # 「免费已耗尽」的免费候选（那是免费渠道目录语义）。
        raw = self._openrouter_raw()
        del raw["channels"][0]["models"]["aion-labs/aion-2.0"]
        app = App(parse_config(raw))
        app.store.record_catalog("or", ["aion-labs/aion-2.0",
                                        "deepseek/deepseek-chat-v3:free",
                                        "gone/nova:free"],
                                 "https://openrouter.ai/api/v1")
        states = {row["model"]: row["status"]
                  for row in app.ui_state()["model_states"]}
        self.assertNotIn("aion-labs/aion-2.0", states)
        self.assertEqual(states["deepseek/deepseek-chat-v3:free"], "free_ok")
        self.assertEqual(states["gone/nova:free"], "free_exhausted")


if __name__ == "__main__":
    unittest.main()
