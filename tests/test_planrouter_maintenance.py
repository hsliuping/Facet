"""Daily self-maintenance: analyze pass, LLM output parsing, apply flows."""
from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from planrouter import maintenance as maint
from planrouter import providers as providers_mod
from planrouter import subscriptions as subs
from planrouter.core import load_config, parse_config
from planrouter.server import App


def providers_registry_fixture() -> dict:
    """One fake vendor entry with a coding mode exposing a docs_url."""
    return {
        "id": "volcengine", "name": "火山引擎方舟",
        "modes": {
            "coding": {
                "label": "Coding 套餐", "protocol": "openai",
                "base_url": "https://ark.example.com/api/coding/v3",
                "docs_url": "https://docs.example.com/coding-plan",
                "tier": "paid", "models": ["old-model"],
            },
            "payg": {
                "label": "按量付费", "protocol": "openai",
                "base_url": "https://ark.example.com/api/v3",
                "docs_url": "", "tier": "paid", "models": [],
            },
        },
    }


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
        self.app.providers = []  # provider doc refresh is covered elsewhere

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


PLAN_DOC_MARKDOWN = """## 支持的模型

Coding Plan 个人版当前支持以下模型：

- `doubao-seed-evolving`（默认模型）
- `doubao-seed-2.1-pro`
- `doubao-seed-2.1-lite`
- `doubao-seed-2.0-mini`
- `glm-5.3`（别名 `glm-latest`）
- `glm-5.3-flash`
- `deepseek-v4.1-flash`
- `deepseek-v4.1-flash-economy`
- `deepseek-v4-flash`
- `deepseek-v4-pro`
- `kimi-k2.8-preview`
- `kimi-k3`
- `kimi-k3.1-pro`

* 完整说明见 https://www.volcengine.com/docs/85508/1434618

以上内容摘自官方文档，仅供参考。
"""

PLAN_DOC_MODELS = [
    "doubao-seed-evolving", "doubao-seed-2.1-pro", "doubao-seed-2.1-lite",
    "doubao-seed-2.0-mini", "glm-5.3", "glm-latest", "glm-5.3-flash",
    "deepseek-v4.1-flash", "deepseek-v4.1-flash-economy", "deepseek-v4-flash",
    "deepseek-v4-pro", "kimi-k2.8-preview", "kimi-k3", "kimi-k3.1-pro",
]


class ExtractPlanModelsTests(unittest.TestCase):
    def test_extracts_models_from_volcengine_style_markdown(self):
        self.assertEqual(maint.extract_plan_models(PLAN_DOC_MARKDOWN), PLAN_DOC_MODELS)

    def test_ignores_plain_lines_and_urls(self):
        text = ("普通文字行没有清单。\n"
                "访问 https://www.volcengine.com/docs 获取帮助\n"
                "- https://only-an-url.example.com/a/b\n"
                "* 无分隔符的 plainword 不算\n")
        self.assertEqual(maint.extract_plan_models(text), [])

    def test_html_to_text_strips_script_style_and_tags(self):
        raw = ("<html><head><style>.x{color:red}</style></head><body>"
               "<h1>Coding Plan</h1><ul><li>glm-5.3</li><li>kimi-k3</li></ul>"
               "<script>var token=1;</script><p>A &amp; B</p></body></html>")
        text = maint.html_to_text(raw)
        self.assertNotIn("var token", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("<li>", text)
        self.assertIn("A & B", text)
        self.assertEqual(maint.extract_plan_models(text), ["glm-5.3", "kimi-k3"])

    def test_parse_plan_models_reply(self):
        self.assertEqual(maint.parse_plan_models_reply('["a-b", "c.d"]'), ["a-b", "c.d"])
        self.assertEqual(maint.parse_plan_models_reply('答案：["a-b"] 以上'), ["a-b"])
        self.assertEqual(maint.parse_plan_models_reply("抱歉，无法解析"), [])
        self.assertEqual(maint.parse_plan_models_reply(""), [])
        self.assertEqual(maint.parse_plan_models_reply(None), [])


class RefreshPlanModelsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.path.write_text(json.dumps(maintenance_config()), encoding="utf-8")
        self.app = App(load_config(self.path), config_path=self.path)
        self.app.probe_channel_models = lambda payload: {"ok": False, "name": payload.get("name")}
        self.app.providers = []  # provider doc refresh is covered elsewhere
        self.app.subscriptions = [subs.normalize({
            "vendor": "火山引擎", "plan": "coding plan", "channels": ["a"],
            "models_url": "https://docs.example.com/coding-plan",
            "models": ["old-model"],
        })]

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        Path(str(self.path) + ".maintenance.bak").unlink(missing_ok=True)
        (self.path.parent / "maintenance.jsonl").unlink(missing_ok=True)

    def patch_fetch(self, result):
        original = maint.fetch_plan_doc

        def fake(url):
            if isinstance(result, Exception):
                raise result
            return result
        maint.fetch_plan_doc = fake
        self.addCleanup(setattr, maint, "fetch_plan_doc", original)

    def test_run_maintenance_updates_plan_models_from_doc(self):
        self.patch_fetch("<html><body><ul>"
                         "<li>doubao-seed-2.1-pro</li>"
                         "<li>doubao-seed-2.0-mini</li>"
                         "</ul></body></html>")
        report = self.app.run_maintenance("manual")
        record = self.app.subscriptions[0]
        self.assertEqual(record["models"], ["doubao-seed-2.1-pro", "doubao-seed-2.0-mini"])
        entry = next(e for e in report["applied"] if e["action"] == "update_plan_models")
        self.assertEqual(entry["source"], "regex")
        self.assertEqual((entry["added"], entry["removed"]), (2, 1))
        self.assertEqual(entry["vendor"], "火山引擎")
        self.assertEqual(entry["plan"], "coding plan")
        self.assertIn("新增 2 个", entry["reason"])
        self.assertIn("来源 regex", entry["reason"])
        log = self.path.parent / "maintenance.jsonl"
        line = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        self.assertIn("update_plan_models", [e["action"] for e in line["applied"]])

    def test_fetch_failure_alerts_and_keeps_old_models(self):
        self.patch_fetch(OSError("network down"))
        report = self.app.run_maintenance("manual")
        self.assertEqual(self.app.subscriptions[0]["models"], ["old-model"])
        alert = next(a for a in report["alerts"] if a["kind"] == "plan_models_fetch")
        self.assertIn("火山引擎", alert["message"])
        self.assertIn("coding plan", alert["message"])

    def test_llm_fallback_updates_when_regex_extraction_empty(self):
        self.patch_fetch("<html><body><p>套餐包含 kimi-k3.1-pro 等模型</p></body></html>")
        self.app.config = dataclasses.replace(
            self.app.config,
            defaults=dataclasses.replace(self.app.config.defaults,
                                         maintenance_model="gpt-x"))
        self.app._maintenance_llm_call = lambda model, prompt: '["kimi-k3.1-pro", "kimi-k3"]'
        report = self.app.run_maintenance("manual")
        self.assertEqual(self.app.subscriptions[0]["models"], ["kimi-k3.1-pro", "kimi-k3"])
        entry = next(e for e in report["applied"] if e["action"] == "update_plan_models")
        self.assertEqual(entry["source"], "llm")

    def test_llm_unparsable_reply_alerts_and_keeps_old_models(self):
        self.patch_fetch("<html><body><p>没有可提取的清单</p></body></html>")
        self.app.config = dataclasses.replace(
            self.app.config,
            defaults=dataclasses.replace(self.app.config.defaults,
                                         maintenance_model="gpt-x"))
        self.app._maintenance_llm_call = lambda model, prompt: "抱歉，我无法完成该任务"
        report = self.app.run_maintenance("manual")
        self.assertEqual(self.app.subscriptions[0]["models"], ["old-model"])
        self.assertIn("plan_models_llm", [a["kind"] for a in report["alerts"]])

    def test_no_change_is_silent(self):
        self.patch_fetch("<html><body><ul><li>old-model</li></ul></body></html>")
        report = self.app.run_maintenance("manual")
        self.assertEqual(self.app.subscriptions[0]["models"], ["old-model"])
        self.assertEqual([e for e in report["applied"]
                          if e["action"] == "update_plan_models"], [])


class RefreshProvidersTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.path.write_text(json.dumps(maintenance_config()), encoding="utf-8")
        self.app = App(load_config(self.path), config_path=self.path)
        self.app.providers = [providers_registry_fixture()]
        self.app.subscriptions = []

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        Path(str(self.path) + ".maintenance.bak").unlink(missing_ok=True)
        (self.path.parent / "maintenance.jsonl").unlink(missing_ok=True)
        overlay = self.path.parent / "providers.json"
        overlay.unlink(missing_ok=True)
        Path(str(overlay) + ".bak").unlink(missing_ok=True)

    def patch_fetch(self, result):
        original = maint.fetch_plan_doc

        def fake(url):
            if isinstance(result, Exception):
                raise result
            return result
        maint.fetch_plan_doc = fake
        self.addCleanup(setattr, maint, "fetch_plan_doc", original)

    def test_updates_models_and_writes_overlay(self):
        self.patch_fetch("<html><body><ul>"
                         "<li>doubao-seed-2.1-pro</li>"
                         "<li>kimi-k3</li>"
                         "</ul></body></html>")
        report = {"applied": [], "suggestions": [], "alerts": []}
        maint.refresh_providers(self.app, report)
        coding = self.app.providers[0]["modes"]["coding"]
        self.assertEqual(coding["models"], ["doubao-seed-2.1-pro", "kimi-k3"])
        entry = next(e for e in report["applied"]
                     if e["action"] == "update_provider_models")
        self.assertEqual((entry["added"], entry["removed"]), (2, 1))
        self.assertEqual(entry["vendor"], "火山引擎方舟")
        self.assertEqual(entry["mode"], "coding")
        self.assertIn("新增 2 个", entry["reason"])
        self.assertIn("火山引擎方舟", entry["reason"])
        overlay = self.path.parent / "providers.json"
        self.assertTrue(overlay.is_file())
        data = json.loads(overlay.read_text(encoding="utf-8"))
        saved = next(p for p in data["providers"] if p["id"] == "volcengine")
        self.assertEqual(saved["modes"]["coding"]["models"],
                         ["doubao-seed-2.1-pro", "kimi-k3"])
        # Reloaded registry (builtin + overlay) sees the refreshed list too.
        reloaded = providers_mod.load_providers(self.path.parent)
        saved2 = next(p for p in reloaded if p["id"] == "volcengine")
        self.assertEqual(saved2["modes"]["coding"]["models"],
                         ["doubao-seed-2.1-pro", "kimi-k3"])

    def test_empty_extraction_alerts_and_keeps_data(self):
        self.patch_fetch("<html><body><p>页面里没有模型清单</p></body></html>")
        report = {"applied": [], "suggestions": [], "alerts": []}
        maint.refresh_providers(self.app, report)
        self.assertEqual(report["applied"], [])
        self.assertIn("provider_models_empty",
                      [a["kind"] for a in report["alerts"]])
        self.assertIn("火山引擎方舟", report["alerts"][0]["message"])
        self.assertEqual(self.app.providers[0]["modes"]["coding"]["models"],
                         ["old-model"])
        self.assertFalse((self.path.parent / "providers.json").exists())

    def test_fetch_failure_alerts_and_keeps_data(self):
        self.patch_fetch(OSError("network down"))
        report = {"applied": [], "suggestions": [], "alerts": []}
        maint.refresh_providers(self.app, report)
        self.assertEqual(report["applied"], [])
        self.assertIn("provider_models_fetch",
                      [a["kind"] for a in report["alerts"]])
        self.assertEqual(self.app.providers[0]["modes"]["coding"]["models"],
                         ["old-model"])
        self.assertFalse((self.path.parent / "providers.json").exists())

    def test_no_change_is_silent(self):
        self.patch_fetch("<html><body><ul><li>old-model</li></ul></body></html>")
        report = {"applied": [], "suggestions": [], "alerts": []}
        maint.refresh_providers(self.app, report)
        self.assertEqual(report["applied"], [])
        self.assertEqual(report["alerts"], [])
        self.assertFalse((self.path.parent / "providers.json").exists())


if __name__ == "__main__":
    unittest.main()
