"""Free-quota alignment: parsing, filtering, sweep and scheduled application."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from planrouter import freealign
from planrouter import providers as providers_mod
from planrouter.core import load_config, parse_config
from planrouter.server import App


TODAY = date.today().isoformat()
FUTURE = (date.today() + timedelta(days=30)).isoformat()
PAST = (date.today() - timedelta(days=1)).isoformat()


def bl_rows() -> list[dict]:
    """Fixture shaped like `bl usage free --output json` output."""
    return [
        {"model": "qwen3.8-flash", "type": "Text", "remaining": 900000,
         "total": 1000000, "expires": TODAY, "autoStop": True},
        {"model": "qwen3.8-27b", "type": "Text", "remaining": 5,
         "total": 100, "expires": FUTURE, "autoStop": True},
        # Exhausted quota → dropped.
        {"model": "qwen3.8-max-0902", "type": "Text", "remaining": 0,
         "total": 100, "expires": FUTURE, "autoStop": True},
        # Expired quota → dropped.
        {"model": "deepseek-v4-pro-0813", "type": "Text", "remaining": 10,
         "total": 100, "expires": PAST, "autoStop": True},
        # Non-chat types never serve /chat/completions → dropped.
        {"model": "qwen3.8-vl-x", "type": "Vision", "remaining": 500,
         "total": 500, "expires": FUTURE},
        {"model": "text-embedding-x", "type": "Embedding", "remaining": 500,
         "total": 500, "expires": FUTURE},
    ]


FREE_MODELS = ["qwen3.8-flash", "qwen3.8-27b"]


def align_config() -> dict:
    key = "sk-" + "fixture"
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "百炼免费", "protocol": "openai",
             "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "api_key": key, "priority": 46, "rate": {"rpm": 60},
             "models": {"auto": "", "qwen3.8-flash": "qwen3.8-flash",
                        "old-paid": "old-paid"},
             "tier": "free", "tiers": {}, "provider": "bailian", "mode": "payg"},
            {"name": "百炼付费", "protocol": "openai",
             "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "api_key": key, "models": {"qwen-plus": "qwen-plus"},
             "tier": "paid", "tiers": {}, "provider": "bailian", "mode": "payg"},
            {"name": "魔搭免费", "protocol": "openai",
             "base_url": "https://api-inference.modelscope.cn/v1",
             "api_key": key, "models": {"Qwen/Qwen3.5": "Qwen/Qwen3.5"},
             "tier": "free", "tiers": {}, "provider": "modelscope",
             "mode": "payg"},
            {"name": "claude-x", "protocol": "anthropic",
             "base_url": "https://api.anthropic.com", "api_key": key,
             "models": {"claude-sonnet-x": "claude-sonnet-x"}, "priority": 90},
        ],
        "routes": [
            {"name": "default", "protocol": "openai",
             "channels": ["百炼免费", "百炼付费", "魔搭免费"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude-x"]},
        ],
    }


def registry_fixture() -> list[dict]:
    return [{
        "id": "bailian", "name": "阿里云百炼",
        "modes": {
            "payg": {
                "label": "按量付费", "protocol": "openai",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "tier": "paid", "models": [],
                "free_quota_cmd": ["fake-bl", "usage", "free"],
            },
        },
    }, {
        "id": "modelscope", "name": "魔搭",
        "modes": {
            "payg": {
                "label": "免费推理", "protocol": "openai",
                "base_url": "https://api-inference.modelscope.cn/v1",
                "tier": "free", "models": [],
            },
        },
    }]


def fake_runner(text: str):
    def run(cmd, timeout=120):
        return text
    return run


class ParseTests(unittest.TestCase):
    def test_parses_bare_array_and_wrappers(self):
        rows = bl_rows()
        self.assertEqual(freealign.parse_free_quota_output(json.dumps(rows)),
                         rows)
        self.assertEqual(freealign.parse_free_quota_output(
            json.dumps({"data": rows})), rows)
        self.assertEqual(freealign.parse_free_quota_output("not json"), [])
        self.assertEqual(freealign.parse_free_quota_output('{"a": 1}'), [])

    def test_filters_to_usable_free_text_models(self):
        self.assertEqual(freealign.free_models_from_rows(bl_rows()), FREE_MODELS)

    def test_missing_expiry_is_dropped(self):
        rows = [{"model": "m", "type": "Text", "remaining": 5, "expires": ""}]
        self.assertEqual(freealign.free_models_from_rows(rows), [])


class RunVendorCmdTests(unittest.TestCase):
    def test_resolve_cmd_wraps_windows_shims(self):
        with mock.patch.object(freealign.shutil, "which",
                               return_value=r"C:\npm\bl.cmd"):
            self.assertEqual(freealign._resolve_cmd(["bl", "usage"]),
                             ["cmd", "/c", r"C:\npm\bl.cmd", "usage"])
        with mock.patch.object(freealign.shutil, "which",
                               return_value="/usr/local/bin/bl"):
            self.assertEqual(freealign._resolve_cmd(["bl", "usage"]),
                             ["bl", "usage"])
        with mock.patch.object(freealign.shutil, "which", return_value=None):
            self.assertEqual(freealign._resolve_cmd(["bl", "usage"]),
                             ["bl", "usage"])

    def test_success_returns_stdout(self):
        out = freealign.run_vendor_cmd(
            [sys.executable, "-c", "print('hello')"], timeout=30)
        self.assertIn("hello", out)

    def test_missing_binary_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            freealign.run_vendor_cmd(["no-such-bl-binary-xyz"], timeout=10)

    def test_nonzero_exit_raises(self):
        with self.assertRaises(RuntimeError):
            freealign.run_vendor_cmd(
                [sys.executable, "-c", "raise SystemExit(3)"], timeout=30)


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.app = App(parse_config(align_config()))
        self.app.providers = registry_fixture()

    def sweep(self, text=None, runner=None):
        if runner is None:
            payload = json.dumps(bl_rows()) if text is None else text
            runner = fake_runner(payload)
        return freealign.run_free_align(self.app, "manual", _run_cmd=runner)

    def test_identity_patch_for_free_channel_only(self):
        report = self.sweep()
        self.assertEqual(report["patch"], {
            "百炼免费": {"auto": "", **{m: m for m in FREE_MODELS}},
        })
        aligned = {row["channel"]: row for row in report["aligned"]}
        self.assertTrue(aligned["百炼免费"]["changed"])
        # Same command, but paid channels are never rewritten by the sweep.
        self.assertNotIn("百炼付费", aligned)
        # No free_quota_cmd on this vendor mode → untouched.
        self.assertNotIn("魔搭免费", aligned)
        self.assertEqual(report["alerts"], [])

    def test_unchanged_mapping_reports_no_patch(self):
        first = self.sweep()  # discovers the change…
        self.assertEqual(first["patch"], {
            "百炼免费": {"auto": "", **{m: m for m in FREE_MODELS}},
        })
        # …the server applies it; re-sweep on the new state.
        raw = align_config()
        raw["channels"][0]["models"] = first["patch"]["百炼免费"]
        self.app = App(parse_config(raw))
        self.app.providers = registry_fixture()
        report = self.sweep()
        self.assertEqual(report["patch"], {})
        aligned = {row["channel"]: row for row in report["aligned"]}
        self.assertFalse(aligned["百炼免费"]["changed"])

    def test_command_failure_keeps_mapping(self):
        def broken(cmd, timeout=120):
            raise RuntimeError("命令不存在")
        report = self.sweep(runner=broken)
        self.assertEqual(report["patch"], {})
        self.assertEqual(len(report["alerts"]), 1)
        self.assertIn("免费额度命令执行失败", report["alerts"][0]["message"])

    def test_empty_quota_clears_concrete_keys_keeps_auto(self):
        report = self.sweep(text=json.dumps([]))
        self.assertEqual(report["patch"], {"百炼免费": {"auto": ""}})


class ApplyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        json.dump(align_config(), tmp)
        tmp.close()
        self.path = Path(tmp.name)
        self.app = App(load_config(self.path), config_path=self.path)
        self.app.providers = registry_fixture()

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_run_free_align_applies_and_persists(self):
        with mock.patch.object(freealign, "run_vendor_cmd",
                               fake_runner(json.dumps(bl_rows()))):
            report = self.app.run_free_align("manual")
        self.assertEqual(report["patch"], {
            "百炼免费": {"auto": "", **{m: m for m in FREE_MODELS}},
        })
        channel = self.app.config.channel("百炼免费")
        self.assertEqual(channel.models,
                         {"auto": "", **{m: m for m in FREE_MODELS}})
        # The stale paid mapping is gone from disk too.
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        saved = next(c for c in raw["channels"] if c["name"] == "百炼免费")
        self.assertNotIn("old-paid", saved["models"])
        self.assertTrue((self.path.parent / "freealign.jsonl").is_file())

    def test_payload_exposes_free_align_state(self):
        with mock.patch.object(freealign, "run_vendor_cmd",
                               fake_runner(json.dumps(bl_rows()))):
            self.app.run_free_align("scheduled")
        payload = self.app.maintenance_payload()
        self.assertTrue(payload["free_align"]["enabled"])
        self.assertEqual(payload["free_align"]["hours"], 24)
        self.assertEqual(payload["free_align"]["reports"][0]["trigger"],
                         "scheduled")


class RegistryTests(unittest.TestCase):
    def test_normalize_keeps_free_quota_cmd(self):
        entry = providers_mod.normalize_vendor(registry_fixture()[0])
        cmd = entry["modes"]["payg"]["free_quota_cmd"]
        self.assertEqual(cmd, ["fake-bl", "usage", "free"])

    def test_normalize_rejects_bad_cmd(self):
        entry = registry_fixture()[0]
        entry["modes"]["payg"]["free_quota_cmd"] = "bl usage"
        with self.assertRaises(ValueError):
            providers_mod.normalize_vendor(entry)

    def test_overlay_entry_inherits_builtin_cmd(self):
        overlay = {
            "providers": [{
                "id": "bailian", "name": "阿里云百炼",
                "modes": {"payg": {
                    "label": "按量付费", "protocol": "openai",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "tier": "paid", "models": [],
                }},
            }],
        }
        with tempfile.TemporaryDirectory() as config_dir:
            Path(config_dir, "providers.json").write_text(
                json.dumps(overlay, ensure_ascii=False), encoding="utf-8")
            merged = providers_mod.load_providers(config_dir)
        bailian = next(p for p in merged if p.get("id") == "bailian")
        self.assertEqual(bailian["modes"]["payg"]["free_quota_cmd"],
                         ["bl", "usage", "free", "--output", "json"])


class DefaultsTests(unittest.TestCase):
    def test_free_align_hours_defaults_and_zero(self):
        config = parse_config(align_config())
        self.assertEqual(config.defaults.free_align_hours, 24)
        raw = align_config()
        raw["defaults"] = {"free_align_hours": 0}
        self.assertEqual(parse_config(raw).defaults.free_align_hours, 0)


if __name__ == "__main__":
    unittest.main()
