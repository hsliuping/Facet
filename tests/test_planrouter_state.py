"""Cooldown state survives restarts."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from planrouter.core import parse_config
from planrouter.persistence import StatePersistence, capture, restore
from planrouter.server import App


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


if __name__ == "__main__":
    unittest.main()
