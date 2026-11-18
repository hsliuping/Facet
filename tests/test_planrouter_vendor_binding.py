"""Vendor strong-binding validation and the container preset seed."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from facet.gateway.core import parse_config
from facet.gateway.providers import MODE_KEYS
from facet.gateway.seed import build_seed
from facet.gateway.server import App

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


def nvidia_channel(name: str = "nvidia-nim") -> dict:
    # The pair used by the shipped seed; guaranteed present in the registry
    # (the seed-binding test below would fail otherwise).
    return {"name": name, "tier": "free",
            "provider": "nvidia", "mode": "payg",
            "base_url": "https://integrate.api.nvidia.com/v1",
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
        channel = nvidia_channel()
        channel["provider"] = "no-such-vendor"
        with self.assertRaisesRegex(ValueError, "厂家 no-such-vendor 不存在"):
            self.apply([channel])

    def test_unknown_mode_rejected(self):
        channel = nvidia_channel()
        channel["mode"] = self.unknown_mode_for("nvidia")
        with self.assertRaisesRegex(ValueError, "不支持模式"):
            self.apply([channel])

    def test_one_sided_binding_rejected(self):
        channel = nvidia_channel()
        channel.pop("mode")
        with self.assertRaisesRegex(ValueError, "厂家信息不完整"):
            self.apply([channel])
        channel = nvidia_channel("nvidia-2")
        channel.pop("provider")
        with self.assertRaisesRegex(ValueError, "厂家信息不完整"):
            self.apply([channel])

    def test_valid_binding_saved_and_persisted(self):
        self.apply([self.raw["channels"][0], nvidia_channel()])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        new = next(c for c in saved["channels"] if c["name"] == "nvidia-nim")
        self.assertEqual((new["provider"], new["mode"]), ("nvidia", "payg"))

    def test_legacy_channel_tolerated(self):
        # Pre-existing vendor-less channels keep loading and saving untouched.
        self.apply([self.raw["channels"][0]])  # old-ch only; old-claude rides along

    def test_stale_tab_inherits_vendor(self):
        vendor_raw = {"listen": {"host": "127.0.0.1", "port": 0},
                      "channels": [nvidia_channel(),
                                   {"name": "anthro", "protocol": "anthropic",
                                    "provider": "anthropic", "mode": "payg",
                                    "base_url": "https://x.example/v1",
                                    "api_key": "k", "models": {"c": "c"}}],
                      "routes": [{"name": "default", "protocol": "openai",
                                  "channels": ["nvidia-nim"]},
                                 {"name": "default", "protocol": "anthropic",
                                  "channels": ["anthro"]}]}
        self.path.write_text(json.dumps(vendor_raw), encoding="utf-8")
        self.app = App(parse_config(vendor_raw), config_path=self.path)
        stale = nvidia_channel()
        stale.pop("provider")
        stale.pop("mode")
        self.apply([stale])
        # An editor older than vendor bookkeeping must not erase the binding.
        self.assertEqual(self.app.raw["channels"][0]["provider"], "nvidia")
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
        # An explicitly supplied key keeps the env reference, which the gateway
        # resolves from the environment.
        with mock.patch.dict(os.environ,
                             {"ROUTER_API_KEY": "k", "ROUTER_ADMIN_KEY": "k"}):
            seed = module.preset_config()
            self.assertEqual(seed["listen"]["admin_key"], "env:ROUTER_ADMIN_KEY")
            self.assertEqual(seed["listen"]["host"], "0.0.0.0")
            self.assertEqual(seed["listen"]["port"], 8787)
            self.assertEqual(seed["listen"]["api_key"], "env:ROUTER_API_KEY")
            # The containerized listen is non-loopback with env: references, so
            # the variables must exist for parse_config to accept the seed.
            app = self._strict_app(seed)
            app._check_vendor_binding(app.config)
        # Without one the entrypoint seeds a random management key: containers
        # listen on 0.0.0.0, so the loopback seed's passwordless listen would
        # expose the console — and a dangling env reference would lock it on
        # first start.
        with mock.patch.dict(os.environ, {"ROUTER_API_KEY": "k"}, clear=True):
            seed = module.preset_config()
        self.assertRegex(seed["listen"]["admin_key"], r"^sk-admin-[0-9a-f]{32}$")


def volc_config() -> dict:
    """Two channels on one vendor, sharing a single management-plane account."""
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "volc-coding", "provider": "volcengine", "mode": "coding",
             "base_url": "https://ark.example/api/v3", "api_key": "k",
             "models": {"auto": "*"}},
            {"name": "old-claude", "protocol": "anthropic",
             "base_url": "https://old.example/v1", "api_key": "k",
             "models": {"auto": "*"}},
        ],
        "routes": [{"name": "default", "protocol": "openai",
                    "channels": ["volc-coding"]},
                   {"name": "default", "protocol": "anthropic",
                    "channels": ["old-claude"]}],
        # Vendor-level fallback: the account should win over it.
        "credentials": {"volcengine": {"VOLC_ACCESSKEY": "vendor-ak"}},
        "accounts": [{
            "id": "volc-main", "provider": "volcengine", "label": "火山主账号",
            "credentials": {"VOLC_ACCESSKEY": "AK1", "VOLC_SECRETKEY": "SK1"},
        }],
    }


class AccountTests(unittest.TestCase):
    """Optional account entity: parse, credential chain, console round-trip."""

    def test_account_parses_and_wins_the_credential_chain(self):
        from facet.gateway.accounts import resolve_creds
        raw = volc_config()
        raw["channels"][0]["account"] = "volc-main"
        raw["channels"][0]["business_line"] = "coding"
        cfg = parse_config(raw)
        self.assertEqual([a.id for a in cfg.accounts], ["volc-main"])
        self.assertEqual(cfg.accounts[0].credentials,
                         {"VOLC_ACCESSKEY": "AK1", "VOLC_SECRETKEY": "SK1"})
        volc = next(c for c in cfg.channels if c.name == "volc-coding")
        self.assertEqual(volc.account, "volc-main")
        self.assertEqual(volc.business_line, "coding")
        self.assertEqual(resolve_creds(volc, cfg),
                         {"VOLC_ACCESSKEY": "AK1", "VOLC_SECRETKEY": "SK1"})
        # A channel without an account binding resolves nothing on its own.
        claude = next(c for c in cfg.channels if c.name == "old-claude")
        self.assertEqual(resolve_creds(claude, cfg), {})

    def test_channel_creds_override_account_creds(self):
        from facet.gateway.accounts import resolve_creds
        raw = volc_config()
        raw["channels"][0].update({"account": "volc-main",
                                   "creds": {"VOLC_ACCESSKEY": "own-ak"}})
        cfg = parse_config(raw)
        volc = next(c for c in cfg.channels if c.name == "volc-coding")
        self.assertEqual(resolve_creds(volc, cfg), {"VOLC_ACCESSKEY": "own-ak"})

    def test_invalid_account_is_skipped_not_fatal(self):
        raw = volc_config()
        raw["accounts"] = [
            {"id": "9bad", "provider": "volcengine", "label": "坏 id",
             "credentials": {"VOLC_ACCESSKEY": "x"}},
            {"id": "no-vendor", "provider": "nope", "label": "坏厂家",
             "credentials": {"VOLC_ACCESSKEY": "x"}},
            {"id": "volc-main", "provider": "volcengine", "label": "火山主账号",
             "credentials": {"VOLC_ACCESSKEY": "AK1"}},
        ]
        cfg = parse_config(raw)
        self.assertEqual([a.id for a in cfg.accounts], ["volc-main"])

    def test_console_masks_and_restores_account_secrets(self):
        from facet.gateway.server import App, merge_secrets
        import hashlib
        import copy
        import json
        
        def _compute_config_version(raw: dict) -> str:
            visible = copy.deepcopy(raw)
            for channel in (visible.get("channels") or []):
                key = channel.get("api_key")
                if key and not str(key).startswith("env:"):
                    channel["api_key"] = "***"
                own_creds = channel.get("creds")
                if isinstance(own_creds, dict):
                    for env_name, value in own_creds.items():
                        if value and not str(value).startswith("env:"):
                            own_creds[env_name] = "***"
            listen = visible.get("listen") or {}
            for field in ("api_key", "admin_key"):
                if listen.get(field) and not str(listen[field]).startswith("env:"):
                    listen[field] = "***"
            creds = visible.get("credentials")
            if isinstance(creds, dict):
                for vendor_map in creds.values():
                    if not isinstance(vendor_map, dict):
                        continue
                    for env_name, value in vendor_map.items():
                        if value and not str(value).startswith("env:"):
                            vendor_map[env_name] = "***"
            for account in (visible.get("accounts") or []):
                if not isinstance(account, dict):
                    continue
                acc_creds = account.get("credentials")
                if isinstance(acc_creds, dict):
                    for env_name, value in acc_creds.items():
                        if value and not str(value).startswith("env:"):
                            acc_creds[env_name] = "***"
            serialized = json.dumps(visible, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        
        raw = volc_config()
        cfg = parse_config(raw)
        app = type("FakeApp", (), {
            "raw": raw, 
            "config": cfg,
            "_compute_config_version": staticmethod(_compute_config_version),
        })()
        visible = App.public_config(app)
        self.assertEqual(visible["accounts"][0]["credentials"],
                         {"VOLC_ACCESSKEY": "***", "VOLC_SECRETKEY": "***"})
        merged = merge_secrets(visible, raw)
        self.assertEqual(merged["accounts"][0]["credentials"],
                         {"VOLC_ACCESSKEY": "AK1", "VOLC_SECRETKEY": "SK1"})
        # A save that deletes the account must keep it deleted.
        visible["accounts"] = []
        self.assertEqual(merge_secrets(visible, raw)["accounts"], [])


if __name__ == "__main__":
    unittest.main()
