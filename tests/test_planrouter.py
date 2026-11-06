"""Unit tests for channel cooldown detection, config validation and routing."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from planrouter.core import OPENAI, StateStore, load_config, parse_cooldown, wire_model

BASE = {
    "listen": {"host": "127.0.0.1", "port": 8787},
    "channels": [
        {
            "name": "plan-a", "protocol": "openai", "base_url": "http://127.0.0.1:9001",
            "models": {"auto": "model-a", "task-model": "wire-a"}, "priority": 10,
            "rate": {"rpm": 30},
        },
        {
            "name": "plan-b", "protocol": "openai", "base_url": "http://127.0.0.1:9002",
            "models": {"*": "*", "auto": "model-b"}, "priority": 20,
        },
        {
            "name": "free", "protocol": "openai", "base_url": "http://127.0.0.1:9003",
            "models": {"auto": "model-free"}, "priority": 30, "rate": {"rpm": 1},
        },
        {
            "name": "claude", "protocol": "anthropic", "base_url": "http://127.0.0.1:9004",
            "models": {"auto": "claude-sonnet-4-5"}, "priority": 5,
        },
    ],
    "routes": [
        {"name": "default", "protocol": "openai", "channels": ["plan-a", "plan-b", "free"]},
        {"name": "cheap", "protocol": "openai", "channels": ["free", "plan-b"]},
        {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
    ],
}


def write_config(data: dict) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, tmp)
    tmp.close()
    return Path(tmp.name)


class CooldownTests(unittest.TestCase):
    def test_retry_after_seconds(self):
        quota, seconds, reason = parse_cooldown(
            429, {"Retry-After": "7200"}, b'{"error":"rate limit"}', 300, 168, now=1000,
        )
        self.assertTrue(quota)
        self.assertEqual(seconds, 7200.0)
        self.assertEqual(reason, "rate limit")

    def test_iso_reset_header(self):
        reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        quota, seconds, _ = parse_cooldown(
            429, {"anthropic-ratelimit-unified-5h-reset": reset},
            b'{"error":{"type":"rate_limit_error"}}', 300, 168,
        )
        self.assertTrue(quota)
        self.assertAlmostEqual(seconds, 7200, delta=5)

    def test_quota_marker_uses_configured_default(self):
        quota, seconds, reason = parse_cooldown(
            400, {}, b'{"error":"usage limit reached"}', 300, 168,
        )
        self.assertTrue(quota)
        self.assertEqual(seconds, 18000.0)
        self.assertEqual(reason, "usage limit")

    def test_transient_5xx_is_short_and_half_open(self):
        quota, seconds, reason = parse_cooldown(500, {}, b"upstream exploded", 300, 168)
        self.assertFalse(quota)
        self.assertEqual(seconds, 30.0)
        self.assertTrue(reason.startswith("transient"))

    def test_success_is_not_cooled(self):
        self.assertEqual(parse_cooldown(200, {}, b"{}", 300, 168), (False, 0.0, ""))

    def test_cooldown_is_capped(self):
        _, seconds, _ = parse_cooldown(429, {"Retry-After": "999999"}, b"{}", 300, 6, now=1000)
        self.assertEqual(seconds, 6 * 3600)


class ConfigTests(unittest.TestCase):
    def test_load_and_env_secret(self):
        os.environ["PLANROUTER_TEST_KEY"] = "secret-value"
        try:
            data = json.loads(json.dumps(BASE))
            data["channels"][0]["api_key"] = "env:PLANROUTER_TEST_KEY"
            cfg = load_config(write_config(data))
            self.assertEqual(cfg.channel("plan-a").api_key, "secret-value")
            self.assertEqual(cfg.route(OPENAI, "cheap").channels, ("free", "plan-b"))
        finally:
            os.environ.pop("PLANROUTER_TEST_KEY", None)

    def test_mixed_protocols_in_one_route_rejected(self):
        data = json.loads(json.dumps(BASE))
        data["routes"][0]["channels"] = ["plan-a", "claude"]
        with self.assertRaisesRegex(ValueError, "never converted"):
            load_config(write_config(data))

    def test_non_loopback_requires_gateway_key(self):
        data = json.loads(json.dumps(BASE))
        data["listen"] = {"host": "0.0.0.0", "port": 8787}
        with self.assertRaisesRegex(ValueError, "requires listen.api_key"):
            load_config(write_config(data))

    def test_missing_default_route_rejected(self):
        data = json.loads(json.dumps(BASE))
        data["routes"] = [r for r in data["routes"] if r["protocol"] != "anthropic"]
        with self.assertRaisesRegex(ValueError, "anthropic:default"):
            load_config(write_config(data))


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(write_config(BASE))
        self.store = StateStore(self.cfg)
        self.now = time.time()

    def test_order_follows_priority(self):
        candidates, route = self.store.pick(OPENAI, "default", "auto", None, self.now)
        self.assertEqual([c.name for c, _ in candidates], ["plan-a", "plan-b", "free"])
        self.assertEqual(route.name, "default")

    def test_wire_name_mapping(self):
        self.assertEqual(wire_model(self.cfg.channel("plan-a"), "task-model"), "wire-a")
        self.assertEqual(wire_model(self.cfg.channel("plan-b"), "anything"), "anything")
        self.assertIsNone(wire_model(self.cfg.channel("plan-a"), "unknown-model"))

    def test_cooling_channel_is_skipped(self):
        self.store.states["plan-a"].mark_cooldown(5 * 3600, "quota", self.now)
        candidates, _ = self.store.pick(OPENAI, "default", "auto", None, self.now)
        self.assertEqual([c.name for c, _ in candidates], ["plan-b", "free"])
        snap = self.store.snapshot(self.now)["channels"]["plan-a"]
        self.assertTrue(snap["cooling"])
        self.assertAlmostEqual(snap["cooling_seconds"], 5 * 3600, delta=1)

    def test_half_open_probe_allowed_after_transient_error(self):
        self.store.states["plan-a"].mark_cooldown(30, "transient", self.now, half_open=True)
        ok, why = self.store.states["plan-a"].available(self.now, self.cfg.channel("plan-a").rate)
        self.assertTrue(ok)
        self.assertEqual(why, "half_open_probe")

    def test_rpm_cap_excludes_channel(self):
        state = self.store.states["free"]
        state.note_attempt(self.now)
        ok, why = state.available(self.now, self.cfg.channel("free").rate)
        self.assertFalse(ok)
        self.assertEqual(why, "rpm_cap")

    def test_sticky_session_moves_channel_to_front(self):
        self.store.bind_session("session-1", "plan-b", self.now)
        candidates, _ = self.store.pick(OPENAI, "default", "auto", "session-1", self.now)
        self.assertEqual(candidates[0][0].name, "plan-b")

    def test_sticky_session_expires(self):
        self.store.bind_session("old", "plan-b", self.now - 3600)
        self.assertIsNone(self.store.session_channel("old", self.now))

    def test_cheap_route_and_model_filter(self):
        candidates, _ = self.store.pick(OPENAI, "cheap", "auto", None, self.now)
        self.assertEqual([c.name for c, _ in candidates], ["free", "plan-b"])
        candidates, _ = self.store.pick(OPENAI, "default", "task-model", None, self.now)
        self.assertEqual([c.name for c, _ in candidates], ["plan-a", "plan-b"])


if __name__ == "__main__":
    unittest.main()
