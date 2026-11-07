"""Daily self-maintenance: analyze pass, LLM output parsing, apply flows."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from planrouter import maintenance as maint
from planrouter.core import load_config, parse_config
from planrouter.server import App


def maintenance_config() -> dict:
    key = "sk-" + "fixture"
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "a", "protocol": "openai", "base_url": "http://127.0.0.1:9201",
             "api_key": key,
             "models": {"gpt-old": "gpt-old", "auto": "gpt-old",
                        "gpt-keep": "gpt-keep"}},
            {"name": "b", "protocol": "openai", "base_url": "http://127.0.0.1:9202",
             "api_key": key, "models": {"gpt-x": "gpt-x"}},
            {"name": "claude", "protocol": "anthropic",
             "base_url": "http://127.0.0.1:9204", "models": {"auto": "claude-x"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["a", "b"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
        ],
    }


def catalog(models: list[str], removed: list[str] | None = None,
            added: list[str] | None = None) -> dict:
    return {"fetched_at": 1000.0, "endpoint": "http://upstream/v1",
            "models": models,
            "changes": {"removed": removed or [], "added": added or []}}


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.config = parse_config(maintenance_config())

    def run_analyze(self, catalogs, states=None, refresh=None):
        return maint.analyze(self.config, catalogs, states or {}, refresh)

    def test_removed_mapping_is_cleaned_and_auto_repointed(self):
        report = self.run_analyze({"a": catalog(["gpt-keep"], removed=["gpt-old"])})
        self.assertEqual(report["patch"],
                         {"a": {"gpt-keep": "gpt-keep", "auto": "gpt-keep"}})
        actions = [(e["channel"], e["action"]) for e in report["applied"]]
        self.assertIn(("a", "remove_mappings"), actions)
        self.assertIn(("a", "repoint_auto"), actions)
        repoint = next(e for e in report["applied"] if e["action"] == "repoint_auto")
        self.assertEqual((repoint["from"], repoint["to"]), ("gpt-old", "gpt-keep"))

    def test_all_models_removed_becomes_alert(self):
        models = self.config.channels[0].models
        models.clear()
        models["gpt-old"] = "gpt-old"
        report = self.run_analyze({"a": catalog(["gpt-new"], removed=["gpt-old"])})
        self.assertEqual(report["patch"], {})
        kinds = [a["kind"] for a in report["alerts"]]
        self.assertIn("all_models_removed", kinds)

    def test_added_models_become_add_model_suggestions(self):
        report = self.run_analyze(
            {"a": catalog(["gpt-old", "gpt-keep", "gpt-new"],
                          added=["gpt-new", "gpt-old"])})
        kinds = [(s["kind"], s["channel"]) for s in report["suggestions"]]
        self.assertIn(("add_model", "a"), kinds)
        suggestion = next(s for s in report["suggestions"] if s["kind"] == "add_model")
        self.assertEqual(suggestion["models"], ["gpt-new"])  # gpt-old already mapped

    def test_empty_catalog_becomes_alert(self):
        report = self.run_analyze({"a": catalog([])})
        kinds = [a["kind"] for a in report["alerts"]]
        self.assertIn("empty_catalog", kinds)

    def test_auth_failure_on_refresh_becomes_alert(self):
        report = self.run_analyze(
            {"a": catalog(["gpt-old", "gpt-keep"])},
            refresh=[{"ok": False, "name": "b", "http_status": 401}])
        auth = [a for a in report["alerts"] if a["kind"] == "auth"]
        self.assertEqual(len(auth), 1)
        self.assertEqual(auth[0]["channel"], "b")

    def test_quota_cooldown_without_rate_suggests_rate_limit(self):
        states = {"a": {"cooling": True, "reason": "insufficient_quota"}}
        report = self.run_analyze({"a": catalog(["gpt-old", "gpt-keep"])}, states)
        kinds = [s["kind"] for s in report["suggestions"]]
        self.assertIn("set_rate", kinds)

    def test_missing_catalog_is_skipped(self):
        report = self.run_analyze({"a": {"models": ["gpt-old"]}})
        self.assertEqual(report, {"applied": [], "suggestions": [],
                                  "alerts": [], "patch": {}})


class ParseLlmOutputTests(unittest.TestCase):
    def test_extracts_json_from_prose(self):
        text = ('好的，以下是我的分析：\n'
                '{"notes": ["渠道 a 需要关注"], "suggestions": '
                '[{"channel": "a", "message": "建议加映射", "action": "add_model"}]}')
        parsed = maint.parse_llm_output(text)
        self.assertEqual(parsed["notes"], ["渠道 a 需要关注"])
        self.assertEqual(parsed["suggestions"], [{
            "kind": "add_model", "channel": "a",
            "message": "建议加映射", "source": "llm"}])

    def test_plain_text_becomes_a_note(self):
        parsed = maint.parse_llm_output("看起来一切正常")
        self.assertEqual(parsed["notes"], ["看起来一切正常"])
        self.assertEqual(parsed["suggestions"], [])

    def test_broken_json_falls_back_to_note(self):
        parsed = maint.parse_llm_output('结论：{"notes": [oops]')
        self.assertEqual(parsed["notes"], ['结论：{"notes": [oops]'])

    def test_long_values_are_truncated(self):
        parsed = maint.parse_llm_output(json.dumps({"notes": ["x" * 500]}))
        self.assertEqual(len(parsed["notes"][0]), 300)

    def test_empty_input(self):
        self.assertEqual(maint.parse_llm_output(""), {"notes": [], "suggestions": []})
        self.assertEqual(maint.parse_llm_output(None), {"notes": [], "suggestions": []})


class ApplySuggestionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.path.write_text(json.dumps(maintenance_config()), encoding="utf-8")
        self.app = App(load_config(self.path), config_path=self.path)

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        Path(str(self.path) + ".maintenance.bak").unlink(missing_ok=True)

    def test_add_model_updates_config_file(self):
        result = self.app.apply_maintenance_suggestion(
            {"channel": "b", "models": ["gpt-new"]})
        self.assertEqual(result, {"ok": True, "channel": "b", "models": ["gpt-new"]})
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        models = next(c["models"] for c in raw["channels"] if c["name"] == "b")
        self.assertEqual(models, {"gpt-x": "gpt-x", "gpt-new": "gpt-new"})

    def test_accepts_single_model_string(self):
        self.app.apply_maintenance_suggestion({"channel": "b", "models": "gpt-new"})
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        models = next(c["models"] for c in raw["channels"] if c["name"] == "b")
        self.assertIn("gpt-new", models)

    def test_unknown_channel_raises(self):
        with self.assertRaises(ValueError):
            self.app.apply_maintenance_suggestion(
                {"channel": "ghost", "models": ["m"]})

    def test_invalid_body_raises(self):
        with self.assertRaises(ValueError):
            self.app.apply_maintenance_suggestion({"channel": "b"})
        with self.assertRaises(ValueError):
            self.app.apply_maintenance_suggestion({"channel": "b", "models": []})
        with self.assertRaises(ValueError):
            self.app.apply_maintenance_suggestion("nope")


class RunMaintenanceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.path.write_text(json.dumps(maintenance_config()), encoding="utf-8")
        self.app = App(load_config(self.path), config_path=self.path)

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        Path(str(self.path) + ".maintenance.bak").unlink(missing_ok=True)
        (self.path.parent / "maintenance.jsonl").unlink(missing_ok=True)

    def test_run_applies_catalog_confirmed_changes(self):
        app = self.app
        app.store.record_catalog("a", ["gpt-old", "gpt-keep"], "http://upstream/v1")
        app.store.record_catalog("b", ["gpt-x"], "http://upstream/v1")

        def fake_probe(payload):
            name = payload.get("name") if isinstance(payload, dict) else payload
            models = ["gpt-keep"] if name == "a" else ["gpt-x"]
            app.store.record_catalog(name, models, "http://upstream/v1")
            return {"ok": True, "name": name, "models": models,
                    "endpoint": "http://upstream/v1"}

        app.probe_channel_models = fake_probe  # no network in tests
        report = app.run_maintenance("manual")

        self.assertEqual(report["trigger"], "manual")
        actions = [(e["channel"], e["action"]) for e in report["applied"]]
        self.assertIn(("a", "remove_mappings"), actions)
        self.assertIn(("a", "repoint_auto"), actions)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        models = next(c["models"] for c in raw["channels"] if c["name"] == "a")
        self.assertNotIn("gpt-old", models)
        self.assertEqual(models.get("auto"), "gpt-keep")
        self.assertTrue(Path(str(self.path) + ".maintenance.bak").is_file())

        payload = app.maintenance_payload()
        self.assertEqual(len(payload["reports"]), 1)
        self.assertNotIn("patch", payload["reports"][0])
        self.assertEqual(payload["reports"][0]["applied"], report["applied"])
        self.assertFalse(payload["llm_available"])  # maintenance_model not set

    def test_run_records_jsonl_log(self):
        app = self.app
        app.store.record_catalog("a", ["gpt-old", "gpt-keep"], "http://upstream/v1")
        app.probe_channel_models = lambda payload: (
            {"ok": True, "name": payload.get("name"), "models": ["gpt-old", "gpt-keep"],
             "endpoint": "http://upstream/v1"})
        app.run_maintenance("manual")
        log = self.path.parent / "maintenance.jsonl"
        self.assertTrue(log.is_file())
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["trigger"], "manual")


if __name__ == "__main__":
    unittest.main()
