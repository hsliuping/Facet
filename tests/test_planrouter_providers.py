from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from planrouter import providers as pmod


def provider_by_id(providers: list[dict], pid: str) -> dict | None:
    return next((p for p in providers if p.get("id") == pid), None)


class BuiltinRegistryTests(unittest.TestCase):
    """The shipped registry must stay structurally valid."""

    def setUp(self):
        self.providers = pmod.load_providers(None)

    def test_builtin_is_loaded(self):
        self.assertTrue(self.providers)
        ids = [p["id"] for p in self.providers]
        self.assertEqual(len(ids), len(set(ids)))
        for expected in ("volcengine", "zhipu", "deepseek", "moonshot",
                         "anthropic", "openai"):
            self.assertIn(expected, ids)

    def test_every_mode_is_well_formed(self):
        for provider in self.providers:
            self.assertTrue(provider.get("id"))
            self.assertTrue(provider.get("name"))
            modes = provider.get("modes")
            self.assertIsInstance(modes, dict)
            self.assertTrue(modes, f"provider {provider['id']} needs modes")
            for key, mode in modes.items():
                self.assertIn(key, pmod.MODE_KEYS)
                self.assertTrue(mode.get("label"))
                self.assertIn(mode.get("protocol"), ("openai", "anthropic"))
                self.assertTrue(mode.get("base_url", "").startswith("http"))
                self.assertIn(mode.get("tier"), ("free", "paid"))
                docs = mode.get("docs_url", "")
                self.assertTrue(docs == "" or docs.startswith("http"))
                self.assertIsInstance(mode.get("models") or [], list)


class LoadProvidersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_missing_overlay_returns_builtin(self):
        self.assertEqual(pmod.load_providers(self.config_dir),
                         pmod.load_providers(None))

    def test_overlay_overrides_whole_provider_entry(self):
        builtin = pmod.load_providers(None)
        original = provider_by_id(builtin, "volcengine")
        override = json.loads(json.dumps(original, ensure_ascii=False))
        override["name"] = "火山引擎（自定义）"
        override["modes"]["coding"]["models"] = ["kimi-k3"]
        (self.config_dir / "providers.json").write_text(
            json.dumps({"providers": [override]}, ensure_ascii=False),
            encoding="utf-8")
        merged = pmod.load_providers(self.config_dir)
        by_id = {p["id"]: p for p in merged}
        self.assertEqual(by_id["volcengine"], override)
        # Unrelated vendors survive untouched and order is preserved.
        self.assertEqual(by_id["zhipu"], provider_by_id(builtin, "zhipu"))
        self.assertEqual([p["id"] for p in merged],
                         [p["id"] for p in builtin])

    def test_overlay_may_add_new_provider(self):
        extra = {"id": "custom-vendor", "name": "自建网关",
                 "modes": {"payg": {"label": "按量付费", "protocol": "openai",
                                    "base_url": "https://gw.example.com/v1",
                                    "docs_url": "", "tier": "paid",
                                    "models": []}}}
        (self.config_dir / "providers.json").write_text(
            json.dumps({"providers": [extra]}, ensure_ascii=False),
            encoding="utf-8")
        merged = pmod.load_providers(self.config_dir)
        self.assertEqual(provider_by_id(merged, "custom-vendor"), extra)
        self.assertEqual(len(merged), len(pmod.load_providers(None)) + 1)

    def test_broken_overlay_json_raises(self):
        (self.config_dir / "providers.json").write_text("{ not json",
                                                        encoding="utf-8")
        with self.assertRaises(ValueError):
            pmod.load_providers(self.config_dir)

    def test_broken_overlay_structure_raises(self):
        (self.config_dir / "providers.json").write_text(
            json.dumps({"providers": "nope"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            pmod.load_providers(self.config_dir)


class SaveOverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_writes_updated_provider(self):
        builtin = pmod.load_providers(None)
        entry = provider_by_id(builtin, "volcengine")
        entry["modes"]["coding"]["models"] = ["kimi-k3"]
        target = pmod.save_overlay(self.config_dir, [entry])
        self.assertTrue(target.is_file())
        data = json.loads(target.read_text(encoding="utf-8"))
        saved = provider_by_id(data["providers"], "volcengine")
        self.assertEqual(saved["modes"]["coding"]["models"], ["kimi-k3"])

    def test_second_write_backs_up_previous_file(self):
        builtin = pmod.load_providers(None)
        entry = provider_by_id(builtin, "volcengine")
        entry["modes"]["coding"]["models"] = ["kimi-k3"]
        pmod.save_overlay(self.config_dir, [entry])
        entry["modes"]["coding"]["models"] = ["kimi-k3", "glm-5.3"]
        target = pmod.save_overlay(self.config_dir, [entry])
        backup = Path(str(target) + ".bak")
        self.assertTrue(backup.is_file())
        backup_data = json.loads(backup.read_text(encoding="utf-8"))
        saved = provider_by_id(backup_data["providers"], "volcengine")
        self.assertEqual(saved["modes"]["coding"]["models"], ["kimi-k3"])
        current = json.loads(target.read_text(encoding="utf-8"))
        saved = provider_by_id(current["providers"], "volcengine")
        self.assertEqual(saved["modes"]["coding"]["models"],
                         ["kimi-k3", "glm-5.3"])

    def test_save_merges_with_existing_overlay_entries(self):
        builtin = pmod.load_providers(None)
        first = provider_by_id(builtin, "volcengine")
        first["modes"]["coding"]["models"] = ["kimi-k3"]
        pmod.save_overlay(self.config_dir, [first])
        second = provider_by_id(builtin, "zhipu")
        second["modes"]["payg"]["models"] = ["glm-5.3"]
        pmod.save_overlay(self.config_dir, [second])
        data = json.loads((self.config_dir / "providers.json")
                          .read_text(encoding="utf-8"))
        ids = [p["id"] for p in data["providers"]]
        self.assertEqual(ids, ["volcengine", "zhipu"])
        # Merging the overlay back over the builtin keeps both refreshes.
        merged = pmod.load_providers(self.config_dir)
        self.assertEqual(provider_by_id(merged, "volcengine")
                         ["modes"]["coding"]["models"], ["kimi-k3"])
        self.assertEqual(provider_by_id(merged, "zhipu")
                         ["modes"]["payg"]["models"], ["glm-5.3"])


if __name__ == "__main__":
    unittest.main()
