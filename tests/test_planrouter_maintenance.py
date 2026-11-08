"""Daily self-maintenance: analyze pass, LLM output parsing, apply flows."""
from __future__ import annotations

import dataclasses
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
        (self.path.parent / "maintenance_models.json").unlink(missing_ok=True)

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
        (self.path.parent / "maintenance_models.json").unlink(missing_ok=True)

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

    def test_read_jsonl_tail(self):
        path = self.path.parent / "maintenance.jsonl"
        filler = "x" * 200
        lines = [json.dumps({"i": i, "filler": filler}) for i in range(500)]
        tail, has_more = maint.read_jsonl_tail(path, 10)  # missing file
        self.assertEqual((tail, has_more), ([], False))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tail, has_more = maint.read_jsonl_tail(path, 10)
        self.assertTrue(has_more)
        self.assertEqual([json.loads(s)["i"] for s in tail], list(range(490, 500)))
        tail, has_more = maint.read_jsonl_tail(path, 1000)
        self.assertFalse(has_more)
        self.assertEqual(len(tail), 500)
        path.write_text("\n".join(lines), encoding="utf-8")  # no trailing newline
        tail, _ = maint.read_jsonl_tail(path, 3)
        self.assertEqual([json.loads(s)["i"] for s in tail], [497, 498, 499])

    def test_maintenance_log_payload_reads_file_tail(self):
        log = self.path.parent / "maintenance.jsonl"
        lines = [json.dumps({
            "ts": f"2026-01-0{i + 1}T10:00:00", "trigger": "scheduled",
            "finished_at": 1000.0 + i, "channels_total": 2,
            "applied": [], "suggestions": [], "alerts": [],
            "note": f"run-{i}",
        }, ensure_ascii=False) for i in range(7)]
        log.write_text("not-json\n" + "\n".join(lines) + "\n", encoding="utf-8")

        payload = self.app.maintenance_log_payload(3)
        self.assertEqual(payload["source"], "file")
        self.assertTrue(payload["has_more"])
        self.assertEqual([e["note"] for e in payload["logs"]],
                         ["run-6", "run-5", "run-4"])

        full = self.app.maintenance_log_payload(50)
        self.assertFalse(full["has_more"])
        self.assertEqual(len(full["logs"]), 7)  # broken line skipped, newest first
        self.assertEqual(full["logs"][0]["note"], "run-6")
        self.assertEqual(full["logs"][-1]["note"], "run-0")

    def test_maintenance_log_payload_memory_fallback(self):
        app = self.app
        app.store.record_catalog("a", ["gpt-x"], "http://upstream/v1")
        app.probe_channel_models = lambda payload: (
            {"ok": True, "name": payload.get("name"), "models": ["gpt-x"],
             "endpoint": "http://upstream/v1"})
        app.run_maintenance("manual")
        (self.path.parent / "maintenance.jsonl").unlink()
        payload = app.maintenance_log_payload(50)
        self.assertEqual(payload["source"], "memory")
        self.assertEqual(len(payload["logs"]), 1)
        self.assertEqual(payload["logs"][0]["trigger"], "manual")
        self.assertNotIn("patch", payload["logs"][0])


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

    def test_filters_dates_filenames_and_plan_names(self):
        text = ("- 更新日期 2026-11-04\n"
                "- 00-8\n"
                "- settings.json\n- node.js\n- coding-plan\n- glm-5.3\n")
        self.assertEqual(maint.extract_plan_models(text), ["glm-5.3"])

    def test_clean_plan_models_drops_letterless_ids(self):
        self.assertEqual(maint.clean_plan_models(["kimi-k3", "00-8", "", "kimi-k3"]),
                         ["kimi-k3"])

    def test_clean_plan_models_drops_harness_names(self):
        self.assertEqual(
            maint.clean_plan_models(["kimi-k3", "豆包搜索", "Agent 记忆",
                                     "Computer Use Agent", "doubao-seed-2.0-mini"]),
            ["kimi-k3", "doubao-seed-2.0-mini"])

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
        (self.path.parent / "maintenance_models.json").unlink(missing_ok=True)

    def patch_fetch(self, result):
        original = maint.fetch_plan_doc

        def fake(url, proxy=""):
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

    def test_llm_primary_updates_plan_models(self):
        # Even when the regex extractor would find list items, the LLM wins.
        self.patch_fetch("<html><body><ul>"
                         "<li>doubao-seed-2.1-pro</li></ul></body></html>")
        self.app.config = dataclasses.replace(
            self.app.config,
            defaults=dataclasses.replace(self.app.config.defaults,
                                         maintenance_model="gpt-x"))
        self.app._maintenance_llm_call = lambda model, prompt: '["kimi-k3.1-pro", "kimi-k3"]'
        report = self.app.run_maintenance("manual")
        self.assertEqual(self.app.subscriptions[0]["models"], ["kimi-k3.1-pro", "kimi-k3"])
        entry = next(e for e in report["applied"] if e["action"] == "update_plan_models")
        self.assertEqual(entry["source"], "llm")

    def test_llm_output_is_cleaned(self):
        self.patch_fetch("<html><body><ul>"
                         "<li>doubao-seed-2.1-pro</li></ul></body></html>")
        self.app.config = dataclasses.replace(
            self.app.config,
            defaults=dataclasses.replace(self.app.config.defaults,
                                         maintenance_model="gpt-x"))
        self.app._maintenance_llm_call = lambda model, prompt: '["kimi-k3", "00-8", "kimi-k3"]'
        self.app.run_maintenance("manual")
        self.assertEqual(self.app.subscriptions[0]["models"], ["kimi-k3"])

    def test_llm_failure_falls_back_to_regex(self):
        self.patch_fetch("<html><body><ul>"
                         "<li>doubao-seed-2.1-pro</li></ul></body></html>")
        self.app.config = dataclasses.replace(
            self.app.config,
            defaults=dataclasses.replace(self.app.config.defaults,
                                         maintenance_model="gpt-x"))

        def boom(model, prompt):
            raise RuntimeError("llm down")

        self.app._maintenance_llm_call = boom
        report = self.app.run_maintenance("manual")
        self.assertEqual(self.app.subscriptions[0]["models"], ["doubao-seed-2.1-pro"])
        entry = next(e for e in report["applied"] if e["action"] == "update_plan_models")
        self.assertEqual(entry["source"], "regex")
        self.assertIn("plan_models_llm", [a["kind"] for a in report["alerts"]])

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
        (self.path.parent / "maintenance_models.json").unlink(missing_ok=True)
        overlay = self.path.parent / "providers.json"
        overlay.unlink(missing_ok=True)
        Path(str(overlay) + ".bak").unlink(missing_ok=True)

    def patch_fetch(self, result):
        original = maint.fetch_plan_doc

        def fake(url, proxy=""):
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

    def test_llm_primary_for_provider_docs(self):
        # Even when the regex extractor would find list items, the LLM wins.
        self.patch_fetch("<html><body><ul><li>from-regex</li></ul></body></html>")
        self.app.config = dataclasses.replace(
            self.app.config,
            defaults=dataclasses.replace(self.app.config.defaults,
                                         maintenance_model="gpt-x"))
        self.app._maintenance_llm_call = lambda model, prompt: '["kimi-k3", "00-8"]'
        report = {"applied": [], "suggestions": [], "alerts": []}
        maint.refresh_providers(self.app, report)
        coding = self.app.providers[0]["modes"]["coding"]
        self.assertEqual(coding["models"], ["kimi-k3"])  # 00-8 dropped
        entry = next(e for e in report["applied"]
                     if e["action"] == "update_provider_models")
        self.assertEqual(entry["source"], "llm")
        self.assertTrue((self.path.parent / "providers.json").is_file())

    def test_llm_failure_falls_back_to_regex_for_provider_docs(self):
        self.patch_fetch("<html><body><ul><li>from-regex</li></ul></body></html>")
        self.app.config = dataclasses.replace(
            self.app.config,
            defaults=dataclasses.replace(self.app.config.defaults,
                                         maintenance_model="gpt-x"))

        def boom(model, prompt):
            raise RuntimeError("llm down")

        self.app._maintenance_llm_call = boom
        report = {"applied": [], "suggestions": [], "alerts": []}
        maint.refresh_providers(self.app, report)
        self.assertEqual(self.app.providers[0]["modes"]["coding"]["models"],
                         ["from-regex"])
        entry = next(e for e in report["applied"]
                     if e["action"] == "update_provider_models")
        self.assertEqual(entry["source"], "regex")
        self.assertIn("provider_models_llm",
                      [a["kind"] for a in report["alerts"]])

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


class ProviderModelsEndpointTests(unittest.TestCase):
    """refresh_providers can also pull model lists from models_url JSON."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.path.write_text(json.dumps(maintenance_config()), encoding="utf-8")
        self.app = App(load_config(self.path), config_path=self.path)
        vendor = providers_registry_fixture()
        vendor["modes"]["payg"]["models_url"] = "https://ark.example.com/api/v3/models"
        vendor["modes"]["payg"]["models"] = ["old-model"]
        self.app.providers = [vendor]
        self.app.subscriptions = []

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        Path(str(self.path) + ".maintenance.bak").unlink(missing_ok=True)
        (self.path.parent / "maintenance.jsonl").unlink(missing_ok=True)
        (self.path.parent / "maintenance_models.json").unlink(missing_ok=True)
        overlay = self.path.parent / "providers.json"
        overlay.unlink(missing_ok=True)
        Path(str(overlay) + ".bak").unlink(missing_ok=True)

    def patch_models_fetch(self, result):
        original = maint.fetch_models_json

        def fake(url, proxy="", api_key=""):
            if isinstance(result, Exception):
                raise result
            return result
        maint.fetch_models_json = fake
        self.addCleanup(setattr, maint, "fetch_models_json", original)

    def refresh(self):
        report = {"applied": [], "suggestions": [], "alerts": []}
        maint.refresh_providers(self.app, report)
        return report

    def test_parse_models_payload_supports_three_shapes(self):
        self.assertEqual(
            maint.parse_models_payload('{"data":[{"id":" b "},{"id":"a"}]}'),
            ["a", "b"])
        self.assertEqual(
            maint.parse_models_payload('{"models":["x", {"id":"y"}]}'),
            ["x", "y"])
        self.assertEqual(maint.parse_models_payload('["z", "a"]'), ["a", "z"])
        # empties dropped, non-string ids stringified, output sorted
        self.assertEqual(
            maint.parse_models_payload('{"data":[{"id":""}, 3, {"id":"a"}]}'),
            ["3", "a"])

    def test_parse_models_payload_rejects_bad_json_and_shapes(self):
        with self.assertRaises(ValueError):
            maint.parse_models_payload("{ not json")
        with self.assertRaises(ValueError):
            maint.parse_models_payload('{"object": 1}')
        with self.assertRaises(ValueError):
            maint.parse_models_payload('{"data": {"a": 1}}')

    def test_parse_models_payload_supports_bailian_output_shape(self):
        # Bailian GET /api/v1/models: {"output": {"total": N, "models": [...]}}
        # with per-item "model" ids.
        self.assertEqual(
            maint.parse_models_payload(
                '{"output":{"total":2,"models":[{"model":"qwen-plus"},'
                '{"model":"qwen-max"}]}}'),
            ["qwen-max", "qwen-plus"])
        self.assertEqual(
            maint.parse_models_payload(
                '{"output":{"models":[{"model":"a"},{"model":"a"}]}}'),
            ["a"])

    def test_fetch_models_json_sends_bearer_when_key_given(self):
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"{}"

        def fake_open(req, timeout, proxy=""):
            captured["timeout"] = timeout
            captured["headers"] = dict(req.headers)
            return FakeResp()

        original = maint.open_with_proxy
        maint.open_with_proxy = fake_open
        self.addCleanup(setattr, maint, "open_with_proxy", original)
        maint.fetch_models_json("https://api.example.com/models")
        self.assertNotIn("Authorization", captured["headers"])
        maint.fetch_models_json("https://api.example.com/models", api_key="sk-k")
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer sk-k")
        self.assertEqual(captured["timeout"], 10)

    def test_models_url_borrows_channel_key_and_proxy(self):
        data = maintenance_config()
        data["channels"][0]["provider"] = "volcengine"
        data["channels"][0]["proxy"] = "http://127.0.0.1:7890"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.app = App(load_config(self.path), config_path=self.path)
        vendor = providers_registry_fixture()
        vendor["modes"]["coding"]["docs_url"] = ""  # keep the test offline
        vendor["modes"]["payg"]["models_url"] = "https://ark.example.com/api/v3/models"
        self.app.providers = [vendor]
        self.app.subscriptions = []
        captured = []
        original = maint.fetch_models_json

        def fake(url, proxy="", api_key=""):
            captured.append((url, proxy, api_key))
            return '{"data":[{"id":"m1"}]}'

        maint.fetch_models_json = fake
        self.addCleanup(setattr, maint, "fetch_models_json", original)
        report = {"applied": [], "suggestions": [], "alerts": []}
        maint.refresh_providers(self.app, report)
        self.assertEqual(
            captured[0],
            ("https://ark.example.com/api/v3/models",
             "http://127.0.0.1:7890", "sk-fixture"))

    def test_models_url_prefers_channel_with_same_base_url(self):
        # Bailian payg (sk-...) and Token Plan (sk-sp-...) live on different
        # endpoints; the mode's own base_url must pick the matching channel.
        data = maintenance_config()
        data["channels"][0]["provider"] = "bailian"
        data["channels"][0]["base_url"] = (
            "https://dashscope.example.com/compatible-mode/v1")
        data["channels"][1]["provider"] = "bailian"
        data["channels"][1]["base_url"] = (
            "https://token-plan.example.com/compatible-mode/v1")
        data["channels"][1]["api_key"] = "sk-sp-plan"
        data["channels"][1]["proxy"] = "http://127.0.0.1:7890"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.app = App(load_config(self.path), config_path=self.path)
        vendor = providers_registry_fixture()
        vendor["id"] = "bailian"
        vendor["modes"]["coding"]["docs_url"] = ""  # keep the test offline
        vendor["modes"]["payg"]["models_url"] = ""  # only agent pulls here
        vendor["modes"]["agent"] = {
            "label": "Token Plan 套餐", "protocol": "openai",
            "base_url": "https://token-plan.example.com/compatible-mode/v1",
            "docs_url": "",
            "models_url":
                "https://token-plan.example.com/compatible-mode/v1/models",
            "tier": "paid", "models": [],
        }
        self.app.providers = [vendor]
        self.app.subscriptions = []
        captured = []
        original = maint.fetch_models_json

        def fake(url, proxy="", api_key=""):
            captured.append((url, proxy, api_key))
            return '{"data":[{"id":"m1"}]}'

        maint.fetch_models_json = fake
        self.addCleanup(setattr, maint, "fetch_models_json", original)
        report = {"applied": [], "suggestions": [], "alerts": []}
        maint.refresh_providers(self.app, report)
        self.assertEqual(
            captured,
            [("https://token-plan.example.com/compatible-mode/v1/models",
              "http://127.0.0.1:7890", "sk-sp-plan")])

    def test_models_url_follows_bailian_pagination(self):
        calls = []
        pages = {
            1: '{"output":{"total":3,"models":[{"model":"m-a"}]}}',
            2: '{"output":{"total":3,"models":[{"model":"m-b"}]}}',
            3: '{"output":{"total":3,"models":[{"model":"m-c"}]}}',
        }
        original = maint.fetch_models_json

        def fake(url, proxy="", api_key=""):
            calls.append(url)
            for page_no, body in pages.items():
                if f"page_no={page_no}" in url or (
                        page_no == 1 and "page_no=" not in url):
                    return body
            return '{"output":{"total":3,"models":[]}}'

        maint.fetch_models_json = fake
        self.addCleanup(setattr, maint, "fetch_models_json", original)
        report = {"applied": [], "suggestions": [], "alerts": []}
        vendor = self.app.providers[0]
        vendor["modes"]["coding"]["docs_url"] = ""  # keep the test offline
        changed = maint._refresh_provider_mode(
            self.app, vendor, "payg", vendor["modes"]["payg"], report)
        self.assertTrue(changed)
        self.assertEqual(vendor["modes"]["payg"]["models"],
                         ["m-a", "m-b", "m-c"])
        self.assertEqual(len(calls), 3)
        self.assertIn("page_no=2", calls[1])
        self.assertIn("page_size=100", calls[1])
        self.assertEqual(report["alerts"], [])

    def test_models_url_pagination_failure_keeps_old_list(self):
        original = maint.fetch_models_json

        def fake(url, proxy="", api_key=""):
            if "page_no=" in url:
                raise OSError("network down mid-paging")
            return '{"output":{"total":2,"models":[{"model":"m-a"}]}}'

        maint.fetch_models_json = fake
        self.addCleanup(setattr, maint, "fetch_models_json", original)
        report = {"applied": [], "suggestions": [], "alerts": []}
        vendor = self.app.providers[0]
        changed = maint._refresh_provider_mode(
            self.app, vendor, "payg", vendor["modes"]["payg"], report)
        self.assertFalse(changed)
        self.assertEqual(vendor["modes"]["payg"]["models"], ["old-model"])
        self.assertIn("provider_models_fetch",
                      [a["kind"] for a in report["alerts"]])
        self.assertIn("分页", report["alerts"][0]["message"])

    def test_models_url_refresh_updates_and_persists(self):
        self.patch_models_fetch(
            '{"data":[{"id":"kimi-k3"},{"id":"doubao-seed-2.1-pro"}]}')
        report = self.refresh()
        mode = self.app.providers[0]["modes"]["payg"]
        self.assertEqual(mode["models"],
                         ["doubao-seed-2.1-pro", "kimi-k3"])
        entry = next(e for e in report["applied"]
                     if e["action"] == "update_provider_models")
        self.assertEqual(entry["source"], "api")
        self.assertEqual(entry["mode"], "payg")
        self.assertIn("模型接口", entry["reason"])
        overlay = self.path.parent / "providers.json"
        data = json.loads(overlay.read_text(encoding="utf-8"))
        saved = next(p for p in data["providers"] if p["id"] == "volcengine")
        self.assertEqual(saved["modes"]["payg"]["models"],
                         ["doubao-seed-2.1-pro", "kimi-k3"])

    def test_models_url_json_failure_alerts_and_keeps_data(self):
        self.patch_models_fetch("not json at all")
        report = self.refresh()
        self.assertEqual(report["applied"], [])
        self.assertIn("provider_models_parse",
                      [a["kind"] for a in report["alerts"]])
        self.assertEqual(self.app.providers[0]["modes"]["payg"]["models"],
                         ["old-model"])
        self.assertFalse((self.path.parent / "providers.json").exists())

    def test_models_url_http_failure_alerts_and_keeps_data(self):
        self.patch_models_fetch(OSError("network down"))
        report = self.refresh()
        self.assertEqual(report["applied"], [])
        self.assertIn("provider_models_fetch",
                      [a["kind"] for a in report["alerts"]])
        self.assertIn("火山引擎方舟", report["alerts"][0]["message"])
        self.assertEqual(self.app.providers[0]["modes"]["payg"]["models"],
                         ["old-model"])

    def test_empty_models_payload_alerts(self):
        self.patch_models_fetch('{"data": []}')
        report = self.refresh()
        self.assertEqual(report["applied"], [])
        self.assertIn("provider_models_empty",
                      [a["kind"] for a in report["alerts"]])
        self.assertEqual(self.app.providers[0]["modes"]["payg"]["models"],
                         ["old-model"])

    def test_docs_url_takes_precedence_over_models_url(self):
        original_doc = maint.fetch_plan_doc
        original_api = maint.fetch_models_json
        vendor = self.app.providers[0]
        vendor["modes"]["coding"]["models_url"] = "https://ark.example.com/api/v3/models"

        def fake_doc(url, proxy=""):
            return "<html><body><ul><li>from-doc</li></ul></body></html>"

        def fake_api(url, proxy="", api_key=""):  # must never be called while docs_url wins
            raise AssertionError("models_url fetch must not run")

        maint.fetch_plan_doc = fake_doc
        maint.fetch_models_json = fake_api
        self.addCleanup(setattr, maint, "fetch_plan_doc", original_doc)
        self.addCleanup(setattr, maint, "fetch_models_json", original_api)
        report = self.refresh()
        self.assertEqual(vendor["modes"]["coding"]["models"], ["from-doc"])
        self.assertEqual(vendor["modes"]["payg"]["models"], ["old-model"])
        self.assertEqual(
            [e["mode"] for e in report["applied"]], ["coding"])

    def test_vendor_id_limits_refresh(self):
        second = providers_registry_fixture()
        second["id"] = "second-vendor"
        second["name"] = "第二厂家"
        self.app.providers = [self.app.providers[0], second]
        original_doc = maint.fetch_plan_doc

        def fake_doc(url, proxy=""):
            return "<html><body><ul><li>from-doc</li></ul></body></html>"

        maint.fetch_plan_doc = fake_doc
        self.addCleanup(setattr, maint, "fetch_plan_doc", original_doc)
        report = {"applied": [], "suggestions": [], "alerts": []}
        maint.refresh_providers(self.app, report, vendor_id="second-vendor")
        self.assertEqual([e["vendor"] for e in report["applied"]], ["第二厂家"])
        self.assertEqual(self.app.providers[0]["modes"]["coding"]["models"],
                         ["old-model"])
        self.assertEqual(second["modes"]["coding"]["models"], ["from-doc"])
        overlay = self.path.parent / "providers.json"
        data = json.loads(overlay.read_text(encoding="utf-8"))
        self.assertEqual([p["id"] for p in data["providers"]],
                         ["second-vendor"])


class MaintenanceModelPoolTests(unittest.TestCase):
    """Verified maintenance-model queue: quiz, probe, pick and runtime drop."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.path.write_text(json.dumps(maintenance_config()), encoding="utf-8")
        self.app = App(load_config(self.path), config_path=self.path)

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        (self.path.parent / "maintenance_models.json").unlink(missing_ok=True)

    @staticmethod
    def quiz_reply() -> str:
        return json.dumps(sorted(maint.MAINTENANCE_QUIZ_ANSWER))

    def test_quiz_requires_the_exact_answer_set(self):
        ok, parsed = maint.check_maintenance_quiz(self.quiz_reply())
        self.assertTrue(ok)
        self.assertEqual(len(parsed), len(maint.MAINTENANCE_QUIZ_ANSWER))
        short = sorted(maint.MAINTENANCE_QUIZ_ANSWER)[:-1]
        self.assertFalse(maint.check_maintenance_quiz(json.dumps(short))[0])
        leaky = sorted(maint.MAINTENANCE_QUIZ_ANSWER) + ["gpt-9-fake"]
        self.assertFalse(maint.check_maintenance_quiz(json.dumps(leaky))[0])
        self.assertFalse(maint.check_maintenance_quiz("抱歉，无法提取")[0])
        self.assertFalse(maint.check_maintenance_quiz(None)[0])

    def test_pool_roundtrip_and_broken_file_recovery(self):
        self.app.maintenance_pool["queue"] = ["m-a"]
        maint.save_maintenance_pool(self.app)
        self.assertEqual(maint.load_maintenance_pool(self.app)["queue"], ["m-a"])
        pool_path = self.path.parent / "maintenance_models.json"
        pool_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(maint.load_maintenance_pool(self.app)["queue"], [])

    def test_probe_keeps_only_perfect_scorers_and_persists(self):
        self.app._maintenance_llm_call = lambda model, prompt: (
            self.quiz_reply() if model == "good-model" else "胡言乱语")
        report = maint.probe_maintenance_models(
            self.app, candidates=["good-model", "bad-model"])
        self.assertEqual(report["queue"], ["good-model"])
        self.assertEqual(len(report["results"]), 2)
        self.assertEqual(maint.load_maintenance_pool(self.app)["queue"],
                         ["good-model"])

    def make_channel_free(self, name: str) -> None:
        """Channel is frozen; rebuild the config with one tier="free" channel."""
        self.app.config = dataclasses.replace(
            self.app.config,
            channels=[dataclasses.replace(c, tier="free") if c.name == name
                      else c for c in self.app.config.channels])

    def test_free_candidates_come_from_free_tier_catalogs(self):
        self.make_channel_free("a")
        self.app.store.record_catalog("a", ["x:free", "y:free", "paid-model"],
                                      "http://upstream/v1")
        self.assertEqual(maint.free_model_candidates(self.app, limit=5),
                         ["x:free", "y:free"])

    def test_free_candidates_cover_wholly_free_catalogs(self):
        """A free channel whose catalog has no :free ids donates every model."""
        self.make_channel_free("a")
        self.app.store.record_catalog(
            "a", ["doubao-seed-lite", "doubao-seed-pro"], "http://upstream/v1")
        self.assertEqual(maint.free_model_candidates(self.app, limit=5),
                         ["doubao-seed-lite", "doubao-seed-pro"])

    def test_free_candidates_fall_back_to_channel_mapping(self):
        """No catalog yet: the channel's own model mapping donates ids."""
        self.make_channel_free("b")
        self.assertEqual(maint.free_model_candidates(self.app, limit=5),
                         ["gpt-x"])

    def test_pool_empty_triggers_probe_and_fills_queue(self):
        self.make_channel_free("a")
        self.app.store.record_catalog("a", ["probe:free"], "http://upstream/v1")
        self.app._maintenance_llm_call = lambda model, prompt: self.quiz_reply()
        models, mode = maint.pick_maintenance_models(self.app)
        self.assertEqual((mode, models), ("pool", ["probe:free"]))

    def test_probe_cooldown_prevents_probing_storms(self):
        self.app.maintenance_pool["last_probe_at"] = time.time()
        calls: list[str] = []

        def llm(model, prompt):
            calls.append(model)
            raise AssertionError("probe should be suppressed by cooldown")
        self.app._maintenance_llm_call = llm
        models, mode = maint.pick_maintenance_models(self.app)
        self.assertEqual((models, mode), ([], "pool"))
        self.assertEqual(calls, [])

    def test_extraction_uses_queue_and_drops_failed_models(self):
        self.app.maintenance_pool["queue"] = ["dead-model", "good-model"]
        calls: list[str] = []

        def llm(model, prompt):
            calls.append(model)
            if model == "dead-model":
                raise RuntimeError("404 model not found")
            return json.dumps(["kimi-k3", "glm-5.3"])
        self.app._maintenance_llm_call = llm
        report = {"alerts": [], "applied": []}
        models, source = maint.extract_doc_models(
            self.app, "<ul><li>kimi-k3</li><li>glm-5.3</li></ul>",
            "doc", "plan_models_llm", report)
        self.assertEqual((source, models), ("llm", ["kimi-k3", "glm-5.3"]))
        self.assertEqual(calls, ["dead-model", "good-model"])
        self.assertEqual(self.app.maintenance_pool["queue"], ["good-model"])

    def test_explicit_model_list_is_used_verbatim(self):
        self.app.config = dataclasses.replace(
            self.app.config,
            defaults=dataclasses.replace(self.app.config.defaults,
                                         maintenance_model="m-1, m-2"))
        self.app._maintenance_llm_call = lambda model, prompt: (
            json.dumps(["a"]) if model == "m-1" else json.dumps(["b"]))
        reply, model = self.app._maintenance_llm_call_any("p")
        self.assertEqual((model, json.loads(reply)), ("m-1", ["a"]))
        # fixed mode never touches the queue even when a candidate fails
        def llm(model, prompt):
            if model == "m-1":
                raise RuntimeError("boom")
            return json.dumps(["kimi-k3"])
        self.app._maintenance_llm_call = llm
        reply, model = self.app._maintenance_llm_call_any("p")
        self.assertEqual(model, "m-2")
        self.assertEqual(self.app.maintenance_pool["queue"], [])

    def test_pool_mode_drops_models_that_fail_validation(self):
        self.app.maintenance_pool["queue"] = ["m-1", "m-2"]
        self.app._maintenance_llm_call = lambda model, prompt: (
            "抱歉，我无法输出 JSON" if model == "m-1"
            else json.dumps(["kimi-k3"]))
        reply, model = self.app._maintenance_llm_call_any(
            "p", validate=lambda r: bool(maint.parse_plan_models_reply(r)))
        self.assertEqual(model, "m-2")
        self.assertEqual(self.app.maintenance_pool["queue"], ["m-2"])
        dropped = self.app.maintenance_pool["results"][0]
        self.assertEqual((dropped["model"], dropped["ok"]), ("m-1", False))

    def test_maintenance_dispatch_prefers_free_channel(self):
        """Route membership is irrelevant: the id's owning channel wins,
        a tier-free owner first; branded ids fall back to the loopback."""
        self.make_channel_free("a")
        self.app.config.channels[0].models["m-shared"] = "m-shared"
        self.app.config.channels[1].models["m-shared"] = "m-shared"
        self.assertEqual(self.app._maintenance_channel_for("m-shared").name, "a")
        self.assertEqual(self.app._maintenance_channel_for("gpt-x").name, "b")
        self.assertIsNone(self.app._maintenance_channel_for("facet-auto"))

    def test_maintenance_direct_call_targets_owning_channel(self):
        """A mapped upstream id is posted straight to its channel's endpoint."""
        channel = self.app.config.channels[1]  # "b", openai
        captured: dict = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": "ok"}}]}).encode()

        def fake_open(req, timeout, proxy=""):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            captured["auth"] = req.headers.get("Authorization")
            captured["timeout"] = timeout
            return FakeResp()

        with mock.patch("planrouter.server.open_with_proxy", fake_open):
            reply = self.app._maintenance_direct_call(channel, "gpt-x", "p")
        self.assertEqual(reply, "ok")
        self.assertEqual(captured["url"],
                         "http://127.0.0.1:9202/v1/chat/completions")
        self.assertEqual(captured["body"]["model"], "gpt-x")
        self.assertIn(channel.api_key, captured["auth"])
        self.assertEqual(captured["timeout"],
                         max(60, self.app.config.defaults.timeout))


if __name__ == "__main__":
    unittest.main()
