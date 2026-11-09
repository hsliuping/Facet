"""Vendor management credentials: parse, mask, merge and persist.

The console editor stores IAM-style credentials (e.g. Volcengine AK/SK for
the free-quota CLI) in the top-level `credentials` section of router.json:
{provider: {ENV_NAME: value}} where value is plaintext or an `env:VAR`
reference. The editor round-trips masked values as "***", so saving must
inherit stored values instead of erasing them — the same rules as the
per-channel api_key.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from planrouter.core import parse_config
from planrouter.server import App, merge_secrets


def base_config() -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "ark-free", "tier": "free",
             "base_url": "https://ark.example/v3", "api_key": "k",
             "models": {"auto": "m"}},
            {"name": "claude", "protocol": "anthropic",
             "base_url": "https://claude.example/v1", "api_key": "k",
             "models": {"claude-x": "claude-x"}},
        ],
        "routes": [{"name": "default", "protocol": "openai",
                    "channels": ["ark-free"]},
                   {"name": "default", "protocol": "anthropic",
                    "channels": ["claude"]}],
        "credentials": {"volcengine": {"VOLC_ACCESSKEY": "ak-plain",
                                       "VOLC_SECRETKEY": "sk-plain"}},
    }


class ParseCredentialsTests(unittest.TestCase):
    def test_plaintext_and_env_resolution(self):
        raw = base_config()
        raw["credentials"]["volcengine"]["VOLC_ACCESSKEY"] = "env:VOLC_AK"
        with mock.patch.dict(os.environ, {"VOLC_AK": "ak-from-env"}):
            config = parse_config(raw)
        self.assertEqual(config.credentials["volcengine"], {
            "VOLC_ACCESSKEY": "ak-from-env",
            "VOLC_SECRETKEY": "sk-plain",
        })

    def test_missing_env_var_dropped(self):
        raw = base_config()
        raw["credentials"]["volcengine"]["VOLC_ACCESSKEY"] = "env:NO_SUCH_VAR_XYZ"
        with mock.patch.dict(os.environ, {}, clear=True):
            config = parse_config(raw)
        self.assertEqual(config.credentials["volcengine"],
                         {"VOLC_SECRETKEY": "sk-plain"})

    def test_malformed_entries_tolerated(self):
        raw = base_config()
        raw["credentials"] = {
            "volcengine": "not-a-dict",           # vendor entry dropped
            "bailian": {"BL_KEY": 123,            # non-str dropped
                        "OK_KEY": "  spaced  "},
        }
        config = parse_config(raw)
        self.assertEqual(config.credentials, {"bailian": {"OK_KEY": "spaced"}})

    def test_section_not_a_dict_ignored(self):
        raw = base_config()
        raw["credentials"] = ["nope"]
        self.assertEqual(parse_config(raw).credentials, {})

    def test_absent_section_empty(self):
        raw = base_config()
        raw.pop("credentials")
        self.assertEqual(parse_config(raw).credentials, {})


class MergeCredentialsTests(unittest.TestCase):
    def test_masked_and_empty_inherit_stored(self):
        old = base_config()
        new = base_config()
        new["credentials"]["volcengine"] = {
            "VOLC_ACCESSKEY": "***", "VOLC_SECRETKEY": ""}
        merged = merge_secrets(new, old)
        self.assertEqual(merged["credentials"]["volcengine"], {
            "VOLC_ACCESSKEY": "ak-plain", "VOLC_SECRETKEY": "sk-plain"})

    def test_explicit_value_wins(self):
        old = base_config()
        new = base_config()
        new["credentials"]["volcengine"]["VOLC_ACCESSKEY"] = "ak-new"
        merged = merge_secrets(new, old)
        self.assertEqual(merged["credentials"]["volcengine"]["VOLC_ACCESSKEY"],
                         "ak-new")

    def test_env_reference_passes_through(self):
        old = base_config()
        new = base_config()
        new["credentials"]["volcengine"]["VOLC_ACCESSKEY"] = "env:VOLC_ACCESSKEY"
        merged = merge_secrets(new, old)
        self.assertEqual(merged["credentials"]["volcengine"]["VOLC_ACCESSKEY"],
                         "env:VOLC_ACCESSKEY")

    def test_stale_tab_inherits_whole_section(self):
        old = base_config()
        new = base_config()
        del new["credentials"]
        merged = merge_secrets(new, old)
        self.assertEqual(merged["credentials"], old["credentials"])

    def test_unconfigured_value_removed_and_empty_vendor_dropped(self):
        old = base_config()
        new = base_config()
        new["credentials"] = {"volcengine": {"VOLC_ACCESSKEY": "***",
                                             "VOLC_SECRETKEY": "sk-new"},
                              "newvendor": {"X": "***"}}  # nothing stored
        merged = merge_secrets(new, old)
        self.assertEqual(merged["credentials"], {
            "volcengine": {"VOLC_ACCESSKEY": "ak-plain",
                           "VOLC_SECRETKEY": "sk-new"},
        })


class AppCredentialsTests(unittest.TestCase):
    """public_config masking + apply_config persistence over a real file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "router.json"
        self.path.write_text(json.dumps(base_config()), encoding="utf-8")
        self.app = App(parse_config(base_config()), config_path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_public_config_masks_plaintext_keeps_env_refs(self):
        visible = self.app.public_config()
        self.assertEqual(visible["credentials"]["volcengine"],
                         {"VOLC_ACCESSKEY": "***", "VOLC_SECRETKEY": "***"})
        self.assertEqual(visible["channels"][0]["api_key"], "***")

    def test_apply_config_masks_and_inherits_then_persists(self):
        raw = dict(self.app.raw)
        raw["credentials"] = {"volcengine": {"VOLC_ACCESSKEY": "***",
                                             "VOLC_SECRETKEY": "sk-rotated"}}
        self.app.apply_config(raw)
        self.assertEqual(self.app.raw["credentials"]["volcengine"], {
            "VOLC_ACCESSKEY": "ak-plain", "VOLC_SECRETKEY": "sk-rotated"})
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["credentials"]["volcengine"], {
            "VOLC_ACCESSKEY": "ak-plain", "VOLC_SECRETKEY": "sk-rotated"})
        # Resolved values flow into the sweep's subprocess injection.
        self.assertEqual(self.app.config.credentials["volcengine"], {
            "VOLC_ACCESSKEY": "ak-plain", "VOLC_SECRETKEY": "sk-rotated"})


if __name__ == "__main__":
    unittest.main()
