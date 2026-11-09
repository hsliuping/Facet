"""Vendor strong-binding validation and the container preset seed."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from planrouter.core import parse_config
from planrouter.providers import MODE_KEYS
from planrouter.seed import build_seed
from planrouter.server import App

REPO_ROOT = Path(__file__).resolve().parents[1]


def legacy_config() -> dict:
    """A config written before vendor bookkeeping: no provider/mode anywhere."""
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "old-ch", "base_url": "https://old.example/v1",
             "api_key": "k", "models": {"auto": "auto-old"}},
            # parse_config demands a non-empty anthropic:default route.
            {"name": "old-claude", "protocol": "anthropic",
             "base_url": "https://old.example/v1", "api_key": "k",
             "models": {"claude-x": "claude-x"}},
        ],
        "routes": [{"name": "default", "protocol": "openai",
                    "channels": ["old-ch"]},
                   {"name": "default", "protocol": "anthropic",
                    "channels": ["old-claude"]}],
    }


def pollinations_channel(name: str = "pollinations") -> dict:
    # The pair used by the shipped seed; guaranteed present in the registry
    # (the seed-binding test below would fail otherwise).
    return {"name": name, "tier": "free",
            "provider": "pollinations", "mode": "payg",
            "base_url": "https://text.pollinations.ai/openai",
            "api_key": "k", "models": {"auto": "openai"}}


class BindingTestBase(unittest.TestCase):
    """App over a real file, so self.raw mirrors an on-disk legacy config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "router.json"
        self.raw = legacy_config()
        self.path.write_text(json.dumps(self.raw), encoding="utf-8")
        self.app = App(parse_config(self.raw), config_path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, channels) -> None:
        """Save a whole config whose routes mirror `channels` by protocol."""
        # Base the submitted payload on the app's *current* raw config: a
        # console tab always posts the whole document it holds, which is the
        # server's state at the time it was opened (see stale-tab below).
        raw = dict(self.app.raw)
        # parse_config demands a non-empty anthropic:default route, so every
        # save keeps whichever anthropic channel the current config carried.
        anthro = next(c for c in self.app.raw["channels"]
                      if c.get("protocol") == "anthropic")
        raw["channels"] = list(channels) + [anthro]
        groups: dict[str, list[str]] = {"openai": [], "anthropic": []}
        for channel in raw["channels"]:
            groups.setdefault(channel.get("protocol", "openai"), []).append(
                channel["name"])
        raw["routes"] = [
            {"name": "default", "protocol": protocol, "channels": names}
            for protocol, names in groups.items()]
        self.app.apply_config(raw)

    def unknown_mode_for(self, provider_id: str) -> str:
        entry = next(p for p in self.app.providers if p.get("id") == provider_id)
        return next(k for k in MODE_KEYS if k not in (entry.get("modes") or {}))


class VendorBindingTests(BindingTestBase):
    def test_new_channel_requires_vendor(self):
        with self.assertRaisesRegex(ValueError, "new-ch.*必须选择厂家"):
            self.apply([self.raw["channels"][0],
                        {"name": "new-ch", "base_url": "https://x.example/v1",
                         "api_key": "k", "models": {"auto": "a"}}])

    def test_unknown_provider_rejected(self):
        channel = pollinations_channel()
        channel["provider"] = "no-such-vendor"
        with self.assertRaisesRegex(ValueError, "厂家 no-such-vendor 不存在"):
            self.apply([channel])

    def test_unknown_mode_rejected(self):
        channel = pollinations_channel()
        channel["mode"] = self.unknown_mode_for("pollinations")
        with self.assertRaisesRegex(ValueError, "不支持模式"):
            self.apply([channel])

    def test_one_sided_binding_rejected(self):
        channel = pollinations_channel()
        channel.pop("mode")
        with self.assertRaisesRegex(ValueError, "厂家信息不完整"):
            self.apply([channel])
        channel = pollinations_channel("pollinations-2")
        channel.pop("provider")
        with self.assertRaisesRegex(ValueError, "厂家信息不完整"):
            self.apply([channel])

    def test_valid_binding_saved_and_persisted(self):
        self.apply([self.raw["channels"][0], pollinations_channel()])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        new = next(c for c in saved["channels"] if c["name"] == "pollinations")
        self.assertEqual((new["provider"], new["mode"]), ("pollinations", "payg"))

    def test_legacy_channel_tolerated(self):
        # Pre-existing vendor-less channels keep loading and saving untouched.
        self.apply([self.raw["channels"][0]])  # old-ch only; old-claude rides along

    def test_stale_tab_inherits_vendor(self):
        vendor_raw = {"listen": {"host": "127.0.0.1", "port": 0},
                      "channels": [pollinations_channel(),
                                   {"name": "anthro", "protocol": "anthropic",
                                    "provider": "anthropic", "mode": "payg",
                                    "base_url": "https://x.example/v1",
                                    "api_key": "k", "models": {"c": "c"}}],
                      "routes": [{"name": "default", "protocol": "openai",
                                  "channels": ["pollinations"]},
                                 {"name": "default", "protocol": "anthropic",
                                  "channels": ["anthro"]}]}
        self.path.write_text(json.dumps(vendor_raw), encoding="utf-8")
        self.app = App(parse_config(vendor_raw), config_path=self.path)
        stale = pollinations_channel()
        stale.pop("provider")
        stale.pop("mode")
        self.apply([stale])
        # An editor older than vendor bookkeeping must not erase the binding.
        self.assertEqual(self.app.raw["channels"][0]["provider"], "pollinations")
        self.assertEqual(self.app.raw["channels"][0]["mode"], "payg")


class SeedBindingTests(unittest.TestCase):
    def _strict_app(self, raw: dict) -> App:
        """A file-backed app whose on-disk raw is unreadable: every channel
        counts as new, so _check_vendor_binding runs at full strictness."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "router.json"
        path.write_text("", encoding="utf-8")  # json.loads fails -> raw {}
        return App(parse_config(raw), config_path=path)

    def test_seed_channels_pass_strict_binding(self):
        app = self._strict_app(build_seed())
        app._check_vendor_binding(app.config)

    def test_entrypoint_preset_seed_is_valid_and_containerized(self):
        spec = importlib.util.spec_from_file_location(
            "docker_entrypoint", REPO_ROOT / "docker" / "entrypoint.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        seed = module.preset_config()
        self.assertEqual(seed["listen"]["host"], "0.0.0.0")
        self.assertEqual(seed["listen"]["port"], 8787)
        self.assertEqual(seed["listen"]["api_key"], "env:ROUTER_API_KEY")
        self.assertEqual(seed["listen"]["admin_key"], "env:ROUTER_ADMIN_KEY")
        # The containerized listen is non-loopback with env: references, so
        # the variables must exist for parse_config to accept the seed.
        with mock.patch.dict(os.environ,
                             {"ROUTER_API_KEY": "k", "ROUTER_ADMIN_KEY": "k"}):
            app = self._strict_app(seed)
            app._check_vendor_binding(app.config)


if __name__ == "__main__":
    unittest.main()
