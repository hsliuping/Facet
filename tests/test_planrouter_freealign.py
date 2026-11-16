"""Free-quota alignment: parsing, filtering, sweep and scheduled application."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from facet.gateway import ark_quota
from facet.gateway import freealign
from facet.gateway import maintenance as maint
from facet.gateway import providers as providers_mod
from facet.gateway.core import load_config, parse_config, parse_free_quota
from facet.gateway.server import App
from facet.gateway.usage import UsageRecorder


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
# The sweep holds the provider's margin back (10k by default), so the 27b row
# above (5 tokens left) sits inside it and never enters the mapping.
SWEEP_MODELS = ["qwen3.8-flash"]


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
    def run(cmd, timeout=120, extra_env=None):
        return text
    return run


def volc_config() -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "火山免费", "protocol": "openai",
             "base_url": "https://ark.cn-beijing.volces.com/api/v3",
             "api_key": "ark-test", "priority": 100, "rate": {},
             "models": {"auto": "", "old-doubao": "old-doubao"},
             "tier": "free", "tiers": {}, "provider": "volcengine",
             "mode": "payg"},
            {"name": "claude-x", "protocol": "anthropic",
             "base_url": "https://api.anthropic.com", "api_key": "sk-x",
             "models": {"claude-sonnet-x": "claude-sonnet-x"}, "priority": 90},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["火山免费"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude-x"]},
        ],
    }


def volc_registry() -> list[dict]:
    return [{
        "id": "volcengine", "name": "火山引擎方舟",
        "modes": {"payg": {
            "label": "按量付费", "protocol": "openai",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "tier": "paid", "models": [], "free_quota_cmd": ["facet-ark-quota"],
            "free_quota": {"tokens": 500000, "period": "total"},
        }},
    }]


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

    def test_reserve_margin_drops_inside_margin_rows(self):
        rows = [
            {"model": "big", "type": "Text", "remaining": 150000,
             "total": 500000, "expires": FUTURE},
            {"model": "small", "type": "Text", "remaining": 80000,
             "total": 500000, "expires": FUTURE},
        ]
        # Without a margin only a hard zero is unusable.
        self.assertEqual(freealign.free_models_from_rows(rows),
                         ["big", "small"])
        # Volcengine keeps 100k in reserve per model.
        self.assertEqual(
            freealign.free_models_from_rows(rows, reserve=100000), ["big"])


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

    def test_extra_env_reaches_child_process(self):
        out = freealign.run_vendor_cmd(
            [sys.executable, "-c",
             "import os; print(os.environ.get('FACET_TEST_TOKEN', 'MISSING'))"],
            timeout=30, extra_env={"FACET_TEST_TOKEN": "hello"})
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
            "百炼免费": {"auto": "", **{m: m for m in SWEEP_MODELS}},
        })
        aligned = {row["channel"]: row for row in report["aligned"]}
        self.assertTrue(aligned["百炼免费"]["changed"])
        # Same command, but paid channels are never rewritten by the sweep.
        self.assertNotIn("百炼付费", aligned)
        # No free_quota_cmd on this vendor mode → untouched.
        self.assertNotIn("魔搭免费", aligned)
        self.assertEqual(report["alerts"], [])

    def test_report_carries_vendor_quota_numbers(self):
        report = self.sweep()
        snapshot = report["quota"]["百炼免费"]
        # The console shows the vendor's own numbers, not the local counter.
        self.assertEqual(snapshot["qwen3.8-flash"],
                         {"remaining": 900000, "total": 1000000,
                          "consumed": 100000})
        # Rows inside the 10k default margin are dropped like exhausted ones:
        # below that a call fails outright and would only trigger a retry.
        self.assertNotIn("qwen3.8-27b", snapshot)
        # Exhausted rows never enter the mapping, so they are not reported.
        self.assertNotIn("qwen3.8-max-0902", snapshot)
        self.assertRegex(report["quota_at"], r"^\d{4}-\d{2}-\d{2}")

    def test_unchanged_mapping_reports_no_patch(self):
        first = self.sweep()  # discovers the change…
        self.assertEqual(first["patch"], {
            "百炼免费": {"auto": "", **{m: m for m in SWEEP_MODELS}},
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
        def broken(cmd, timeout=120, extra_env=None):
            raise RuntimeError("命令不存在")
        report = self.sweep(runner=broken)
        self.assertEqual(report["patch"], {})
        self.assertEqual(len(report["alerts"]), 1)
        self.assertIn("免费额度命令执行失败", report["alerts"][0]["message"])

    def test_empty_quota_clears_concrete_keys_keeps_auto(self):
        report = self.sweep(text=json.dumps([]))
        self.assertEqual(report["patch"], {"百炼免费": {"auto": ""}})

    def test_empty_quota_blanks_stale_auto_target(self):
        # All free models gone but auto still points at one: blank it, or
        # auto traffic keeps routing to a model that now bills the balance.
        raw = align_config()
        raw["channels"][0]["models"] = {"auto": "qwen3.8-flash",
                                        "qwen3.8-flash": "qwen3.8-flash"}
        self.app = App(parse_config(raw))
        self.app.providers = registry_fixture()
        report = self.sweep(text=json.dumps([]))
        self.assertEqual(report["patch"], {"百炼免费": {"auto": ""}})

    def test_credentials_injected_and_keyed_per_value(self):
        captured = []

        def run(cmd, timeout=120, extra_env=None):
            captured.append((tuple(cmd), dict(extra_env or {})))
            return json.dumps(bl_rows())

        raw = align_config()
        raw["credentials"] = {"bailian": {"BL_TOKEN": "abc"}}
        self.app = App(parse_config(raw))
        self.app.providers = registry_fixture()
        report = freealign.run_free_align(self.app, "manual", _run_cmd=run)
        self.assertEqual(captured,
                         [(("fake-bl", "usage", "free"), {"BL_TOKEN": "abc"})])
        self.assertEqual(report["alerts"], [])
        # Unrelated providers keep empty injection; the sweep still works.
        raw2 = align_config()
        raw2["credentials"] = {"volcengine": {"VOLC_ACCESSKEY": "ak"}}
        app2 = App(parse_config(raw2))
        app2.providers = registry_fixture()
        captured.clear()
        freealign.run_free_align(app2, "manual", _run_cmd=run)
        self.assertEqual(captured, [(("fake-bl", "usage", "free"), {})])

    def test_missing_credentials_skip_sweep_without_alert(self):
        # TokenHub channels declare SecretId/SecretKey for the management
        # CLI; without them the sweep must keep the current mapping and
        # stay silent instead of alerting on every run.
        raw = align_config()
        raw["channels"][0]["provider"] = "hunyuan"
        raw["channels"][0]["mode"] = "payg"
        self.app = App(parse_config(raw))
        self.app.providers = [{
            "id": "hunyuan", "name": "腾讯混元",
            "modes": {"payg": {
                "label": "按量付费", "tier": "paid", "models": [],
                "free_quota_cmd": ["facet-tokenhub-quota"],
                "credential_envs": [
                    {"env": "TENCENT_SECRET_ID", "label": "SecretId"},
                    {"env": "TENCENT_SECRET_KEY", "label": "SecretKey"}],
            }},
        }]
        ran = []

        def run(cmd, timeout=120, extra_env=None):
            ran.append(cmd)
            return json.dumps(bl_rows())

        env = {k: v for k, v in os.environ.items()
               if k not in ("TENCENT_SECRET_ID", "TENCENT_SECRET_KEY")}
        with mock.patch.dict(os.environ, env, clear=True):
            report = freealign.run_free_align(self.app, "manual", _run_cmd=run)
        self.assertEqual(ran, [])
        self.assertEqual(report["alerts"], [])
        self.assertEqual(report["patch"], {})
        self.assertEqual(report["skipped"],
                         [{"channel": "百炼免费",
                           "missing": ["TENCENT_SECRET_ID",
                                       "TENCENT_SECRET_KEY"]}])
        # Mapping untouched: the pull-time static subset still applies.
        channel = self.app.config.channel("百炼免费")
        self.assertEqual(channel.models.get("old-paid"), "old-paid")

    def test_same_command_same_credentials_run_once(self):
        seen = []

        def run(cmd, timeout=120, extra_env=None):
            seen.append(extra_env)
            return json.dumps(bl_rows())

        raw = align_config()
        dup = dict(raw["channels"][0])
        dup["name"] = "百炼免费B"
        raw["channels"].append(dup)
        raw["credentials"] = {"bailian": {"BL_TOKEN": "abc"}}
        app = App(parse_config(raw))
        app.providers = registry_fixture()
        report = freealign.run_free_align(app, "manual", _run_cmd=run)
        self.assertEqual(len(seen), 1)  # same command + same creds → once
        self.assertEqual(report["alerts"], [])

    def test_volcengine_families_resolve_to_versioned_names(self):
        app = App(parse_config(volc_config()))
        app.providers = volc_registry()
        rows = json.dumps([
            {"model": "kimi-k3-1-pro", "type": "Text", "remaining": 480693,
             "total": 500000, "consumed": 19307, "expires": FUTURE},
        ])
        with mock.patch("facet.gateway.freealign.probe_models",
                        return_value={"ok": True,
                                      "endpoint": "https://ark.../models",
                                      "models": ["kimi-k3-1-pro-261024"]}):
            report = freealign.run_free_align(app, "manual",
                                              _run_cmd=fake_runner(rows))
        self.assertEqual(report["patch"], {
            "火山免费": {"auto": "",
                         "kimi-k3-1-pro-261024": "kimi-k3-1-pro-261024"},
        })
        self.assertEqual(report["alerts"], [])
        # Family name → versioned callable id, numbers kept intact.
        self.assertEqual(report["quota"]["火山免费"]["kimi-k3-1-pro-261024"],
                         {"remaining": 480693, "total": 500000,
                          "consumed": 19307})

    def test_volcengine_resolution_failure_keeps_mapping(self):
        app = App(parse_config(volc_config()))
        app.providers = volc_registry()
        rows = json.dumps([
            {"model": "kimi-k3-1-pro", "type": "Text", "remaining": 480693,
             "expires": FUTURE},
        ])
        with mock.patch("facet.gateway.freealign.probe_models",
                        return_value={"ok": False, "error": "401"}):
            report = freealign.run_free_align(app, "manual",
                                              _run_cmd=fake_runner(rows))
        self.assertEqual(report["patch"], {})
        self.assertEqual(len(report["alerts"]), 1)
        self.assertIn("版本名解析失败", report["alerts"][0]["message"])

    def test_volcengine_reserve_trims_inside_margin_models(self):
        # A model inside the 100k margin is treated as spent by the sweep
        # too, so auto is repointed at a sibling with real headroom instead
        # of one the dispatch gate would refuse anyway.
        app = App(parse_config(volc_config()))
        app.providers = volc_registry()
        rows = json.dumps([
            {"model": "kimi-k3-1-pro", "type": "Text", "remaining": 150000,
             "total": 500000, "consumed": 350000, "expires": FUTURE},
            {"model": "doubao-seed", "type": "Text", "remaining": 80000,
             "total": 500000, "consumed": 420000, "expires": FUTURE},
        ])
        with mock.patch("facet.gateway.freealign.probe_models",
                        return_value={"ok": True,
                                      "models": ["kimi-k3-1-pro-261024",
                                                 "doubao-seed-260915"]}):
            report = freealign.run_free_align(app, "manual",
                                              _run_cmd=fake_runner(rows))
        self.assertEqual(report["patch"], {
            "火山免费": {"auto": "",
                         "kimi-k3-1-pro-261024": "kimi-k3-1-pro-261024"},
        })
        self.assertEqual(list(report["quota"]["火山免费"]),
                         ["kimi-k3-1-pro-261024"])


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
        for name in ("vendorquota.json", "vendorquota.tmp", "freealign.jsonl"):
            (self.path.parent / name).unlink(missing_ok=True)

    def test_run_free_align_applies_and_persists(self):
        with mock.patch.object(freealign, "run_vendor_cmd",
                               fake_runner(json.dumps(bl_rows()))):
            report = self.app.run_free_align("manual")
        self.assertEqual(report["patch"], {
            "百炼免费": {"auto": "", **{m: m for m in SWEEP_MODELS}},
        })
        channel = self.app.config.channel("百炼免费")
        self.assertEqual(channel.models,
                         {"auto": "", **{m: m for m in SWEEP_MODELS}})
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
        self.assertEqual(payload["free_align"]["hours"], 0.5)
        self.assertEqual(payload["free_align"]["reports"][0]["trigger"],
                         "scheduled")

    def test_vendor_quota_persists_and_survives_restart(self):
        with mock.patch.object(freealign, "run_vendor_cmd",
                               fake_runner(json.dumps(bl_rows()))):
            self.app.run_free_align("manual")
        saved = json.loads(
            (self.path.parent / "vendorquota.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["channels"]["百炼免费"]["qwen3.8-flash"],
                         {"remaining": 900000, "total": 1000000,
                          "consumed": 100000})
        self.assertEqual(saved["at"], self.app.vendor_quota_at)
        # The per-channel freshness stamp travels with the snapshot.
        self.assertIn("百炼免费", saved["synced"])
        # A restarted process reloads the snapshot instead of showing zeros.
        reloaded = App(load_config(self.path), config_path=self.path)
        reloaded.providers = registry_fixture()
        status = reloaded.free_quota_status(
            reloaded.config.channel("百炼免费"), "qwen3.8-flash")
        self.assertEqual(status["vendor_remaining"], 900000)
        self.assertEqual(status["vendor_total"], 1000000)
        self.assertEqual(status["vendor_consumed"], 100000)
        self.assertEqual(status["vendor_at"], self.app.vendor_quota_at)
        # …including its age, so a restart does not reset the freeze clock.
        self.assertIn("百炼免费", reloaded.vendor_synced)
        self.assertFalse(status["stale"])
        self.assertLess(status["sync_age"], 60)

    def test_legacy_vendor_quota_file_derives_synced_from_at(self):
        # Files written before per-channel stamps landed carry only "at":
        # treat it as the shared stamp so the freshness gate works across the
        # upgrade instead of trusting an undated snapshot forever.
        at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        (self.path.parent / "vendorquota.json").write_text(json.dumps({
            "at": at,
            "channels": {"百炼免费": {"qwen3.8-flash": {
                "remaining": 900000, "total": 1000000, "consumed": 100000}}},
        }), encoding="utf-8")
        app = App(load_config(self.path), config_path=self.path)
        self.assertIn("百炼免费", app.vendor_synced)
        self.assertGreater(app.vendor_synced["百炼免费"], 0)

    def test_free_budget_persists_and_survives_restart(self):
        usage_path = self.path.with_name(self.path.stem + "-usage.json")
        app = App(load_config(self.path), config_path=self.path,
                  usage_file=usage_path)
        app.providers = registry_fixture()
        try:
            with mock.patch.object(freealign, "run_vendor_cmd",
                                   fake_runner(json.dumps(bl_rows()))):
                app.run_free_align("manual")
            app.usage.record("百炼免费", True, tokens=40000,
                             model="qwen3.8-flash")
            app.usage.flush()
            channel = app.config.channel("百炼免费")
            self.assertAlmostEqual(
                app._free_budget_for(channel, "qwen3.8-flash"), 860000)
            # A restarted process reloads the seed (vendorquota.json) and
            # the counter (usage.json): the budget survives untouched.
            reloaded = App(load_config(self.path), config_path=self.path,
                           usage_file=usage_path)
            reloaded.providers = registry_fixture()
            self.assertAlmostEqual(
                reloaded._free_budget_for(
                    reloaded.config.channel("百炼免费"), "qwen3.8-flash"),
                860000)
        finally:
            usage_path.unlink(missing_ok=True)


class PlanQuotaTests(unittest.TestCase):
    """Vendor plan usage on the subscriptions page rows.

    plan_for() is per-row and per-credential: the first channel bound to the
    row supplies the account (its own creds first, vendor-level fallback),
    and snapshots are cached per `kind:sha1(ak:sk)`.
    """

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        raw = align_config()
        # The plan kind and the vendor gate come from the linked channel now,
        # so the fixture needs an Ark coding-plan channel to bind to.
        raw["channels"].append({
            "name": "火山引擎0198", "protocol": "openai",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "api_key": "sk-" + "fixture", "models": {"auto": "doubao"},
            "provider": "volcengine", "mode": "coding"})
        json.dump(raw, tmp)
        tmp.close()
        self.path = Path(tmp.name)
        self.app = App(load_config(self.path), config_path=self.path)
        self.app.subscriptions = [
            {"id": "s1", "vendor": "火山引擎_6198_codingplan",
             "account": "6198", "plan": "coding plan", "tier": "Pro",
             "expires_at": "2026-12-09", "channels": ["火山引擎0198"]},
            {"id": "s2", "vendor": "百炼", "account": "main",
             "plan": "free tier", "tier": "free",
             "expires_at": "2026-12-31", "channels": ["百炼免费"]},
        ]
        # Config is a frozen dataclass; swap in management credentials.
        cfg = self.app.config
        object.__setattr__(cfg, "credentials", {
            "volcengine": {"VOLC_ACCESSKEY": "ak", "VOLC_SECRETKEY": "sk"}})

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        for name in ("vendorquota.json", "vendorquota.tmp"):
            (self.path.parent / name).unlink(missing_ok=True)

    @staticmethod
    def plan_payload():
        return {"plan": "CodingPlan", "plan_type": "Pro", "status": "Running",
                "end_time": "2026-12-09T00:00:00+08:00",
                "windows": [{"level": "monthly", "percent": 3.64,
                             "reset_ts": 1796831999}]}

    def record(self, record_id: str) -> dict:
        return next(r for r in self.app.subscriptions
                    if r["id"] == record_id)

    def test_plan_attaches_only_to_codingplan_rows_and_caches(self):
        with mock.patch.object(ark_quota, "fetch_coding_plan",
                               return_value=self.plan_payload()) as fetch:
            payload = self.app.subscriptions_payload()
            again = self.app.subscriptions_payload()
        self.assertEqual(fetch.call_count, 1)  # second view hits the TTL cache
        rows = {r["id"]: r for r in payload["subscriptions"]}
        self.assertIn("vendor_plan", rows["s1"])
        self.assertNotIn("vendor_plan", rows["s2"])  # 百炼: not a plan vendor
        plan = rows["s1"]["vendor_plan"]
        self.assertEqual(plan["plan_type"], "Pro")
        self.assertTrue(plan["fetched_at"])
        self.assertEqual(again["subscriptions"][0]["vendor_plan"],
                         rows["s1"]["vendor_plan"])

    def test_fetch_failure_reports_error_and_keeps_stale_snapshot(self):
        with mock.patch.object(ark_quota, "fetch_coding_plan",
                               return_value=self.plan_payload()):
            self.app.subscriptions_payload()
        with mock.patch.object(
                ark_quota, "fetch_coding_plan",
                side_effect=RuntimeError("API 错误 ResourceNotFound.Plan")), \
             mock.patch.object(self.app._plan_pool, "submit",
                               side_effect=lambda fn, *a: fn(*a)):
            for entry in self.app.plan_entries.values():
                entry["ts"] = 0.0  # force a live retry, keep the snapshot
            plan = self.app.plan_for(self.record("s1"))
        self.assertTrue(plan["stale"])
        self.assertIn("ResourceNotFound", plan["error"])
        self.assertEqual(plan["plan_type"], "Pro")
        # Snapshot survives a restart and is served (marked stale) even when
        # the vendor API keeps failing.
        reloaded = App(load_config(self.path), config_path=self.path)
        object.__setattr__(reloaded.config, "credentials", {
            "volcengine": {"VOLC_ACCESSKEY": "ak", "VOLC_SECRETKEY": "sk"}})
        with mock.patch.object(
                ark_quota, "fetch_coding_plan",
                side_effect=RuntimeError("down")), \
             mock.patch.object(reloaded._plan_pool, "submit",
                               side_effect=lambda fn, *a: fn(*a)):
            plan = reloaded.plan_for(self.record("s1"))
        self.assertEqual(plan["plan_type"], "Pro")
        self.assertEqual(plan["error"], "down")

    def test_no_credentials_returns_none(self):
        cfg = self.app.config
        object.__setattr__(cfg, "credentials", {})
        self.assertIsNone(self.app.plan_for(self.record("s1")))

    def test_agentplan_row_uses_channel_bound_creds(self):
        """渠道即账号：第二条火山渠道自带 AK/SK，agentplan 行绑它查 AgentPlan."""
        raw = json.loads(json.dumps(self.app.raw))
        raw["channels"].append({
            "name": "火山Agent", "protocol": "openai",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "api_key": "sk-x", "models": {"kimi": "kimi"},
            "provider": "volcengine", "mode": "agent",
            "creds": {"VOLC_ACCESSKEY": "ch-ak", "VOLC_SECRETKEY": "ch-sk"}})
        self.app.apply_config(raw)
        record = {"id": "s3", "vendor": "火山引擎_agentplan",
                  "account": "other", "plan": "agent plan",
                  "channels": ["火山Agent"]}
        payload = {"plan": "AgentPlan", "plan_type": "Pro",
                   "status": "Running",
                   "windows": [{"level": "daily", "quota": 400.0,
                                "used": 10.0, "reset_ts": 1794435200}]}
        with mock.patch.object(ark_quota, "fetch_agent_plan",
                               return_value=payload) as fetch:
            plan = self.app.plan_for(record)
        # Channel-bound creds win over the vendor-level slot (ak/sk).
        fetch.assert_called_once_with("ch-ak", "ch-sk")
        self.assertEqual(plan["plan"], "AgentPlan")
        self.assertEqual(plan["windows"][0]["used"], 10.0)


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
        # Half-hour cadence: the sweep is the budget reseed point, so a dry
        # model must be caught quickly.
        self.assertEqual(config.defaults.free_align_hours, 0.5)
        raw = align_config()
        raw["defaults"] = {"free_align_hours": 0}
        self.assertEqual(parse_config(raw).defaults.free_align_hours, 0)
        raw["defaults"] = {"free_align_hours": 0.25}
        self.assertEqual(parse_config(raw).defaults.free_align_hours, 0.25)


WIRE = "doubao-seed-2-1-lite-260915"


class FreeQuotaParseTests(unittest.TestCase):
    def test_parser_accepts_tokens_requests_and_period(self):
        self.assertIsNone(parse_free_quota(None))
        self.assertEqual(parse_free_quota({"tokens": 500000}).tokens, 500000)
        self.assertEqual(parse_free_quota({"tokens": 500000}).period, "total")
        both = parse_free_quota({"tokens": 10, "requests": 5, "period": "month"})
        self.assertEqual((both.tokens, both.requests, both.period),
                         (10, 5, "month"))

    def test_parser_rejects_invalid_values(self):
        for bad in ({"tokens": -1}, {"tokens": True}, {"tokens": "500k"},
                    {"tokens": 1, "period": "year"}, {}):
            with self.assertRaises(ValueError):
                parse_free_quota(bad)
        with self.assertRaisesRegex(ValueError, "free_quota must be an object"):
            parse_free_quota([1])

    def test_channel_config_carries_free_quota(self):
        raw = align_config()
        raw["channels"][0]["free_quota"] = {"tokens": 1000, "requests": 10}
        channel = parse_config(raw).channel("百炼免费")
        self.assertEqual(channel.free_quota.tokens, 1000)
        self.assertEqual(channel.free_quota.requests, 10)
        raw["channels"][0]["free_quota"] = {"tokens": 1000, "period": "year"}
        with self.assertRaisesRegex(ValueError, "百炼免费"):
            parse_config(raw)

    def test_builtin_baselines_per_vendor_model(self):
        merged = providers_mod.load_providers(None)
        volc = next(p for p in merged if p["id"] == "volcengine")
        self.assertEqual(volc["modes"]["payg"]["free_quota"]["tokens"], 500000)
        bailian = next(p for p in merged if p["id"] == "bailian")
        self.assertEqual(bailian["modes"]["payg"]["free_quota"]["tokens"],
                         1000000)


class FreeQuotaUsageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        tmp.close()
        self.path = Path(tmp.name)
        self.usage = UsageRecorder(self.path)
        self.addCleanup(self.path.unlink, missing_ok=True)

    def record(self, when: date, tokens: int, success: bool = True):
        self.usage.record("火山免费", success, tokens=tokens, on=when,
                          model=WIRE)

    def test_total_month_and_day_windows(self):
        self.record(date(2026, 1, 31), 100)
        self.record(date(2026, 2, 1), 200)
        self.record(date(2026, 2, 1), 300, success=False)
        self.assertEqual(self.usage.model_usage("火山免费", WIRE)["tokens"], 600)
        self.assertEqual(
            self.usage.model_usage("火山免费", WIRE, "month",
                                   on=date(2026, 2, 15))["tokens"], 500)
        self.assertEqual(
            self.usage.model_usage("火山免费", WIRE, "day",
                                   on=date(2026, 2, 1))["tokens"], 500)
        self.assertEqual(
            self.usage.model_usage("火山免费", WIRE, "day",
                                   on=date(2026, 2, 1))["success"], 1)
        # Unknown model / channel never counts.
        self.assertEqual(self.usage.model_usage("火山免费", "nope")["tokens"], 0)


class FreeQuotaRoutingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        tmp.close()
        self.path = Path(tmp.name)
        self.addCleanup(self.path.unlink, missing_ok=True)
        raw = volc_config()
        raw["channels"][0]["models"] = {"auto": "", WIRE: WIRE}
        self.app = App(parse_config(raw), usage_file=self.path)
        self.app.providers = volc_registry()
        self.channel = self.app.config.channel("火山免费")

    def test_baseline_quota_drives_exhaustion(self):
        self.app.usage.record("火山免费", True, tokens=400000, model=WIRE)
        status = self.app.free_quota_status(self.channel, WIRE)
        self.assertEqual(status["source"], "provider_baseline")
        self.assertEqual(status["limit_tokens"], 500000)
        self.assertFalse(status["exhausted"])
        self.assertTrue(status["warning"])  # 80% used
        self.app.usage.record("火山免费", True, tokens=100000, model=WIRE)
        self.assertTrue(self.app.free_quota_exhausted(self.channel, WIRE))
        self.assertEqual(status["limit_tokens"], 500000)

    def test_explicit_channel_quota_overrides_baseline(self):
        raw = volc_config()
        raw["channels"][0]["free_quota"] = {"requests": 2}
        app = App(parse_config(raw), usage_file=self.path)
        app.providers = volc_registry()
        channel = app.config.channel("火山免费")
        app.usage.record("火山免费", True, tokens=900000, model=WIRE)
        status = app.free_quota_status(channel, WIRE)
        self.assertEqual(status["source"], "channel")
        self.assertEqual(status["limit_requests"], 2)
        self.assertFalse(status["exhausted"])  # tokens are uncapped here
        app.usage.record("火山免费", True, tokens=1, model=WIRE)
        self.assertTrue(app.free_quota_exhausted(channel, WIRE))

    def test_paid_channels_share_no_free_cap(self):
        raw = volc_config()
        raw["channels"][0]["tier"] = "paid"
        app = App(parse_config(raw), usage_file=self.path)
        app.providers = volc_registry()
        channel = app.config.channel("火山免费")
        app.usage.record("火山免费", True, tokens=900000, model=WIRE)
        self.assertIsNone(app.free_quota_status(channel, WIRE))

    def test_model_tier_override_decides_eligibility(self):
        # Paid model on a free channel: not capped. Free model on a paid
        # channel: capped by the vendor baseline.
        raw = volc_config()
        raw["channels"][0]["tiers"] = {WIRE: "paid", "old-doubao": "free"}
        app = App(parse_config(raw), usage_file=self.path)
        app.providers = volc_registry()
        channel = app.config.channel("火山免费")
        self.assertIsNone(app.free_quota_status(channel, WIRE))
        self.assertEqual(app.free_quota_status(channel, "old-doubao")["limit_tokens"],
                         500000)
        raw["channels"][0]["tier"] = "paid"
        raw["channels"][0]["tiers"] = {WIRE: "free"}
        app = App(parse_config(raw), usage_file=self.path)
        app.providers = volc_registry()
        app.usage.record("火山免费", True, tokens=600000, model=WIRE)
        self.assertTrue(app.free_quota_exhausted(
            app.config.channel("火山免费"), WIRE))

    def test_filter_drops_exhausted_candidate_without_fallback(self):
        self.app.usage.record("火山免费", True, tokens=600000, model=WIRE)
        kept, skipped = self.app._apply_free_quota_filter(
            [(self.channel, WIRE)], mock.Mock(exposed=""))
        self.assertEqual(kept, [])
        self.assertEqual(skipped[0]["reason"], "free_quota_exhausted")
        self.assertEqual(skipped[0]["channel"], "火山免费")

    def test_filter_keeps_healthy_candidate(self):
        self.app.usage.record("火山免费", True, tokens=1000, model=WIRE)
        kept, skipped = self.app._apply_free_quota_filter(
            [(self.channel, WIRE)], mock.Mock(exposed=""))
        self.assertEqual([wire for _ch, wire in kept], [WIRE])
        self.assertEqual(skipped, [])

    def test_budget_gate_closes_between_sweeps(self):
        # The last sweep reported 101k tokens left; the vendor snapshot is
        # hours old and Volcengine bills silently once quota runs dry, so
        # the extrapolated budget — not the stale snapshot or the generous
        # provider baseline — must decide.
        self.app._seed_free_budget({"火山免费": {WIRE: {
            "remaining": 101000, "total": 500000, "consumed": 399000}}})
        self.app.usage.record("火山免费", True, tokens=600, model=WIRE)
        status = self.app.free_quota_status(self.channel, WIRE)
        self.assertEqual(status["budget_remaining"], 100400)
        self.assertFalse(status["exhausted"])
        self.app.usage.record("火山免费", True, tokens=500, model=WIRE)
        status = self.app.free_quota_status(self.channel, WIRE)
        self.assertLessEqual(status["budget_remaining"], 100000)
        self.assertTrue(status["exhausted"])
        kept, skipped = self.app._apply_free_quota_filter(
            [(self.channel, WIRE)], mock.Mock(exposed=""))
        self.assertEqual(kept, [])
        self.assertEqual(skipped[0]["reason"], "free_quota_exhausted")

    def test_volcengine_reserve_closes_gate_above_zero(self):
        # One request can spend ~90k tokens, so the Volcengine gate must close
        # while 100k free tokens still remain — never at the actual zero.
        self.app._seed_free_budget({"火山免费": {WIRE: {"remaining": 150000}}})
        status = self.app.free_quota_status(self.channel, WIRE)
        self.assertEqual(status["reserve_tokens"], 100000)
        self.assertFalse(status["exhausted"])
        self.app.usage.record("火山免费", True, tokens=60000, model=WIRE)
        status = self.app.free_quota_status(self.channel, WIRE)
        self.assertEqual(status["budget_remaining"], 90000)
        self.assertTrue(status["exhausted"])  # quota left, but inside margin
        kept, skipped = self.app._apply_free_quota_filter(
            [(self.channel, WIRE)], mock.Mock(exposed=""))
        self.assertEqual(kept, [])
        self.assertEqual(skipped[0]["reason"], "free_quota_exhausted")

    def test_reserve_is_provider_scoped(self):
        # Providers without an explicit entry keep the shared 10k default:
        # below that a call fails outright anyway, so stopping early only
        # saves the doomed request and its retry.
        raw = volc_config()
        raw["channels"][0]["provider"] = "modelscope"
        app = App(parse_config(raw), usage_file=self.path)
        app.providers = volc_registry()
        channel = app.config.channel("火山免费")
        app._seed_free_budget({"火山免费": {WIRE: {"remaining": 40000}}})
        app.usage.record("火山免费", True, tokens=5000, model=WIRE)
        status = app.free_quota_status(channel, WIRE)
        self.assertEqual(status["reserve_tokens"], 10_000)
        self.assertEqual(status["budget_remaining"], 35000)
        self.assertFalse(status["exhausted"])

    def test_stale_sync_freezes_whole_channel(self):
        # The budget extrapolates safely only while the sweep that seeded it
        # keeps running: vendor numbers older than one sweep interval stop
        # every wire of the channel, because the grant may already be gone
        # and Volcengine would keep serving silently. (0.5h sweep + the
        # 2-minute grace = 1920s trip point.)
        self.app._seed_free_budget({"火山免费": {WIRE: {"remaining": 400000}}})
        self.app.vendor_synced["火山免费"] = time.time() - 2000
        status = self.app.free_quota_status(self.channel, WIRE)
        self.assertTrue(status["stale"])
        self.assertGreater(status["sync_age"], 1920)
        self.assertTrue(status["exhausted"])
        # Channel-wide by design: the sibling mapping freezes with the
        # drained model instead of picking up traffic during the stall.
        kept, _ = self.app._apply_free_quota_filter(
            [(self.channel, WIRE), (self.channel, "old-doubao")],
            mock.Mock(exposed=""))
        self.assertEqual(kept, [])
        # A fresh sweep stamp lifts the freeze immediately.
        self.app.vendor_synced["火山免费"] = time.time() - 60
        status = self.app.free_quota_status(self.channel, WIRE)
        self.assertFalse(status["stale"])
        self.assertFalse(status["exhausted"])

    def test_stale_freeze_needs_enabled_sweep_and_known_stamp(self):
        # Without a configured sweep there is nothing to refresh, so freezing
        # would disable the channel forever; without a stamp (first boot,
        # channels the sweep never touched) the numbers cannot be called
        # stale and the regular per-wire gate stays in charge.
        raw = volc_config()
        raw["defaults"] = {"free_align_hours": 0}
        disabled = App(parse_config(raw), usage_file=self.path)
        channel = disabled.config.channel("火山免费")
        disabled._seed_free_budget({"火山免费": {WIRE: {"remaining": 400000}}})
        disabled.vendor_synced["火山免费"] = time.time() - 9999
        status = disabled.free_quota_status(channel, WIRE)
        self.assertFalse(status["stale"])
        self.assertFalse(status["exhausted"])
        # Sweep enabled but never stamped yet: no freeze either.
        self.app._seed_free_budget({"火山免费": {WIRE: {"remaining": 400000}}})
        status = self.app.free_quota_status(self.channel, WIRE)
        self.assertIsNone(status["sync_age"])
        self.assertFalse(status["stale"])
        self.assertFalse(status["exhausted"])

    def test_reserve_applies_to_vendor_snapshot(self):
        # No seed yet (first sweep pending): the raw vendor remainder decides,
        # and the margin still holds.
        self.app.vendor_quota = {"火山免费": {WIRE: {
            "remaining": 80000, "total": 500000, "consumed": 420000}}}
        status = self.app.free_quota_status(self.channel, WIRE)
        self.assertIsNone(status["budget_remaining"])
        self.assertTrue(status["exhausted"])

    def test_reserve_cools_only_the_depleted_model(self):
        # The margin is per model, never channel-wide: one wire inside its
        # margin is refused while its sibling on the same channel keeps
        # serving. A whole-channel freeze would take headroom down with it.
        self.app._seed_free_budget({"火山免费": {
            WIRE: {"remaining": 90000},
            "old-doubao": {"remaining": 400000}}})
        kept, skipped = self.app._apply_free_quota_filter(
            [(self.channel, WIRE), (self.channel, "old-doubao")],
            mock.Mock(exposed=""))
        self.assertEqual([wire for _, wire in kept], ["old-doubao"])
        self.assertEqual(skipped[0]["wire_model"], WIRE)
        self.assertEqual(skipped[0]["reason"], "free_quota_exhausted")

    def test_budget_reseed_recovers_after_quota_refund(self):
        self.app._seed_free_budget({"火山免费": {WIRE: {"remaining": 100}}})
        self.app.usage.record("火山免费", True, tokens=200, model=WIRE)
        self.assertTrue(self.app.free_quota_exhausted(self.channel, WIRE))
        # Next sweep: the vendor restocked the free resource pack.
        self.app._seed_free_budget({"火山免费": {WIRE: {"remaining": 150000}}})
        status = self.app.free_quota_status(self.channel, WIRE)
        self.assertEqual(status["budget_remaining"], 150000)
        self.assertFalse(status["exhausted"])

    def test_budget_seed_skips_unparsable_rows(self):
        self.app._seed_free_budget({"火山免费": {WIRE: {"remaining": None}}})
        self.assertIsNone(self.app._free_budget_for(self.channel, WIRE))
        # No seed → the raw vendor snapshot still decides, as before.
        self.app.vendor_quota = {"火山免费": {WIRE: {"remaining": 0}}}
        self.assertTrue(self.app.free_quota_exhausted(self.channel, WIRE))

    def test_failed_sweep_keeps_previous_seed(self):
        self.app._seed_free_budget({"火山免费": {WIRE: {"remaining": 100}}})
        self.app.usage.record("火山免费", True, tokens=30, model=WIRE)
        # A later sweep that only reports other channels (this one failed)
        # must not wipe the seed: the quota really was spent.
        self.app._seed_free_budget({"other": {"m": {"remaining": 9}}})
        self.assertEqual(self.app._free_budget_for(self.channel, WIRE), 70.0)

    def test_payload_reports_exhausted_and_sync_warning(self):
        self.app.usage.record("火山免费", True, tokens=600000, model=WIRE)
        payload = maint.free_models_payload(self.app)
        entry = next(m for m in payload["channels"][0]["models"]
                     if m["model"] == WIRE)
        self.assertTrue(entry["quota"]["exhausted"])
        self.assertFalse(entry["routable"])
        self.assertIn("免费额度已用完", entry["skip_reason"])
        self.assertEqual(payload["summary"]["quota_exhausted"], 1)
        # Registry declares a free-quota command but no sweep has run yet.
        self.assertEqual(payload["channels"][0]["quota_sync"],
                         "尚未同步厂家免费额度：当前按基础额度 + 本地用量估算，"
                         "可能与实际用量不一致")

    def test_payload_marks_sync_failure(self):
        self.app.free_align_reports = [{
            "trigger": "scheduled",
            "alerts": [{"channel": "火山免费", "kind": "free_quota_cmd",
                        "message": "免费额度命令执行失败"}],
        }]
        payload = maint.free_models_payload(self.app)
        self.assertIn("同步失败", payload["channels"][0]["quota_sync"])

    def test_payload_carries_vendor_free_limits_and_counts(self):
        self.app.providers = [{
            "id": "volcengine", "name": "火山引擎方舟",
            "modes": {"payg": {
                "label": "按量付费", "protocol": "openai",
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "tier": "paid", "models": [],
                "docs_url": "https://www.volcengine.com/docs/82379",
                "free_quota_cmd": ["facet-ark-quota"],
                "free_limits": {"rpm": 40, "note": "免费层约 40 RPM"},
            }},
        }]
        payload = maint.free_models_payload(self.app)
        channel = payload["channels"][0]
        self.assertEqual(channel["provider_name"], "火山引擎方舟")
        self.assertEqual(channel["mode"], "payg")
        self.assertEqual(channel["docs_url"],
                         "https://www.volcengine.com/docs/82379")
        self.assertEqual(channel["free_limits"],
                         {"rpm": 40, "note": "免费层约 40 RPM"})
        # Gateway-side throttling stays distinct from the vendor's own caps.
        self.assertIsNone(channel["rate"]["rpm"])
        self.assertEqual(payload["summary"]["quota_unsynced"], 1)
        routable = [m for m in channel["models"] if m["routable"]]
        self.assertEqual(payload["summary"]["rate_limited"], len(routable))

    def test_payload_counts_quota_warning(self):
        self.app.usage.record("火山免费", True, tokens=400000, model=WIRE)
        payload = maint.free_models_payload(self.app)
        self.assertGreaterEqual(payload["summary"]["quota_warning"], 1)
        self.assertTrue(any((m["quota"] or {}).get("warning")
                            for m in payload["channels"][0]["models"]))

    def test_vendor_quota_visible_when_local_counter_is_zero(self):
        # The vendor dashboard says 19,307 tokens were consumed elsewhere;
        # the gateway never billed a single token for this model.
        self.app.vendor_quota = {"火山免费": {WIRE: {
            "remaining": 480693, "total": 500000, "consumed": 19307}}}
        self.app.vendor_quota_at = "2026-11-11 09:31:00"
        status = self.app.free_quota_status(self.channel, WIRE)
        self.assertEqual(status["used_tokens"], 0)
        self.assertEqual(status["vendor_remaining"], 480693)
        self.assertEqual(status["vendor_total"], 500000)
        self.assertEqual(status["vendor_consumed"], 19307)
        self.assertEqual(status["vendor_at"], "2026-11-11 09:31:00")
        # Ratio follows the vendor (19307/500000), not the empty local counter.
        self.assertEqual(status["ratio"], 0.0386)
        self.assertFalse(status["exhausted"])
        payload = maint.free_models_payload(self.app)
        entry = next(m for m in payload["channels"][0]["models"]
                     if m["model"] == WIRE)
        self.assertEqual(entry["quota"]["vendor_remaining"], 480693)
        self.assertEqual(entry["quota"]["vendor_consumed"], 19307)
        self.assertTrue(entry["routable"])

    def test_vendor_zero_remaining_blocks_routing(self):
        # Quota drained outside the gateway: local usage is still zero, yet
        # routing must stop instead of falling through to per-token billing.
        self.app.vendor_quota = {"火山免费": {WIRE: {
            "remaining": 0, "total": 500000, "consumed": 500000}}}
        self.app.vendor_quota_at = "2026-11-11 09:31:00"
        status = self.app.free_quota_status(self.channel, WIRE)
        self.assertEqual(status["used_tokens"], 0)
        self.assertTrue(status["exhausted"])
        self.assertEqual(status["vendor_remaining"], 0)
        kept, skipped = self.app._apply_free_quota_filter(
            [(self.channel, WIRE)], mock.Mock(exposed=""))
        self.assertEqual(kept, [])
        self.assertEqual(skipped[0]["reason"], "free_quota_exhausted")
        payload = maint.free_models_payload(self.app)
        entry = next(m for m in payload["channels"][0]["models"]
                     if m["model"] == WIRE)
        self.assertFalse(entry["routable"])
        self.assertEqual(payload["summary"]["quota_exhausted"], 1)

    def test_vendor_only_quota_reports_vendor_source(self):
        # A channel without registry baseline / channel cap still gets a
        # status when the vendor supplied numbers.
        raw = volc_config()
        app = App(parse_config(raw), usage_file=self.path)
        app.providers = [{"id": "volcengine", "name": "火山引擎方舟",
                          "modes": {"payg": {"label": "按量付费"}}}]
        channel = app.config.channel("火山免费")
        self.assertIsNone(app.free_quota_status(channel, WIRE))
        app.vendor_quota = {"火山免费": {WIRE: {
            "remaining": 480693, "total": 500000, "consumed": 19307}}}
        status = app.free_quota_status(channel, WIRE)
        self.assertEqual(status["source"], "vendor")
        self.assertIsNone(status["limit_tokens"])
        self.assertEqual(status["vendor_remaining"], 480693)


if __name__ == "__main__":
    unittest.main()
