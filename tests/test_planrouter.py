"""Unit tests for channel cooldown detection, config validation and routing."""
from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from facet.gateway.core import (OPENAI, StateStore, body_has_vision,
                             channel_takes_images, load_config,
                             open_with_proxy, parse_cooldown, vision_wire,
                             wire_model)
from facet.gateway.keys import KeyStore, normalize_limits
from facet.gateway.server import App, HealthProber

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

    def test_not_found_cooldown(self):
        quota, seconds, reason = parse_cooldown(
            404, {}, b'{"error":"model not found"}', 300, 168, now=1000,
            not_found_hours=24,
        )
        self.assertTrue(quota)
        self.assertEqual(seconds, 86400.0)
        self.assertEqual(reason, "http_404_not_found")

    def test_not_found_cooldown_disabled_keeps_transient(self):
        quota, seconds, reason = parse_cooldown(
            404, {}, b'{"error":"model not found"}', 300, 168, now=1000,
        )
        self.assertFalse(quota)
        self.assertEqual(seconds, 30.0)
        self.assertEqual(reason, "transient_http_404")

    def test_daily_429_cools_until_utc_midnight(self):
        # 2026-06-01 12:00 UTC -> 12h until the next UTC midnight.
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        quota, seconds, reason = parse_cooldown(
            429, {}, b'{"error":"free-models-per-day limit reached"}', 300, 168,
            now=now,
        )
        self.assertTrue(quota)
        self.assertAlmostEqual(seconds, 12 * 3600, delta=5)
        self.assertEqual(reason, "daily_quota_reset")

    def test_daily_429_chinese_marker(self):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        quota, _, reason = parse_cooldown(
            429, {}, '{"error":"每日免费额度已用完"}'.encode("utf-8"), 300, 168,
            now=now, daily_hours=6,
        )
        self.assertTrue(quota)
        self.assertEqual(reason, "daily_quota_reset")

    def test_daily_429_fixed_hours(self):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        quota, seconds, _ = parse_cooldown(
            429, {}, b'{"error":"daily quota exhausted"}', 300, 168,
            now=now, daily_hours=6,
        )
        self.assertTrue(quota)
        self.assertEqual(seconds, 6 * 3600)

    def test_daily_429_disabled_falls_back_to_default(self):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        quota, seconds, reason = parse_cooldown(
            429, {}, b'{"error":"per-day limit reached"}', 300, 168,
            now=now, daily_hours=-1,
        )
        self.assertTrue(quota)
        self.assertEqual(seconds, 18000.0)
        self.assertEqual(reason, "http_429")

    def test_daily_429_retry_after_header_wins(self):
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        quota, seconds, _ = parse_cooldown(
            429, {"Retry-After": "600"}, b'{"error":"daily quota exhausted"}', 300, 168,
            now=now,
        )
        self.assertTrue(quota)
        self.assertEqual(seconds, 600.0)


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

    def test_new_default_fields(self):
        data = json.loads(json.dumps(BASE))
        cfg = load_config(write_config(data))
        self.assertEqual(cfg.defaults.not_found_cooldown_hours, 24)
        self.assertEqual(cfg.defaults.daily_429_cooldown_hours, 0)
        self.assertEqual(cfg.defaults.network_retries, 1)
        data["defaults"] = {
            "not_found_cooldown_hours": 0,
            "daily_429_cooldown_hours": 12,
            "network_retries": 0,
        }
        cfg = load_config(write_config(data))
        self.assertEqual(cfg.defaults.not_found_cooldown_hours, 0)
        self.assertEqual(cfg.defaults.daily_429_cooldown_hours, 12)
        self.assertEqual(cfg.defaults.network_retries, 0)

    def test_mixed_protocols_in_one_route_allowed(self):
        # P1-3: payloads are converted at the channel boundary, so a route may
        # chain channels speaking different wire protocols.
        data = json.loads(json.dumps(BASE))
        data["routes"][0]["channels"] = ["plan-a", "claude"]
        cfg = load_config(write_config(data))
        self.assertEqual(cfg.route("openai", "default").channels, ("plan-a", "claude"))

    def test_route_system_prompt_parsed(self):
        data = json.loads(json.dumps(BASE))
        data["routes"][0]["system_prompt"] = "Answer in Chinese."
        cfg = load_config(write_config(data))
        self.assertEqual(cfg.route("openai", "default").system_prompt, "Answer in Chinese.")
        self.assertEqual(cfg.route("openai", "cheap").system_prompt, "")

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

    def test_channel_proxy(self):
        data = json.loads(json.dumps(BASE))
        cfg = load_config(write_config(data))
        self.assertEqual(cfg.channel("plan-a").proxy, "")
        data["channels"][0]["proxy"] = "env:PLANROUTER_TEST_PROXY"
        os.environ["PLANROUTER_TEST_PROXY"] = "http://127.0.0.1:7890"
        try:
            cfg = load_config(write_config(data))
            self.assertEqual(cfg.channel("plan-a").proxy,
                             "http://127.0.0.1:7890")
        finally:
            os.environ.pop("PLANROUTER_TEST_PROXY", None)
        data["channels"][0]["proxy"] = "socks5://127.0.0.1:7890"
        with self.assertRaisesRegex(ValueError, "proxy"):
            load_config(write_config(data))


class OpenWithProxyTests(unittest.TestCase):
    def test_empty_proxy_keeps_plain_urlopen(self):
        with mock.patch("facet.gateway.core.urlopen") as fake:
            fake.return_value = "resp"
            self.assertEqual(open_with_proxy("req", 5), "resp")
        fake.assert_called_once_with("req", timeout=5)

    def test_proxy_builds_opener(self):
        captured = {}

        class FakeOpener:
            def open(self, req, timeout):
                captured["timeout"] = timeout
                return "resp"

        def fake_build_opener(handler):
            captured["proxies"] = handler.proxies
            return FakeOpener()

        with mock.patch("facet.gateway.core.build_opener", fake_build_opener):
            self.assertEqual(
                open_with_proxy("req", 7, "http://127.0.0.1:7890"), "resp")
        self.assertEqual(captured["timeout"], 7)
        self.assertEqual(
            captured["proxies"],
            {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"})


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
        self.assertEqual([c.name for c, _ in candidates], ["plan-a"])
        self.assertEqual(candidates[0][1], "wire-a")
        # An unknown model is scheduled internally instead of being rejected.
        candidates, _ = self.store.pick(OPENAI, "default", "gpt-5.1-codex", None, self.now)
        self.assertEqual([c.name for c, _ in candidates], ["plan-a", "plan-b", "free"])
        self.assertEqual(candidates[0][1], "model-a")
        # A cooling exact channel must not break the calling tool.
        self.store.states["plan-a"].mark_cooldown(3600, "quota", self.now)
        candidates, _ = self.store.pick(OPENAI, "default", "task-model", None, self.now)
        self.assertEqual([c.name for c, _ in candidates], ["plan-b", "free"])


class CrossProtocolSkipTests(unittest.TestCase):
    """Routes only dispatch channels speaking the route's own protocol.

    Locks in actual scheduling behavior: parse_config accepts an explicit
    cross-protocol channel name inside a route, but Router.pick skips it at
    scheduling time instead of converting the payload at the channel
    boundary — so one wire dialect flows through a route end to end.
    """

    def setUp(self):
        data = json.loads(json.dumps(BASE))
        # claude (anthropic, priority 5) sits inside the openai route;
        # plan-a (openai) sits inside the anthropic route.
        data["routes"][0]["channels"] = ["plan-a", "claude", "plan-b", "free"]
        data["routes"][2]["channels"] = ["claude", "plan-a"]
        self.cfg = load_config(write_config(data))
        self.store = StateStore(self.cfg)

    def test_openai_route_skips_anthropic_channel(self):
        candidates, _ = self.store.pick(OPENAI, "default", "auto", None, time.time())
        self.assertEqual([c.name for c, _ in candidates], ["plan-a", "plan-b", "free"])

    def test_anthropic_route_skips_openai_channel(self):
        candidates, _ = self.store.pick("anthropic", "default", "auto", None, time.time())
        self.assertEqual([c.name for c, _ in candidates], ["claude"])


class ModelLevelCooldownTests(unittest.TestCase):
    """A model's allowance is not the channel's: its siblings stay usable."""

    def setUp(self):
        self.cfg = load_config(write_config(BASE))
        self.store = StateStore(self.cfg)
        self.now = time.time()

    def test_model_cooldown_keeps_the_channel_open(self):
        state = self.store.states["plan-a"]
        state.mark_cooldown(3600, "http_429", self.now,
                            per_model=True, wire="wire-a")
        ok, _ = state.available(self.now, self.cfg.channel("plan-a").rate)
        self.assertTrue(ok)
        self.assertFalse(state.model_available(self.now, "wire-a"))
        self.assertTrue(state.model_available(self.now, "model-a"))
        snap = self.store.snapshot(self.now)["channels"]["plan-a"]
        self.assertFalse(snap["cooling"])
        self.assertEqual(snap["model_cooldowns"], {"wire-a": 3600.0})

    def test_route_falls_through_to_the_next_channel(self):
        # facet-cheap's chain starts at 'free'; only free's model is out of
        # allowance, so the next channel in the chain must take over.
        self.store.states["free"].mark_cooldown(
            3600, "http_429", self.now, per_model=True, wire="model-free")
        candidates, _ = self.store.pick(OPENAI, "cheap", "auto", None, self.now)
        self.assertEqual([c.name for c, _ in candidates], ["plan-b"])

    def test_exact_model_cooling_degrades_to_schedule(self):
        self.store.states["plan-a"].mark_cooldown(
            3600, "http_404_not_found", self.now, per_model=True, wire="wire-a")
        candidates, _ = self.store.pick(OPENAI, "default", "task-model", None, self.now)
        self.assertEqual([c.name for c, _ in candidates], ["plan-b", "free"])

    def test_success_clears_only_the_served_model(self):
        state = self.store.states["plan-a"]
        state.mark_cooldown(3600, "http_429", self.now,
                            per_model=True, wire="wire-a")
        state.mark_cooldown(3600, "http_429", self.now,
                            per_model=True, wire="model-a")
        state.mark_success(10, self.now, "wire-a")
        self.assertTrue(state.model_available(self.now, "wire-a"))
        self.assertFalse(state.model_available(self.now, "model-a"))


class WeightedOrderTests(unittest.TestCase):
    """lb_strategy="weighted": score-ranked candidates, chain order for ties."""

    def setUp(self):
        data = {**BASE, "defaults": {"lb_strategy": "weighted"}}
        self.cfg = load_config(write_config(data))
        self.store = StateStore(self.cfg)
        self.now = time.time()

    def candidates(self, route="default", model="auto", session=None):
        pairs, _ = self.store.pick(OPENAI, route, model, session, self.now)
        return [c.name for c, _ in pairs]

    def test_idle_channels_keep_chain_order(self):
        # Zero history everywhere: identical ranks, stable sort keeps the
        # configured order — the prior never demotes an unknown channel.
        self.assertEqual(self.candidates(), ["plan-a", "plan-b", "free"])

    def test_consecutive_errors_sink_a_channel(self):
        self.store.states["plan-a"].consecutive_errors = 3
        self.assertEqual(self.candidates(), ["plan-b", "free", "plan-a"])

    def test_high_latency_sinks_a_channel(self):
        self.store.states["plan-b"].record_latency(9.0)
        self.assertEqual(self.candidates(), ["plan-a", "free", "plan-b"])

    def test_expired_transient_failure_still_sinks_a_channel(self):
        # Cooldown long gone, but failed_requests + consecutive_errors keep
        # the pair ranked below its healthy siblings.
        self.store.states["plan-a"].mark_cooldown(30, "transient_http_502",
                                                  self.now - 60)
        self.assertEqual(self.candidates(), ["plan-b", "free", "plan-a"])

    def test_route_level_lb_overrides_the_default(self):
        routes = [dict(r) for r in BASE["routes"]]
        routes[1]["lb"] = "order"  # the cheap route
        data = {**BASE, "defaults": {"lb_strategy": "weighted"}, "routes": routes}
        store = StateStore(load_config(write_config(data)))
        store.states["plan-b"].record_latency(9.0)
        pairs, _ = store.pick(OPENAI, "cheap", "auto", None, self.now)
        self.assertEqual([c.name for c, _ in pairs], ["free", "plan-b"])

    def test_sticky_session_still_pinned_to_front(self):
        self.store.bind_session("s1", "plan-b", self.now)
        self.assertEqual(self.candidates(session="s1")[0], "plan-b")

    def test_invalid_strategy_is_rejected(self):
        with self.assertRaises(ValueError):
            load_config(write_config({**BASE, "defaults": {"lb_strategy": "random"}}))


class HealthProberTests(unittest.TestCase):
    """Background probes lift transient cooldowns; window-bound ones stay put."""

    def setUp(self):
        data = {**BASE, "defaults": {"lb_strategy": "weighted",
                                     "probe_interval_seconds": 30}}
        self.cfg = load_config(write_config(data))
        self.app = App(self.cfg)
        self.now = time.time()
        self.seen: list[str] = []
        self.results: dict[str, bool] = {}

    def prober(self, interval: float = 5.0) -> HealthProber:
        def probe(channel):
            self.seen.append(channel.name)
            return self.results.get(channel.name, True)
        return HealthProber(self.app, interval, probe_fn=probe)

    def test_transient_cooldown_is_cleared_by_probe(self):
        self.app.store.states["plan-a"].mark_cooldown(120, "transient_http_503", self.now)
        self.prober()._cycle()
        self.assertEqual(self.seen, ["plan-a"])
        state = self.app.store.states["plan-a"]
        self.assertEqual(state.cooldown_until, 0.0)
        self.assertEqual(state.reason, "")
        self.assertFalse(state.half_open)

    def test_quota_and_window_cooldowns_are_never_probed(self):
        self.app.store.states["plan-a"].mark_cooldown(3600, "quota", self.now)
        self.app.store.states["plan-b"].mark_cooldown(7200, "plan_default_exhausted", self.now)
        self.app.store.states["free"].mark_cooldown(3600, "daily_quota_reset", self.now)
        self.prober()._cycle()
        self.assertEqual(self.seen, [])

    def test_long_freeze_and_half_open_are_left_to_the_timer(self):
        self.app.store.states["plan-a"].mark_cooldown(900, "transient_http_503", self.now)
        self.app.store.states["plan-b"].mark_cooldown(120, "transient_http_503",
                                                      self.now, half_open=True)
        self.prober()._cycle()
        self.assertEqual(self.seen, [])

    def test_failed_probes_back_off_exponentially(self):
        self.results["plan-a"] = False
        self.app.store.states["plan-a"].mark_cooldown(120, "transient_http_503", self.now)
        prober = self.prober()
        prober._cycle()
        self.assertEqual(self.seen, ["plan-a"])
        self.assertGreater(prober._deferred["plan-a"], time.time() + 20)
        prober._deferred["plan-a"] = time.time() - 1  # expire the backoff
        prober._cycle()
        self.assertEqual(self.seen.count("plan-a"), 2)
        self.assertGreater(prober._deferred["plan-a"], time.time() + 50)

    def test_success_resets_backoff_state(self):
        self.results["plan-a"] = False
        self.app.store.states["plan-a"].mark_cooldown(120, "transient_http_503", self.now)
        prober = self.prober()
        prober._cycle()
        prober._deferred["plan-a"] = time.time() - 1
        self.results["plan-a"] = True
        prober._cycle()
        self.assertEqual(self.app.store.states["plan-a"].cooldown_until, 0.0)
        self.assertNotIn("plan-a", prober._deferred)
        self.assertNotIn("plan-a", prober._attempts)


class ModelScopedFailureTests(unittest.TestCase):
    """HTTP failures: a model allowance cools that model, billing the channel."""

    def setUp(self):
        self.app = App(load_config(write_config(BASE)))
        self.channel = self.app.config.channel("plan-a")

    def forward(self, status, body, headers=None):
        err = HTTPError(self.channel.base_url + "/v1/chat/completions", status,
                        "boom", headers or {}, io.BytesIO(body.encode()))
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=err):
            return self.app._forward(None, OPENAI, self.channel, "wire-a",
                                     {"messages": []}, 0, False)

    def test_quota_429_cools_only_that_model(self):
        status, _, _ = self.forward(429, '{"error":"rate limit exceeded"}')
        self.assertEqual(status, 429)
        state = self.app.store.states["plan-a"]
        now = time.time()
        self.assertTrue(state.available(now, self.channel.rate)[0])
        self.assertFalse(state.model_available(now, "wire-a"))
        self.assertTrue(state.model_available(now, "model-a"))

    def test_account_out_of_credit_cools_the_channel(self):
        self.forward(
            429,
            '{"error":{"type":"insufficient_quota","message":"You exceeded your current quota"}}')
        state = self.app.store.states["plan-a"]
        now = time.time()
        self.assertFalse(state.available(now, self.channel.rate)[0])
        self.assertTrue(state.cooldown_until > now)

    def test_free_quota_403_cools_only_that_model(self):
        # A vendor withdrawing one model's free grant says "Free quota
        # exhausted ... use free tier only" — the body also matches
        # insufficient_quota, but only the model is out, not the account.
        self.forward(
            403,
            '{"error":{"type":"insufficient_quota","message":'
            '"Free quota exhausted, please use free tier only"}}')
        state = self.app.store.states["plan-a"]
        now = time.time()
        self.assertTrue(state.available(now, self.channel.rate)[0])
        self.assertFalse(state.model_available(now, "wire-a"))
        self.assertTrue(state.model_available(now, "model-a"))

    def test_per_model_quota_channel_cools_only_the_model(self):
        # 百炼 / 火山引擎 hand every model its own allowance: a plain
        # insufficient_quota then belongs to that model, not the account.
        data = json.loads(json.dumps(BASE))
        data["channels"][0]["per_model_quota"] = True
        self.app = App(load_config(write_config(data)))
        self.channel = self.app.config.channel("plan-a")
        self.forward(
            403,
            '{"error":{"type":"insufficient_quota","message":"quota exceeded"}}')
        state = self.app.store.states["plan-a"]
        now = time.time()
        self.assertTrue(state.available(now, self.channel.rate)[0])
        self.assertFalse(state.model_available(now, "wire-a"))
        self.assertTrue(state.model_available(now, "model-a"))


class PlanWindowCooldownTests(unittest.TestCase):
    """A 429 on a plan whose vendor window already reads exhausted cools the
    channel until that window resets, not for the fixed five minutes."""

    def setUp(self):
        self.app = App(load_config(write_config(BASE)))
        self.channel = self.app.config.channel("plan-a")
        self.now = time.time()

    def bind(self, plan):
        self.app.subscriptions = [{"plan": "agentplan", "channels": ["plan-a"]}]
        with mock.patch.object(App, "plan_for", return_value=plan):
            err = HTTPError(self.channel.base_url + "/v1/chat/completions", 429,
                            "boom", {}, io.BytesIO(b'{"error":"quota exceeded"}'))
            with mock.patch("facet.gateway.server.open_with_proxy", side_effect=err):
                return self.app._forward(None, OPENAI, self.channel, "wire-a",
                                         {"messages": []}, 0, False)

    def test_exhausted_window_extends_to_reset(self):
        status, reason, seconds = self.bind({
            "kind": "agent",
            "windows": [{"level": "monthly", "quota": 100, "used": 100,
                         "reset_ts": self.now + 40 * 3600}],
        })
        self.assertEqual(status, 429)
        self.assertEqual(reason, "plan_monthly_exhausted")
        self.assertAlmostEqual(seconds, 40 * 3600, delta=5)
        state = self.app.store.states["plan-a"]
        now = time.time()
        self.assertFalse(state.available(now, self.channel.rate)[0])
        self.assertEqual(state.reason, "plan_monthly_exhausted")

    def test_longest_exhausted_window_wins(self):
        status, reason, seconds = self.bind({
            "kind": "agent",
            "windows": [{"level": "five_hour", "quota": 10, "used": 10,
                         "reset_ts": self.now + 3600},
                        {"level": "monthly", "quota": 100, "used": 100,
                         "reset_ts": self.now + 40 * 3600}],
        })
        self.assertEqual(reason, "plan_monthly_exhausted")
        self.assertAlmostEqual(seconds, 40 * 3600, delta=5)

    def test_window_with_room_keeps_model_level_cooldown(self):
        status, reason, seconds = self.bind({
            "kind": "agent",
            "windows": [{"level": "monthly", "quota": 100, "used": 10,
                         "reset_ts": self.now + 40 * 3600}],
        })
        self.assertEqual(reason, "quota")
        self.assertAlmostEqual(seconds, 18000.0, delta=5)
        state = self.app.store.states["plan-a"]
        now = time.time()
        self.assertTrue(state.available(now, self.channel.rate)[0])
        self.assertFalse(state.model_available(now, "wire-a"))

    def test_percent_window_and_stale_reset_ignored(self):
        status, reason, seconds = self.bind({
            "kind": "coding",
            "windows": [{"level": "monthly", "percent": 100.0,
                         "reset_ts": self.now - 5},
                        {"level": "five_hour", "percent": 30.0}],
        })
        self.assertEqual(reason, "quota")
        self.assertAlmostEqual(seconds, 18000.0, delta=5)


class VisionRoutingTests(unittest.TestCase):
    """Image-bearing requests only reach channels that can take them."""

    def setUp(self):
        data = json.loads(json.dumps(BASE))
        data["channels"][0]["vision"] = False                 # plan-a: text-only
        data["channels"][2]["models"]["vision"] = "model-vl"  # free: extra mapping
        data["channels"][2]["vision_model"] = "model-vl"      # free: re-points images
        self.app = App(load_config(write_config(data)))
        self.image_body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}]}

    def route_image(self):
        candidates, _ = self.app.store.pick(OPENAI, "default", "auto", None, time.time())
        candidates = [(channel, vision_wire(channel, wire))
                      for channel, wire in candidates]
        return self.app._apply_vision_filter(candidates)

    def test_image_request_skips_text_only_channel(self):
        candidates, skipped = self.route_image()
        self.assertEqual([c.name for c, _ in candidates], ["plan-b", "free"])
        self.assertEqual(skipped, [{"channel": "plan-a", "wire_model": "model-a",
                                    "reason": "channel_text_only"}])

    def test_vision_model_replaces_auto_mapping(self):
        candidates, _ = self.route_image()
        self.assertEqual([wire for _, wire in candidates], ["model-b", "model-vl"])

    def test_text_request_keeps_full_chain_and_auto_mapping(self):
        # The handler consults the filter only for image-bearing requests;
        # a text request flows through pick untouched, text-only channels
        # included (vision only gates multimodal traffic).
        candidates, _ = self.app.store.pick(OPENAI, "default", "auto", None, time.time())
        self.assertEqual([c.name for c, _ in candidates],
                         ["plan-a", "plan-b", "free"])
        self.assertEqual([wire for _, wire in candidates],
                         ["model-a", "model-b", "model-free"])
        self.assertFalse(channel_takes_images(
            self.app.config.channel("plan-a"), "model-a"))
        self.assertTrue(channel_takes_images(
            self.app.config.channel("free"), "model-free"))

    def test_vision_list_restricts_by_wire_model(self):
        data = json.loads(json.dumps(BASE))
        data["channels"][0]["vision"] = ["wire-a"]
        cfg = load_config(write_config(data))
        channel = cfg.channel("plan-a")
        self.assertTrue(channel_takes_images(channel, "wire-a"))
        self.assertFalse(channel_takes_images(channel, "other"))

    def test_anthropic_blocks_detected_as_vision(self):
        self.assertTrue(body_has_vision({"messages": [
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "data": "AAAA"}}]}]}))
        self.assertTrue(body_has_vision({"messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "read this"},
                {"type": "document", "source": {}}]}]}))
        self.assertFalse(body_has_vision({"messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]}]}))
        self.assertFalse(body_has_vision({"messages": [
            {"role": "user", "content": "plain string"}]}))

    def test_vision_config_validation(self):
        data = json.loads(json.dumps(BASE))
        data["channels"][0]["vision"] = "yes"
        with self.assertRaisesRegex(ValueError, "vision"):
            load_config(write_config(data))
        data["channels"][0]["vision"] = []
        with self.assertRaisesRegex(ValueError, "vision list"):
            load_config(write_config(data))
        data["channels"][0]["vision"] = False
        data["channels"][0]["vision_model"] = "vl"
        with self.assertRaisesRegex(ValueError, "conflicts"):
            load_config(write_config(data))


class EmbeddingRoutingTests(unittest.TestCase):
    """Embeddings use the same routing pipeline but a different endpoint."""

    def setUp(self):
        data = json.loads(json.dumps(BASE))
        # Add an embedding-specific channel and route
        data["channels"].append({
            "name": "emb-ch1",
            "protocol": "openai",
            "base_url": "http://127.0.0.1:9005",
            "models": {
                "auto": "text-embedding-3-small",
                "text-embedding-3-small": "text-embedding-3-small",
                "text-embedding-3-large": "text-embedding-3-large",
            },
            "priority": 5,
            "tier": "free",
            "free_quota": {"requests": 10, "period": "total"},
        })
        data["channels"].append({
            "name": "emb-ch2",
            "protocol": "openai",
            "base_url": "http://127.0.0.1:9006",
            "models": {"auto": "text-embedding-ada-002", "*": "*", "text-embedding-3-large": "text-embedding-3-large", "text-embedding-ada-002": "text-embedding-ada-002"},
            "priority": 10,
        })
        data["routes"].append({
            "name": "embedding",
            "protocol": "openai",
            "channels": ["emb-ch1", "emb-ch2"],
        })
        # Need usage_file for UsageRecorder
        import tempfile
        self.usage_file = tempfile.NamedTemporaryFile(suffix='.json', delete=False).name
        self.app = App(load_config(write_config(data)), usage_file=self.usage_file)
        self.embedding_body = {"model": "facet-embedding", "input": "test"}

    def tearDown(self):
        try:
            os.unlink(self.usage_file)
        except OSError:
            pass

    def test_branded_embedding_routes_to_embedding_chain(self):
        """facet-embedding should pick the embedding route."""
        candidates, route = self.app.store.pick(OPENAI, "embedding", "auto", None, time.time())
        self.assertEqual(route.name, "embedding")
        self.assertEqual([c.name for c, _ in candidates], ["emb-ch1", "emb-ch2"])
        self.assertEqual([wire for _, wire in candidates],
                         ["text-embedding-3-small", "text-embedding-ada-002"])

    def test_exact_embedding_model_on_embedding_route(self):
        """Exact model name routes correctly on embedding route."""
        candidates, _ = self.app.store.pick(OPENAI, "embedding", "text-embedding-3-large", None, time.time())
        self.assertEqual([c.name for c, _ in candidates], ["emb-ch1", "emb-ch2"])
        self.assertEqual(candidates[0][1], "text-embedding-3-large")

    def test_embedding_failover_on_model_cooldown(self):
        """Model-level cooldown on emb-ch1 should fall through to emb-ch2."""
        self.app.store.states["emb-ch1"].mark_cooldown(
            3600, "http_429", time.time(), per_model=True, wire="text-embedding-3-small"
        )
        candidates, _ = self.app.store.pick(OPENAI, "embedding", "auto", None, time.time())
        self.assertEqual([c.name for c, _ in candidates], ["emb-ch2"])
        self.assertEqual(candidates[0][1], "text-embedding-ada-002")

    def test_embedding_free_quota_exhaustion_blocks_model(self):
        """Free quota exhaustion on an embedding model blocks it from routing."""
        # Record 10 successful requests to exhaust the free quota (configured in setUp)
        for _ in range(10):
            self.app.record_usage("emb-ch1", True, tokens=0, model="text-embedding-3-small")
        # Check via App's free_quota_exhausted method
        channel = self.app.config.channel("emb-ch1")
        self.assertTrue(self.app.free_quota_exhausted(channel, "text-embedding-3-small"))
        # Verify the filter skips it
        candidates, _ = self.app.store.pick(OPENAI, "embedding", "auto", None, time.time())
        decision = type("Decision", (), {"exposed": "facet-embedding", "hint": "embedding"})()
        kept, skipped = self.app._apply_free_quota_filter(candidates, decision)
        self.assertEqual([c.name for c, _ in kept], ["emb-ch2"])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "free_quota_exhausted")

    def test_embedding_channel_not_used_for_chat(self):
        """Embedding channels don't appear in default chat route."""
        candidates, route = self.app.store.pick(OPENAI, "default", "auto", None, time.time())
        self.assertEqual(route.name, "default")
        self.assertNotIn("emb-ch1", [c.name for c, _ in candidates])
        self.assertNotIn("emb-ch2", [c.name for c, _ in candidates])

    def test_embedding_model_list_includes_branded(self):
        """Model list includes embedding models from channels and branded alias."""
        model_list = self.app.model_list()
        ids = [m["id"] for m in model_list["data"]]
        # Models explicitly declared in channels
        self.assertIn("text-embedding-3-small", ids)
        self.assertIn("text-embedding-3-large", ids)
        self.assertIn("text-embedding-ada-002", ids)
        # Branded alias
        self.assertIn("facet-embedding", ids)


class _FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler in auth checks."""

    def __init__(self, headers=None):
        self.headers = headers or {}


class KeyTests(unittest.TestCase):
    """Member keys: storage, the gateway auth matrix and per-member usage."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.keys_file = self.tmp / "keys.json"
        self.usage_file = self.tmp / "usage.json"

    def app(self, api_key="master-key"):
        data = json.loads(json.dumps(BASE))
        if api_key:
            data["listen"]["api_key"] = api_key
        return App(load_config(write_config(data)),
                   usage_file=self.usage_file, keys_file=self.keys_file)

    def check(self, app, key):
        h = _FakeHandler({"Authorization": "Bearer " + key} if key else {})
        return app._authorized(h), getattr(h, "user_key", None)

    def test_key_store_format_and_persistence(self):
        app = self.app()
        record = app.keys.create("alice")
        self.assertTrue(record["key"].startswith("sk-pr-"))
        self.assertEqual(len(record["key"]), len("sk-pr-") + 32)
        # Same name twice is fine: the key is the identity, the name a label.
        second = app.keys.create("alice")
        self.assertNotEqual(record["key"], second["key"])
        reloaded = KeyStore(self.keys_file)
        self.assertEqual(reloaded.match(record["key"]), "alice")
        self.assertEqual(len(reloaded.list()), 2)

    def test_master_only_when_no_members(self):
        app = self.app()
        self.assertEqual(app.keys.records, [])
        self.assertEqual(self.check(app, "master-key"), (True, ""))
        self.assertEqual(self.check(app, "wrong"), (False, ""))
        self.assertEqual(self.check(app, ""), (False, ""))

    def test_auth_compares_non_ascii_keys_without_crashing(self):
        # hmac.compare_digest raises TypeError on non-ASCII str inputs;
        # the gateway must encode both sides before comparing.
        app = self.app(api_key="主密钥-甲")
        self.assertEqual(self.check(app, "主密钥-甲"), (True, ""))
        self.assertEqual(self.check(app, "主密钥-乙"), (False, ""))
        self.assertEqual(self.check(app, "master-key"), (False, ""))

    def test_member_key_is_a_valid_credential_and_attributes(self):
        app = self.app()
        record = app.keys.create("alice")
        self.assertEqual(self.check(app, record["key"]), (True, "alice"))
        self.assertEqual(self.check(app, "master-key"), (True, ""))

    def test_revoked_key_fails_and_master_survives(self):
        app = self.app()
        record = app.keys.create("bob")
        self.assertTrue(app.keys.revoke(record["key"]))
        self.assertFalse(app.keys.revoke(record["key"]))  # already revoked
        self.assertEqual(self.check(app, record["key"])[0], False)
        self.assertEqual(self.check(app, "master-key")[0], True)

    def test_open_gateway_allows_keyless_but_attributes_members(self):
        app = self.app(api_key=None)
        record = app.keys.create("carol")
        self.assertEqual(self.check(app, record["key"]), (True, "carol"))
        self.assertEqual(self.check(app, ""), (True, ""))

    def test_usage_is_attributed_per_member(self):
        app = self.app()
        record = app.keys.create("alice")
        ok, user = self.check(app, record["key"])
        self.assertTrue(ok)
        h = _FakeHandler({"Authorization": "Bearer " + record["key"]})
        h.user_key = user
        app.record_usage("plan-a", True, 100, model="wire-a", h=h)
        app.record_usage("plan-a", False, 0, model="wire-a", h=None)  # anonymous
        stats = app.usage.user_stats(30)
        self.assertEqual([(u["name"], u["requests"], u["success"], u["failed"], u["tokens"])
                          for u in stats["users"]],
                         [("alice", 1, 1, 0, 100)])

    def test_keyless_record_lands_nowhere_in_users(self):
        app = self.app()
        app.record_usage("plan-a", True, 10, model="wire-a", h=None)
        stats = app.usage.user_stats(30)
        self.assertEqual(stats["users"], [])

    # ---------------------------------------------------------- member caps
    def test_normalize_limits_accepts_zero_and_rejects_bad_values(self):
        self.assertEqual(normalize_limits(None), {})
        self.assertEqual(normalize_limits(""), {})
        self.assertEqual(normalize_limits({"rpm": 60, "daily_tokens": 0}), {"rpm": 60})
        self.assertEqual(normalize_limits({"daily_tokens": 1000.0}), {"daily_tokens": 1000})
        for bad in (42, {"rpm": -1}, {"rpm": "many"}, {"daily_tokens": 0.5},
                    {"monthly_requests": True}):
            with self.assertRaises(ValueError):
                normalize_limits(bad)

    def test_create_and_set_limits_persist(self):
        store = KeyStore(self.keys_file)
        record = store.create("alice", limits={"rpm": 30, "daily_tokens": 1000})
        self.assertEqual(record["limits"], {"rpm": 30, "daily_tokens": 1000})
        self.assertIsNone(store.set_limits("sk-pr-" + "0" * 32, {"rpm": 5}))
        updated = store.set_limits(record["key"], {"rpm": 5, "monthly_tokens": 9})
        self.assertEqual(updated["limits"], {"rpm": 5, "monthly_tokens": 9})
        self.assertNotIn("limits", store.set_limits(record["key"], {}))
        reloaded = KeyStore(self.keys_file)
        self.assertEqual(reloaded.lookup(record["key"])["name"], "alice")
        self.assertEqual(reloaded.match(record["key"]), "alice")

    def test_lookup_respects_enabled_and_prefix(self):
        store = KeyStore(self.keys_file)
        record = store.create("bob")
        self.assertEqual(store.lookup(record["key"])["name"], "bob")
        self.assertIsNone(store.lookup("master-key"))
        store.revoke(record["key"])
        self.assertIsNone(store.lookup(record["key"]))
        store.enable(record["key"])
        self.assertIsNotNone(store.lookup(record["key"]))

    def test_member_without_limits_is_never_limited(self):
        app = self.app()
        record = app.keys.create("alice")
        h = _FakeHandler({"Authorization": "Bearer " + record["key"]})
        self.assertTrue(app._authorized(h))
        for _ in range(5):
            app.record_usage("plan-a", True, 1000, h=h)
        self.assertIsNone(app._member_limited(h.user_key, h.member_record))
        # Keyless callers (and a missing record) are untouched as well.
        self.assertIsNone(app._member_limited("", h.member_record))
        self.assertIsNone(app._member_limited("ghost", None))

    def test_daily_token_quota_rejects_member(self):
        app = self.app()
        record = app.keys.create("alice", limits={"daily_tokens": 100})
        h = _FakeHandler({"Authorization": "Bearer " + record["key"]})
        self.assertTrue(app._authorized(h))
        app.record_usage("plan-a", True, 60, h=h)
        self.assertIsNone(app._member_limited(h.user_key, h.member_record))
        app.record_usage("plan-a", True, 40, h=h)
        status, message = app._member_limited(h.user_key, h.member_record)
        self.assertEqual(status, 429)
        self.assertIn("今日 token 配额已用完", message)
        self.assertIn("100 / 上限 100", message)

    def test_daily_request_quota_rejects_member(self):
        app = self.app()
        record = app.keys.create("alice", limits={"daily_requests": 1})
        h = _FakeHandler({"Authorization": "Bearer " + record["key"]})
        self.assertTrue(app._authorized(h))
        app.record_usage("plan-a", True, 0, h=h)
        status, message = app._member_limited(h.user_key, h.member_record)
        self.assertEqual(status, 429)
        self.assertIn("今日请求数配额已用完", message)

    def test_monthly_request_quota_rejects_member(self):
        app = self.app()
        record = app.keys.create("bob", limits={"monthly_requests": 2})
        h = _FakeHandler({"Authorization": "Bearer " + record["key"]})
        self.assertTrue(app._authorized(h))
        for _ in range(2):
            app.record_usage("plan-a", True, 0, h=h)
        status, message = app._member_limited(h.user_key, h.member_record)
        self.assertEqual(status, 429)
        self.assertIn("本月请求数配额已用完", message)

    def test_rpm_cap_uses_sliding_window(self):
        app = self.app()
        record = app.keys.create("carol", limits={"rpm": 2})
        h = _FakeHandler({"Authorization": "Bearer " + record["key"]})
        self.assertTrue(app._authorized(h))
        self.assertIsNone(app._member_limited(h.user_key, h.member_record))
        self.assertIsNone(app._member_limited(h.user_key, h.member_record))
        status, message = app._member_limited(h.user_key, h.member_record)
        self.assertEqual(status, 429)
        self.assertIn("每分钟 2 次请求上限", message)
        # A rejected call eats no slot, so expiring the window unblocks.
        self.assertEqual(len(app.member_rpm["carol"]), 2)
        app.member_rpm["carol"] = [time.time() - 120]
        self.assertIsNone(app._member_limited(h.user_key, h.member_record))

    def test_rpm_reservation_is_atomic_under_concurrency(self):
        # A thundering herd must not squeeze extra calls past the cap: the
        # check and the window append happen in one critical section.
        app = self.app()
        record = app.keys.create("dana", limits={"rpm": 5})
        h = _FakeHandler({"Authorization": "Bearer " + record["key"]})
        self.assertTrue(app._authorized(h))
        ready = threading.Barrier(40)
        results: list = []
        lock = threading.Lock()

        def probe():
            ready.wait()
            verdict = app._member_limited(h.user_key, h.member_record)
            with lock:
                results.append(verdict)

        workers = [threading.Thread(target=probe) for _ in range(40)]
        for t in workers:
            t.start()
        for t in workers:
            t.join()
        passed = [r for r in results if r is None]
        self.assertEqual(len(passed), 5)
        self.assertEqual(len(app.member_rpm["dana"]), 5)

    def test_quota_rejection_releases_the_rpm_reservation(self):
        # A call blocked by a usage quota never reaches inference; keeping its
        # rpm slot would let repeated rejects lock the member out entirely.
        app = self.app()
        record = app.keys.create("erin", limits={"rpm": 3, "daily_requests": 1})
        h = _FakeHandler({"Authorization": "Bearer " + record["key"]})
        self.assertTrue(app._authorized(h))
        app.record_usage("plan-a", True, 0, h=h)  # daily quota spent up front
        for _ in range(4):
            status, message = app._member_limited(h.user_key, h.member_record)
            self.assertEqual(status, 429)
            self.assertIn("今日请求数配额已用完", message)
        self.assertEqual(len(app.member_rpm["erin"]), 0)

    def test_member_usage_tracks_channels_and_totals(self):
        app = self.app()
        h = _FakeHandler({})
        h.user_key = "alice"
        app.record_usage("plan-a", True, 100, h=h)
        app.record_usage("plan-b", True, 30, h=h)
        totals = app.usage.member_totals("alice", date.today())
        self.assertEqual(totals["requests"], 2)
        self.assertEqual(totals["tokens"], 130)
        self.assertEqual(totals["channels"]["plan-a"], {"requests": 1, "tokens": 100})
        row = app.usage.user_stats(7)["users"][0]
        self.assertEqual(row["channels"]["plan-b"]["requests"], 1)
        self.assertEqual(row["today"]["requests"], 2)
        self.assertFalse(row["spike"])

    def test_user_stats_flags_spike(self):
        app = self.app()
        yesterday = date.today() - timedelta(days=1)
        app.usage.record("plan-a", True, 0, user="alice")
        for _ in range(3):
            app.usage.record("plan-a", True, 0, on=yesterday, user="alice")
        for _ in range(10):
            app.usage.record("plan-a", True, 0, user="bob")
        for _ in range(3):
            app.usage.record("plan-a", True, 0, on=yesterday, user="bob")
        rows = {row["name"]: row for row in app.usage.user_stats(7)["users"]}
        self.assertFalse(rows["alice"]["spike"])
        self.assertTrue(rows["bob"]["spike"])

    def test_member_usage_merges_limits_and_state(self):
        app = self.app()
        record = app.keys.create("alice", limits={"daily_requests": 5})
        h = _FakeHandler({})
        h.user_key = "alice"
        app.record_usage("plan-a", True, 0, h=h)
        payload = app.member_usage(7)
        row = payload["users"][0]
        self.assertEqual(row["limits"], {"daily_requests": 5})
        self.assertTrue(row["enabled"])
        app.keys.pause(record["key"])
        self.assertFalse(app.member_usage(7)["users"][0]["enabled"])


class _StreamResp:
    """Fake upstream response yielding prepared chunks and tracking close()."""

    def __init__(self, chunks, log=None):
        self._chunks = list(chunks)
        self.headers = {"Content-Type": "text/event-stream"}
        self.closed = 0
        self.log = [] if log is None else log

    def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed += 1
        self.log.append("resp.close")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _DropHandler(_FakeHandler):
    """Handler whose client connection dies after `fail_after` wfile writes."""

    def __init__(self, log=None, fail_after=1):
        super().__init__()
        self.log = [] if log is None else log
        self.fail_after = fail_after
        self.writes = 0
        self.sent = []
        self.wfile = self

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def write(self, data):
        self.writes += 1
        if self.writes > self.fail_after:
            self.log.append("disconnect")
            raise BrokenPipeError("client hung up")
        self.sent.append(data)

    def flush(self):
        pass


class _TailAdapter:
    """Streaming adapter whose finish() emits a small tail event."""

    def feed(self, chunk):
        return chunk

    def finish(self, data=None):
        return b"tail"


SSE_DELTA = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
SSE_USAGE_7 = (b'data: {"choices":[],"usage":{"prompt_tokens":5,'
               b'"completion_tokens":2,"total_tokens":7}}\n\n')
SSE_USAGE_99 = (b'data: {"choices":[],"usage":{"prompt_tokens":50,'
                b'"completion_tokens":49,"total_tokens":99}}\n\n')


class StreamCancelTests(unittest.TestCase):
    """P0-1: a client that hangs up mid-stream must drop the upstream
    connection immediately (stop paying for a stream nobody reads) and still
    record exactly the tokens that were relayed before the disconnect."""

    def setUp(self):
        self.app = App(load_config(write_config(BASE)))
        self.channel = self.app.config.channel("plan-a")

    def test_disconnect_closes_upstream_before_recording_usage(self):
        log = []
        resp = _StreamResp([SSE_USAGE_7, SSE_USAGE_99], log)  # 2nd never relays
        h = _DropHandler(log, fail_after=1)
        with mock.patch.object(self.app, "record_usage") as rec:
            rec.side_effect = lambda *a, **k: log.append("usage")
            result = self.app._relay(h, resp, OPENAI, self.channel, "wire-a",
                                     0, False, True)
        self.assertIsNone(result)        # no exception escaped _relay
        self.assertEqual(resp.closed, 1)  # upstream released at the disconnect
        # close happens inside the except branch, before usage is recorded
        self.assertEqual(log, ["disconnect", "resp.close", "usage"])
        rec.assert_called_once()
        args, kwargs = rec.call_args
        self.assertEqual(args, ("plan-a", True, 7))  # only the relayed chunk
        self.assertEqual(kwargs["input_tokens"], 5)

    def test_tail_write_disconnect_still_records_usage(self):
        # The adapter tail write used to sit outside the try block: a second
        # disconnect there escaped into _forward's bare except, which marked
        # success(0) and skipped record_usage entirely.
        resp = _StreamResp([SSE_DELTA, SSE_USAGE_7])
        h = _DropHandler(fail_after=2)   # chunks relay, the tail write dies
        with mock.patch("facet.gateway.server.open_with_proxy", return_value=resp):
            with mock.patch.object(self.app, "record_usage") as rec:
                result = self.app._forward(
                    h, OPENAI, self.channel, "wire-a",
                    {"messages": [], "stream": True}, 0, False,
                    delivery={"adapter": _TailAdapter()})
        self.assertIsNone(result)
        rec.assert_called_once()
        self.assertEqual(rec.call_args[0], ("plan-a", True, 7))
        self.assertEqual(resp.closed, 1)  # released via _forward's with-block

    def test_full_stream_records_all_relayed_usage(self):
        resp = _StreamResp([SSE_DELTA, SSE_USAGE_7])
        h = _DropHandler(fail_after=99)   # nobody hangs up
        with mock.patch("facet.gateway.server.open_with_proxy", return_value=resp):
            with mock.patch.object(self.app, "record_usage") as rec:
                result = self.app._forward(
                    h, OPENAI, self.channel, "wire-a",
                    {"messages": [], "stream": True}, 0, False)
        self.assertIsNone(result)
        self.assertEqual(h.writes, 2)
        self.assertEqual(h.sent, [SSE_DELTA, SSE_USAGE_7])
        self.assertEqual(resp.closed, 1)
        rec.assert_called_once()
        self.assertEqual(rec.call_args[0], ("plan-a", True, 7))


class RuntimeAlertConditionTests(unittest.TestCase):
    """P1-2: the three runtime alarm families evaluated off live state."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.usage_file = Path(self.dir.name) / "usage.json"

    def tearDown(self):
        self.dir.cleanup()

    def app(self, data=None, notifications=False):
        kwargs = {"usage_file": self.usage_file}
        if notifications:
            kwargs["notifications_file"] = Path(self.dir.name) / "notifications.json"
        return App(load_config(write_config(data or BASE)), **kwargs)

    def test_route_chain_all_cooling_alerts(self):
        data = json.loads(json.dumps(BASE))
        for channel in data["channels"]:
            channel["api_key"] = "sk-test"
        app = self.app(data, notifications=True)
        now = time.time()
        for name in ("free", "plan-b"):  # the whole "cheap" chain
            app.store.states[name].mark_cooldown(3600, "rate limit", now)
        alerts = app.runtime_alerts()
        keys = {a["key"] for a in alerts}
        self.assertIn("route:openai:cheap", keys)
        # plan-a is still healthy, so the default chains stay silent
        self.assertNotIn("route:openai:default", keys)
        self.assertNotIn("route:anthropic:default", keys)
        # the notifier is wired to the same conditions
        self.assertEqual(app.notifier.get_health(), alerts)

    def test_route_alert_ignores_config_gaps(self):
        # Channels without api_key are config gaps: cooling one of them plus
        # the keyless rest must not raise a "chain down" alarm.
        app = self.app()
        now = time.time()
        for name in ("free", "plan-b"):
            app.store.states[name].mark_cooldown(3600, "rate limit", now)
        self.assertNotIn("route:openai:cheap", {a["key"] for a in app.runtime_alerts()})

    def test_probe_failing_alerts_after_repeated_errors(self):
        app = self.app()
        state = app.store.states["plan-a"]
        for _ in range(3):
            state.mark_cooldown(30, "transient", time.time())
        self.assertIn("probe:plan-a", {a["key"] for a in app.runtime_alerts()})
        state.mark_success(0, time.time())
        self.assertNotIn("probe:plan-a", {a["key"] for a in app.runtime_alerts()})

    def test_free_quota_exhausted_alerts(self):
        data = json.loads(json.dumps(BASE))
        data["channels"][2]["api_key"] = "sk-test"
        data["channels"][2]["tier"] = "free"
        data["channels"][2]["free_quota"] = {"tokens": 10, "period": "total"}
        app = self.app(data)
        app.record_usage("free", True, 100, model="model-free")
        self.assertIn("freequota:free:model-free",
                      {a["key"] for a in app.runtime_alerts()})


if __name__ == "__main__":
    unittest.main()
