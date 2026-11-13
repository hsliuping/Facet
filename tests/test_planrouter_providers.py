from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from facet.gateway import providers as pmod


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

    def test_free_quota_clis_declare_management_credentials(self):
        """Every free-quota CLI needs management AK/SK the console can collect.

        Both `facet-ark-quota` and `bl` read the vendor's IAM/RAM key pair
        from these env vars, so the registry must declare them for the
        channel editor to render the inputs and for the sweep to inject.
        """
        expected = {
            "volcengine": ("VOLC_ACCESSKEY", "VOLC_SECRETKEY"),
            "bailian": ("BAILIAN_ACCESS_KEY_ID", "BAILIAN_ACCESS_KEY_SECRET"),
        }
        for pid, envs in expected.items():
            mode = provider_by_id(self.providers, pid)["modes"]["payg"]
            self.assertTrue(mode.get("free_quota_cmd"), pid)
            got = tuple(e["env"] for e in mode.get("credential_envs") or [])
            self.assertEqual(got, envs, pid)

    def test_credential_help_declared_and_expanded(self):
        """AK/SK acquisition walkthrough ships for the two hard vendors.

        Volcengine needs an IAM user + the ArkFullAccess policy; Bailian
        needs a RAM AccessKey + AliyunBailianFullAccess + workspace
        membership. The registry declares the guide once per vendor and
        load_providers expands it into every credential-collecting mode so
        the channel editor can render it next to the inputs.
        """
        for pid, tokens in (
            ("volcengine", ("ArkFullAccess", "访问控制", "显示一次")),
            ("bailian", ("AliyunBailianFullAccess", "业务空间", "显示一次")),
        ):
            vendor = provider_by_id(self.providers, pid)
            block = vendor.get("credential_help")
            self.assertTrue(block and block.get("steps"), pid)
            self.assertTrue(block.get("permissions"), pid)
            self.assertTrue(block.get("note"), pid)
            text = (" ".join(block["steps"]) + block.get("permissions", "")
                    + block.get("note", ""))
            for token in tokens:
                self.assertIn(token, text, pid)
            for key, mode in vendor["modes"].items():
                if mode.get("credential_envs"):
                    self.assertEqual(mode.get("credential_help"), block,
                                     f"{pid}.{key}")

    def test_openrouter_declares_topup_tier_presets(self):
        """OpenRouter's daily :free cap depends on the account's balance.

        Without a top-up it is 50 requests/day; a $10+ balance lifts it to
        1,000. The presets turn that into editor buttons, so they must track
        the vendor's real numbers.
        """
        mode = provider_by_id(self.providers, "openrouter")["modes"]["payg"]
        presets = mode["free_limit_presets"]
        self.assertEqual([p["label"] for p in presets],
                         ["未充值（余额 < $10）", "已充值 ≥ $10"])
        self.assertEqual(presets[0]["rate"], {"rpm": 20, "rpd": 50})
        self.assertEqual(presets[1]["rate"], {"rpm": 20, "rpd": 1000})
        self.assertTrue(all(p.get("note") for p in presets))


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

    def test_overlay_entry_inherits_credential_help(self):
        """An overlay entry predating credential_help must not erase it.

        The vendor-level block is backfilled from builtin and expanded into
        the modes, exactly like the free-quota command and env declarations.
        """
        builtin = provider_by_id(pmod.load_providers(None), "volcengine")
        overlay = {"id": "volcengine", "name": "火山引擎方舟",
                   "modes": {"payg": {"label": "按量付费", "protocol": "openai",
                                      "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                                      "tier": "paid", "models": []}}}
        (self.config_dir / "providers.json").write_text(
            json.dumps({"providers": [overlay]}, ensure_ascii=False),
            encoding="utf-8")
        merged = provider_by_id(pmod.load_providers(self.config_dir),
                                "volcengine")
        self.assertEqual(merged.get("credential_help"),
                         builtin.get("credential_help"))
        self.assertEqual(merged["modes"]["payg"].get("credential_help"),
                         builtin.get("credential_help"))

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


def valid_vendor(**overrides) -> dict:
    entry = {
        "id": "acme",
        "name": "Acme AI",
        "modes": {"payg": {
            "label": "按量付费", "protocol": "openai",
            "base_url": "https://api.acme.com/v1", "tier": "paid",
            "docs_url": "https://docs.acme.com/pricing",
            "models_url": "", "models": [" acme-large ", "", "acme-small"],
        }},
    }
    for key, value in overrides.items():
        entry[key] = value
    return entry


class NormalizeVendorTests(unittest.TestCase):
    def test_valid_entry_is_normalized(self):
        entry = pmod.normalize_vendor(valid_vendor())
        self.assertEqual(entry["id"], "acme")
        self.assertEqual(entry["name"], "Acme AI")
        mode = entry["modes"]["payg"]
        self.assertEqual(mode["label"], "按量付费")
        self.assertEqual(mode["protocol"], "openai")
        self.assertEqual(mode["base_url"], "https://api.acme.com/v1")
        self.assertEqual(mode["tier"], "paid")
        self.assertEqual(mode["docs_url"], "https://docs.acme.com/pricing")
        self.assertEqual(mode["models_url"], "")
        # Models are stripped, empties dropped, whitespace cleaned.
        self.assertEqual(mode["models"], ["acme-large", "acme-small"])

    def test_tier_defaults_to_empty(self):
        entry = valid_vendor()
        del entry["modes"]["payg"]["tier"]
        self.assertEqual(pmod.normalize_vendor(entry)["modes"]["payg"]["tier"], "")

    def test_missing_id_raises(self):
        entry = valid_vendor()
        del entry["id"]
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(entry)

    def test_bad_id_raises(self):
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(valid_vendor(id="Acme!"))
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(valid_vendor(id=""))

    def test_missing_name_raises(self):
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(valid_vendor(name="  "))

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(["not", "a", "dict"])

    def test_missing_or_empty_modes_raises(self):
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(valid_vendor(modes={}))
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(valid_vendor(modes="nope"))

    def test_unknown_mode_key_raises(self):
        entry = valid_vendor()
        entry["modes"]["ultra"] = dict(entry["modes"]["payg"])
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(entry)

    def test_bad_protocol_raises(self):
        entry = valid_vendor()
        entry["modes"]["payg"]["protocol"] = "grpc"
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(entry)

    def test_missing_label_raises(self):
        entry = valid_vendor()
        entry["modes"]["payg"]["label"] = ""
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(entry)

    def test_bad_base_url_raises(self):
        entry = valid_vendor()
        entry["modes"]["payg"]["base_url"] = "ftp://api.acme.com"
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(entry)

    def test_bad_docs_url_raises(self):
        entry = valid_vendor()
        entry["modes"]["payg"]["docs_url"] = "javascript:alert(1)"
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(entry)

    def test_bad_models_url_raises(self):
        entry = valid_vendor()
        entry["modes"]["payg"]["models_url"] = "notaurl"
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(entry)

    def test_non_list_models_raises(self):
        entry = valid_vendor()
        entry["modes"]["payg"]["models"] = "acme-large"
        with self.assertRaises(ValueError):
            pmod.normalize_vendor(entry)

    def test_free_limits_are_normalized(self):
        entry = valid_vendor()
        entry["modes"]["payg"]["free_limits"] = {
            "rpm": 40, "rpd": 250, "note": " 免费层约 40 RPM ",
        }
        mode = pmod.normalize_vendor(entry)["modes"]["payg"]
        self.assertEqual(mode["free_limits"],
                         {"rpm": 40, "rpd": 250, "note": "免费层约 40 RPM"})

    def test_empty_free_limits_become_none(self):
        entry = valid_vendor()
        entry["modes"]["payg"]["free_limits"] = {"note": "   "}
        self.assertIsNone(
            pmod.normalize_vendor(entry)["modes"]["payg"]["free_limits"])

    def test_bad_free_limits_raise(self):
        for value in ("nope", {"rpm": 0}, {"rpm": -5}, {"rpm": "40"},
                      {"rpm": True}, {"note": 7}):
            entry = valid_vendor()
            entry["modes"]["payg"]["free_limits"] = value
            with self.assertRaises(ValueError):
                pmod.normalize_vendor(entry)

    def test_free_limit_presets_are_normalized(self):
        entry = valid_vendor()
        entry["modes"]["payg"]["free_limit_presets"] = [
            {"label": " 未充值 ", "rate": {"rpm": 20, "rpd": 50},
             "note": " 50 次/天 "},
            {"label": "已充值", "rate": {}},
        ]
        mode = pmod.normalize_vendor(entry)["modes"]["payg"]
        self.assertEqual(mode["free_limit_presets"], [
            {"label": "未充值", "rate": {"rpm": 20, "rpd": 50},
             "note": "50 次/天"},
            {"label": "已充值", "rate": {}},
        ])

    def test_missing_free_limit_presets_become_empty(self):
        mode = pmod.normalize_vendor(valid_vendor())["modes"]["payg"]
        self.assertEqual(mode["free_limit_presets"], [])

    def test_bad_free_limit_presets_raise(self):
        for value in ("nope", [], [{"rate": {"rpm": 20}}], [{"label": ""}],
                      [{"label": "x", "rate": {"rpd": 0}}],
                      [{"label": "x", "note": 5}]):
            entry = valid_vendor()
            entry["modes"]["payg"]["free_limit_presets"] = value
            with self.assertRaises(ValueError):
                pmod.normalize_vendor(entry)

    def test_credential_help_is_kept_and_expanded(self):
        """A vendor-level walkthrough survives normalization and reaches modes."""
        entry = valid_vendor(credential_help={
            "steps": [" step one ", "step two"],
            "permissions": " AcmeAdmin ",
            "note": " secret shown once ",
        })
        norm = pmod.normalize_vendor(entry)
        self.assertEqual(norm["credential_help"], {
            "steps": ["step one", "step two"],
            "permissions": "AcmeAdmin",
            "note": "secret shown once"})
        self.assertEqual(norm["modes"]["payg"]["credential_help"],
                         norm["credential_help"])

    def test_mode_level_credential_help_is_kept(self):
        entry = valid_vendor()
        entry["modes"]["payg"]["credential_help"] = {
            "steps": ["only this mode"], "permissions": "", "note": None}
        mode = pmod.normalize_vendor(entry)["modes"]["payg"]
        self.assertEqual(mode["credential_help"], {"steps": ["only this mode"]})

    def test_bad_credential_help_raises(self):
        for value in ("nope", {"steps": []}, {"steps": "x"},
                      {"steps": [1]}, {"steps": ["ok"], "note": 7},
                      {"steps": ["ok"], "permissions": ["x"]}):
            with self.assertRaises(ValueError):
                pmod.normalize_vendor(valid_vendor(credential_help=value))

    def test_missing_credential_help_stays_absent(self):
        norm = pmod.normalize_vendor(valid_vendor())
        self.assertIsNone(norm["modes"]["payg"]["credential_help"])
        self.assertNotIn("credential_help", norm)


class DeleteOverlayEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_removes_only_target_entry(self):
        pmod.save_overlay(self.config_dir, [
            provider_by_id(pmod.load_providers(None), "volcengine"),
            provider_by_id(pmod.load_providers(None), "zhipu"),
        ])
        self.assertTrue(pmod.delete_overlay_entry(self.config_dir, "volcengine"))
        data = json.loads((self.config_dir / "providers.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual([p["id"] for p in data["providers"]], ["zhipu"])

    def test_missing_id_returns_false_and_keeps_file(self):
        pmod.save_overlay(self.config_dir, [
            provider_by_id(pmod.load_providers(None), "zhipu")])
        self.assertFalse(pmod.delete_overlay_entry(self.config_dir, "nope"))
        data = json.loads((self.config_dir / "providers.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual([p["id"] for p in data["providers"]], ["zhipu"])

    def test_no_overlay_file_returns_false(self):
        self.assertFalse(pmod.delete_overlay_entry(self.config_dir, "zhipu"))

    def test_backup_written_on_delete(self):
        pmod.save_overlay(self.config_dir, [
            provider_by_id(pmod.load_providers(None), "zhipu")])
        self.assertTrue(pmod.delete_overlay_entry(self.config_dir, "zhipu"))
        backup = self.config_dir / "providers.json.bak"
        self.assertTrue(backup.is_file())
        data = json.loads(backup.read_text(encoding="utf-8"))
        self.assertEqual([p["id"] for p in data["providers"]], ["zhipu"])

    def test_overlay_ids_reports_custom_ids(self):
        self.assertEqual(pmod.overlay_ids(self.config_dir), set())
        pmod.save_overlay(self.config_dir, [
            provider_by_id(pmod.load_providers(None), "zhipu")])
        self.assertEqual(pmod.overlay_ids(self.config_dir), {"zhipu"})


if __name__ == "__main__":
    unittest.main()
