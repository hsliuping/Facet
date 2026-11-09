"""$free route macro: expansion order, filtering and error handling."""
from __future__ import annotations

import unittest

from planrouter.core import parse_config


def base_config() -> dict:
    """Five openai channels + one anthropic channel, all with API keys."""
    key = "sk-fixture"
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "paid-a", "base_url": "https://a.example/v1", "api_key": key,
             "priority": 10, "tier": "paid", "models": {"m1": "m1"}},
            {"name": "free-b", "base_url": "https://b.example/v1", "api_key": key,
             "priority": 20, "tier": "free", "models": {"m2": "m2"}},
            {"name": "free-c", "base_url": "https://c.example/v1", "api_key": key,
             "priority": 30, "tier": "free", "models": {"m3": "m3"}},
            {"name": "free-a", "base_url": "https://d.example/v1", "api_key": key,
             "priority": 20, "tier": "free", "models": {"m4": "m4"}},
            # tier left empty: never enters the macro.
            {"name": "untiered", "base_url": "https://e.example/v1", "api_key": key,
             "models": {"m5": "m5"}},
            # Disabled: never enters the macro.
            {"name": "free-off", "base_url": "https://f.example/v1", "api_key": key,
             "priority": 5, "tier": "free", "enabled": False, "models": {"m6": "m6"}},
            {"name": "anthro-free", "protocol": "anthropic",
             "base_url": "https://g.example/v1", "api_key": key,
             "tier": "free", "models": {"claude-x": "claude-x"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["$free"]},
            {"name": "default", "protocol": "anthropic",
             "channels": ["anthro-free"]},
        ],
    }


class FreeMacroTests(unittest.TestCase):
    def test_expansion_order_is_priority_then_name(self):
        config = parse_config(base_config())
        route = config.route("openai", "default")
        # channels table is sorted by (priority, name): free-b and free-a tie
        # on priority 20 so free-a precedes free-b.
        self.assertEqual(route.channels, ("free-a", "free-b", "free-c"))

    def test_explicit_channel_keeps_position_and_dedupes(self):
        raw = base_config()
        raw["routes"][0]["channels"] = ["paid-a", "$free", "free-c"]
        config = parse_config(raw)
        route = config.route("openai", "default")
        self.assertEqual(route.channels, ("paid-a", "free-a", "free-b", "free-c"))

    def test_macro_filters_by_protocol(self):
        raw = base_config()
        raw["routes"][0]["channels"] = ["$free", "paid-a"]
        config = parse_config(raw)
        route = config.route("openai", "default")
        self.assertNotIn("anthro-free", route.channels)

    def test_untiered_and_disabled_channels_excluded(self):
        config = parse_config(base_config())
        route = config.route("openai", "default")
        self.assertNotIn("untiered", route.channels)
        self.assertNotIn("free-off", route.channels)

    def test_macro_with_no_free_channels_raises(self):
        raw = base_config()
        for channel in raw["channels"]:
            if channel.get("tier") == "free":
                channel["tier"] = "paid"
        with self.assertRaises(ValueError):
            parse_config(raw)

    def test_unknown_explicit_channel_still_raises(self):
        raw = base_config()
        raw["routes"][0]["channels"] = ["$free", "no-such-channel"]
        with self.assertRaises(ValueError):
            parse_config(raw)

    def test_macro_mid_chain_inserts_in_place(self):
        raw = base_config()
        raw["routes"][0]["channels"] = ["free-c", "$free"]
        config = parse_config(raw)
        route = config.route("openai", "default")
        # free-c listed first stays first; the macro fills in the rest.
        self.assertEqual(route.channels, ("free-c", "free-a", "free-b"))

    def test_explicit_free_after_macro_keeps_position(self):
        raw = base_config()
        # free-a is tier=free and would sort first inside the macro; listing
        # it explicitly AFTER the macro must still pin it to the end rather
        # than letting the macro drag it forward.
        raw["routes"][0]["channels"] = ["$free", "free-a"]
        config = parse_config(raw)
        route = config.route("openai", "default")
        self.assertEqual(route.channels, ("free-b", "free-c", "free-a"))

    def test_anthropic_macro_uses_anthropic_free_channels(self):
        raw = base_config()
        raw["routes"][1]["channels"] = ["$free"]
        config = parse_config(raw)
        route = config.route("anthropic", "default")
        self.assertEqual(route.channels, ("anthro-free",))


if __name__ == "__main__":
    unittest.main()
