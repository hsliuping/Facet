"""Cooldown state survives restarts."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from planrouter.core import StateStore, parse_config
from planrouter.persistence import StatePersistence, capture, restore
from planrouter.server import App, channel_tier


def config_dict() -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "a", "protocol": "openai", "base_url": "http://127.0.0.1:9201",
             "models": {"auto": "model-a"}},
            {"name": "b", "protocol": "anthropic", "base_url": "http://127.0.0.1:9202",
             "models": {"auto": "model-b"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["a"]},
            {"name": "default", "protocol": "anthropic", "channels": ["b"]},
        ],
    }


def rotation_config() -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "a", "protocol": "openai", "base_url": "http://127.0.0.1:9201",
             "models": {"auto": "model-a"}},
            {"name": "b", "protocol": "openai", "base_url": "http://127.0.0.1:9202",
             "models": {"auto": "model-b"}},
            {"name": "c", "protocol": "openai", "base_url": "http://127.0.0.1:9203",
             "models": {"auto": "model-c"}, "priority": 200},
            {"name": "claude", "protocol": "anthropic",
             "base_url": "http://127.0.0.1:9204", "models": {"auto": "claude-x"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["a", "b", "c"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
        ],
    }


class RoundRobinTests(unittest.TestCase):
    """New sessions rotate the starting channel inside the top-priority tier."""

    def setUp(self):
        self.store = StateStore(parse_config(rotation_config()))

    def pick(self, session):
        picked, route = self.store.pick("openai", "default", "some-model",
                                        session, now=1000.0)
        self.assertIsNotNone(route)
        return [channel.name for channel, _ in picked]

    def test_new_sessions_rotate_within_top_tier(self):
        self.assertEqual(self.pick("s1"), ["a", "b", "c"])
        self.assertEqual(self.pick("s2"), ["b", "a", "c"])
        self.assertEqual(self.pick("s3"), ["a", "b", "c"])
        self.assertEqual(self.pick("s4"), ["b", "a", "c"])

    def test_low_priority_channel_keeps_tail_position(self):
        for i in range(4):
            self.assertEqual(self.pick(f"s{i}")[-1], "c")

    def test_sticky_session_skips_rotation(self):
        self.pick("s1")  # cursor advances to 1
        self.store.bind_session("s2", "b", 1000.0)
        self.assertEqual(self.pick("s2"), ["b", "a", "c"])
        self.assertEqual(self.store.rr_cursor.get("openai:default"), 1)

    def test_single_tier_channel_is_untouched(self):
        raw = rotation_config()
        raw["routes"][0]["channels"] = ["a", "c"]
        store = StateStore(parse_config(raw))
        picked, _ = store.pick("openai", "default", "some-model", "s1", now=1000.0)
        self.assertEqual([c.name for c, _ in picked], ["a", "c"])
        self.assertEqual(store.rr_cursor, {})

    def test_cursor_survives_capture_restore(self):
        self.pick("s1")  # cursor advances to 1
        snapshot = capture(self.store, 1000.0)
        revived = StateStore(parse_config(rotation_config()))
        restore(revived, snapshot, 1000.0)
        picked, _ = revived.pick("openai", "default", "some-model", "s9", now=1000.0)
        self.assertEqual([c.name for c, _ in picked], ["b", "a", "c"])


class PersistenceTests(unittest.TestCase):
    def test_future_cooldown_is_restored(self):
        app = App(parse_config(config_dict()))
        now = time.time()
        app.store.states["a"].mark_cooldown(5 * 3600, "quota", now)
        app.store.states["a"].note_attempt(now)
        snapshot = capture(app.store, now)

        restarted = App(parse_config(config_dict()))
        restore(restarted.store, snapshot, now)
        state = restarted.store.states["a"]
        self.assertGreater(state.cooldown_until, now)
        self.assertEqual(state.reason, "quota")
        self.assertEqual(state.total_requests, 1)
        self.assertTrue(restarted.store.snapshot(now)["channels"]["a"]["cooling"])

    def test_expired_cooldown_is_dropped(self):
        app = App(parse_config(config_dict()))
        now = time.time()
        app.store.states["a"].mark_cooldown(3600, "quota", now - 7200)
        snapshot = capture(app.store, now)
        restarted = App(parse_config(config_dict()))
        restore(restarted.store, snapshot, now + 60)
        self.assertFalse(restarted.store.snapshot(now + 60)["channels"]["a"]["cooling"])
        self.assertEqual(restarted.store.states["a"].cooldown_until, 0.0)

    def test_flush_and_restart_via_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "router.state.json"
            app = App(parse_config(config_dict()))
            now = time.time()
            app.store.states["a"].mark_cooldown(3 * 3600, "usage limit", now)
            persistence = StatePersistence(state_path, lambda: app.store)
            persistence.flush()
            self.assertTrue(state_path.is_file())

            restarted = App(parse_config(config_dict()), state_file=state_path)
            state = restarted.store.states["a"]
            self.assertGreater(state.cooldown_until, time.time())
            self.assertEqual(state.reason, "usage limit")

    def test_flush_skips_unchanged_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "router.state.json"
            app = App(parse_config(config_dict()))
            persistence = StatePersistence(state_path, lambda: app.store)
            persistence.flush()
            first = json.loads(state_path.read_text(encoding="utf-8"))["saved_at"]
            persistence.flush()
            second = json.loads(state_path.read_text(encoding="utf-8"))["saved_at"]
            self.assertEqual(first, second)


class TierTests(unittest.TestCase):
    """Channel tier: explicit free/paid field, inferred from subscriptions."""

    def test_parse_tier_accepts_free_and_paid(self):
        raw = rotation_config()
        raw["channels"][0]["tier"] = "free"
        raw["channels"][1]["tier"] = "Paid "
        config = parse_config(raw)
        self.assertEqual(config.channels[0].tier, "free")
        self.assertEqual(config.channels[1].tier, "paid")

    def test_parse_tier_rejects_unknown_value(self):
        raw = rotation_config()
        raw["channels"][0]["tier"] = "expensive"
        with self.assertRaises(ValueError):
            parse_config(raw)

    def test_channel_tier_inference(self):
        raw = rotation_config()
        raw["channels"][0]["tier"] = "free"
        config = parse_config(raw)
        by_name = {c.name: c for c in config.channels}
        subs = [{"name": "plan", "price": 39, "channels": ["b"]},
                {"name": "trial", "price": 0, "channels": ["c"]}]
        self.assertEqual(channel_tier(by_name["a"], subs), "free")  # explicit wins
        self.assertEqual(channel_tier(by_name["b"], subs), "paid")  # priced sub
        self.assertEqual(channel_tier(by_name["c"], subs), "")      # zero price
        self.assertEqual(channel_tier(by_name["claude"], subs), "")  # unlinked


if __name__ == "__main__":
    unittest.main()
