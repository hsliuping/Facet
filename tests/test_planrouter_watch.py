"""Source watch (watch.py): diff rules, excerpts, dedup, persistence, API.

The watch only observes: every test here asserts that config and registry
data survive a sweep untouched.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from facet.gateway import watch as watch_mod
from facet.gateway.core import load_config, parse_config
from facet.gateway.server import App, PERSIST_KIND_LABELS


def watch_config() -> dict:
    key = "sk-" + "fixture"
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "defaults": {"watch_hours": 0},
        "channels": [
            {"name": "a", "protocol": "openai", "base_url": "http://127.0.0.1:9201",
             "api_key": key, "models": {"gpt-x": "gpt-x"},
             "provider": "volcengine", "mode": "coding"},
            {"name": "b", "protocol": "openai", "base_url": "http://127.0.0.1:9202",
             "api_key": key, "enabled": False, "models": {"gpt-y": "gpt-y"}},
            {"name": "claude", "protocol": "anthropic",
             "base_url": "http://127.0.0.1:9204", "models": {"auto": "claude-x"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["a"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
        ],
    }


def registry_fixture() -> list[dict]:
    return [{
        "id": "volcengine", "name": "火山引擎方舟",
        "modes": {
            "coding": {
                "label": "Coding 套餐", "protocol": "openai",
                "base_url": "https://ark.example.com/api/coding/v3",
                "docs_url": "https://docs.example.com/coding-plan",
                "models_url": "https://ark.example.com/api/coding/v3/models",
                "tier": "paid", "models": ["plan-a"],
            },
            "payg": {
                "label": "按量付费", "protocol": "openai",
                "base_url": "https://ark.example.com/api/v3",
                "tier": "paid", "models": [],
            },
        },
    }]


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, limit: int = -1):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_app(self) -> App:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                      encoding="utf-8")
    tmp.close()
    self.path = Path(tmp.name)
    self.path.write_text(json.dumps(watch_config()), encoding="utf-8")
    app = App(load_config(self.path), config_path=self.path)
    app.providers = registry_fixture()
    return app


def _snap(source, models, sha: str = "", head: str = "head") -> dict:
    """Deterministic snapshot: sha follows the model set like a real page."""
    return {"url": source["url"],
            "sha256": sha or "h-" + ",".join(models),
            "ok": True, "error": "", "models": models, "text_head": head}


class CollectSourcesTests(unittest.TestCase):
    def setUp(self):
        self.app = _make_app(self)

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        watch_mod.save_snapshots(self.app, {})

    def test_provider_modes_docs_and_models(self):
        sources = {s["key"]: s for s in watch_mod.collect_sources(self.app)}
        docs = sources.get("provider:volcengine:coding:docs")
        models = sources.get("provider:volcengine:coding:models")
        self.assertEqual(docs["url"], "https://docs.example.com/coding-plan")
        self.assertEqual(docs["kind"], "docs")
        self.assertEqual(models["kind"], "models")
        # The registry mode's endpoint is fetched with the linked channel's key.
        self.assertEqual(models["api_key"],
                         self.app.config.channels[0].api_key)
        # payg mode has no docs_url / models_url: nothing to watch there.
        self.assertNotIn("provider:volcengine:payg:docs", sources)
        self.assertNotIn("provider:volcengine:payg:models", sources)

    def test_channels_own_endpoints_with_protocol_auth(self):
        sources = {s["key"]: s for s in watch_mod.collect_sources(self.app)}
        chan = sources["channel:a"]
        self.assertEqual(chan["url"], "http://127.0.0.1:9201/v1/models")
        self.assertEqual(chan["protocol"], "openai")
        claude = sources["channel:claude"]
        self.assertEqual(claude["url"], "http://127.0.0.1:9204/v1/models")
        self.assertEqual(claude["protocol"], "anthropic")
        # Disabled channels stay out of the sweep.
        self.assertNotIn("channel:b", sources)

    def test_subscription_docs(self):
        self.app.subscriptions = [{"id": "s1", "plan": "Coding 套餐",
                                   "models_url": "https://docs.example.com/plan-doc"}]
        sources = {s["key"]: s for s in watch_mod.collect_sources(self.app)}
        self.assertEqual(sources["subscription:s1"]["kind"], "docs")
        self.assertEqual(sources["subscription:s1"]["url"],
                         "https://docs.example.com/plan-doc")

    def test_same_url_fetched_once(self):
        self.app.subscriptions = [{"id": "s1", "plan": "Coding 套餐",
                                   "models_url": "https://docs.example.com/coding-plan"}]
        sources = watch_mod.collect_sources(self.app)
        urls = [s["url"] for s in sources]
        self.assertEqual(urls.count("https://docs.example.com/coding-plan"), 1)
        self.assertNotIn("subscription:s1",
                         {s["key"] for s in sources})  # registry source wins


class SnapshotSourceTests(unittest.TestCase):
    def test_docs_source_extracts_models_and_excerpt(self):
        source = {"key": "k", "kind": "docs", "origin": "provider",
                  "label": "L", "url": "https://docs.example.com/x",
                  "api_key": "", "proxy": "", "protocol": "openai"}
        html = "<html><body><h1>套餐模型</h1><ul>" \
               "<li>glm-4.7</li><li>kimi-k2</li><li>deepseek-v3.2</li>" \
               "</ul></body></html>"
        with mock.patch("facet.gateway.maintenance.fetch_plan_doc",
                        return_value=html):
            snap = watch_mod.snapshot_source(source)
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["models"],
                         ["glm-4.7", "kimi-k2", "deepseek-v3.2"])
        self.assertLessEqual(len(snap["text_head"]), watch_mod.EXCERPT_MAX)
        self.assertEqual(snap["sha256"],
                         hashlib.sha256(html.encode("utf-8")).hexdigest())

    def test_models_json_source_sends_bearer_and_parses(self):
        captured = {}
        source = {"key": "k", "kind": "models", "origin": "channel",
                  "label": "L", "url": "http://up/v1/models",
                  "api_key": "sk-fix", "proxy": "", "protocol": "openai"}

        def fake_open(req, timeout, proxy):
            captured["auth"] = req.headers.get("Authorization")
            captured["url"] = req.full_url
            return _FakeResp(b'{"data":[{"id":"m1"},{"id":"m2"}]}')

        with mock.patch("facet.gateway.watch.open_with_proxy", fake_open):
            snap = watch_mod.snapshot_source(source)
        self.assertEqual(captured["auth"], "Bearer sk-fix")
        self.assertEqual(snap["models"], ["m1", "m2"])

    def test_models_json_anthropic_headers(self):
        source = {"key": "k", "kind": "models", "origin": "channel",
                  "label": "L", "url": "http://up/v1/models",
                  "api_key": "sk-fix", "proxy": "", "protocol": "anthropic"}

        def fake_open(req, timeout, proxy):
            return _FakeResp(b'{"data":[{"id":"m1"}]}')

        with mock.patch("facet.gateway.watch.open_with_proxy", fake_open):
            snap = watch_mod.snapshot_source(source)
        self.assertEqual(snap["models"], ["m1"])

    def test_fetch_error_is_recorded_not_raised(self):
        source = {"key": "k", "kind": "docs", "origin": "provider",
                  "label": "L", "url": "https://docs.example.com/x",
                  "api_key": "", "proxy": "", "protocol": "openai"}
        with mock.patch("facet.gateway.maintenance.fetch_plan_doc",
                        side_effect=urllib.error.URLError("boom")):
            snap = watch_mod.snapshot_source(source)
        self.assertFalse(snap["ok"])
        self.assertIn("URLError", snap["error"])
        self.assertEqual(snap["models"], [])


class DiffSnapshotTests(unittest.TestCase):
    def test_baseline_produces_no_events(self):
        new = {"sha256": "a", "models": ["m1"]}
        self.assertEqual(watch_mod.diff_snapshot(None, new, "docs"), [])

    def test_added_and_removed(self):
        old = {"sha256": "a", "models": ["m1", "m2"]}
        new = {"sha256": "b", "models": ["m2", "m3"]}
        events = watch_mod.diff_snapshot(old, new, "models")
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["new_model", "removed_model"])
        self.assertEqual(events[0]["detail"]["added"], ["m3"])
        self.assertEqual(events[1]["detail"]["removed"], ["m1"])

    def test_docs_sha_change_without_model_change_is_doc_updated(self):
        old = {"sha256": "a", "models": ["m1"]}
        new = {"sha256": "b", "models": ["m1"]}
        events = watch_mod.diff_snapshot(old, new, "docs")
        self.assertEqual([e["kind"] for e in events], ["doc_updated"])

    def test_models_json_sha_noise_is_ignored(self):
        old = {"sha256": "a", "models": ["m1", "m2"]}
        new = {"sha256": "b", "models": ["m2", "m1"]}
        self.assertEqual(watch_mod.diff_snapshot(old, new, "models"), [])

    def test_models_json_count_change_same_set_is_catalog_changed(self):
        old = {"sha256": "a", "models": ["m1", "m1"]}
        new = {"sha256": "b", "models": ["m1"]}
        events = watch_mod.diff_snapshot(old, new, "models")
        self.assertEqual([e["kind"] for e in events], ["catalog_changed"])


class EventKeyTests(unittest.TestCase):
    def test_same_models_same_key_regardless_of_order(self):
        e1 = {"kind": "new_model", "detail": {"added": ["a", "b"], "removed": []}}
        e2 = {"kind": "new_model", "detail": {"added": ["b", "a"], "removed": []}}
        self.assertEqual(watch_mod.event_key(e1, "src"),
                         watch_mod.event_key(e2, "src"))

    def test_different_kind_or_models_differ(self):
        e1 = {"kind": "new_model", "detail": {"added": ["a"], "removed": []}}
        e2 = {"kind": "removed_model", "detail": {"added": [], "removed": ["a"]}}
        e3 = {"kind": "new_model", "detail": {"added": ["a", "c"], "removed": []}}
        keys = {watch_mod.event_key(e, "src") for e in (e1, e2, e3)}
        self.assertEqual(len(keys), 3)
        self.assertNotEqual(watch_mod.event_key(e1, "src"),
                            watch_mod.event_key(e1, "other"))


class RunWatchTests(unittest.TestCase):
    def setUp(self):
        self.app = _make_app(self)

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        (self.path.parent / "change-snapshots.json").unlink(missing_ok=True)
        (self.path.parent / "change-events.jsonl").unlink(missing_ok=True)

    def test_first_run_builds_baseline_without_events(self):
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m1", "m2"])):
            report = watch_mod.run_watch(self.app)
        self.assertEqual(report["appended"], 0)
        self.assertFalse((self.path.parent / "change-events.jsonl").exists())
        snaps = watch_mod.load_snapshots(self.app)["snapshots"]
        self.assertEqual(snaps["channel:a"]["models"], ["m1", "m2"])

    def test_model_set_change_is_recorded_once(self):
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m1", "m3"] if s["key"] == "channel:a" else ["m1"])):
            watch_mod.run_watch(self.app)          # baseline
            report = watch_mod.run_watch(self.app)  # same content: no events
        self.assertEqual(report["appended"], 0)
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m2", "m3"] if s["key"] == "channel:a" else ["m1"])):
            changed = watch_mod.run_watch(self.app)
        # channel:a gains m2, loses m1: exactly two events, nothing else moved.
        self.assertEqual(changed["appended"], 2)
        events = watch_mod.load_events(self.app)
        self.assertEqual(sorted(e["kind"] for e in events),
                         ["new_model", "removed_model"])
        new_ev = next(e for e in events if e["kind"] == "new_model")
        self.assertEqual(new_ev["detail"]["added"], ["m2"])
        self.assertEqual(new_ev["source"]["key"], "channel:a")
        self.assertEqual(new_ev["status"], "unread")
        self.assertLessEqual(len(new_ev["evidence"]["excerpt"]),
                             watch_mod.EXCERPT_MAX)
        # Third sweep with the same content must not re-report the diff.
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m2", "m3"] if s["key"] == "channel:a" else ["m1"])):
            again = watch_mod.run_watch(self.app)
        self.assertEqual(again["appended"], 0)
        self.assertEqual(len(watch_mod.load_events(self.app)), 2)

    def test_failed_fetch_keeps_previous_models_and_marks_error(self):
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m1"])):
            watch_mod.run_watch(self.app)

        def broken(source):
            if source["key"] == "channel:a":
                return {"url": source["url"], "sha256": "", "ok": False,
                        "error": "URLError: boom", "models": [], "text_head": ""}
            return _snap(source, ["m1"])

        with mock.patch.object(watch_mod, "snapshot_source", broken):
            report = watch_mod.run_watch(self.app)
        self.assertEqual(report["error_sources"], 1)
        snaps = watch_mod.load_snapshots(self.app)["snapshots"]
        self.assertEqual(snaps["channel:a"]["models"], ["m1"])  # previous kept
        self.assertFalse(snaps["channel:a"]["ok"])
        self.assertTrue(snaps["channel:claude"]["ok"])

    def test_observational_config_untouched(self):
        raw_before = self.path.read_text(encoding="utf-8")
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["zzz"])):
            watch_mod.run_watch(self.app)
        self.assertEqual(self.path.read_text(encoding="utf-8"), raw_before)
        # And the app's in-memory config too (nothing auto-applied).
        self.assertEqual(self.app.config.channels[0].models,
                         {"gpt-x": "gpt-x"})


class ReadStateTests(unittest.TestCase):
    def setUp(self):
        self.app = _make_app(self)
        self.app.providers = []  # just the two channels, fewer moving parts

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        (self.path.parent / "change-snapshots.json").unlink(missing_ok=True)
        (self.path.parent / "change-events.jsonl").unlink(missing_ok=True)

    def test_mark_read_flips_status_in_payload(self):
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m1"])):
            watch_mod.run_watch(self.app)
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m2"])):
            watch_mod.run_watch(self.app)
        payload = self.app.changes_payload()
        self.assertEqual(len(payload["events"]), 4)  # 2 channels x add+remove
        self.assertEqual(payload["unread"], 4)
        event = payload["events"][0]
        self.assertEqual(event["status"], "unread")
        self.assertEqual(self.app.mark_changes_read([event["id"]])["changed"], 1)
        payload = self.app.changes_payload()
        self.assertEqual(payload["events"][0]["status"], "read")
        self.assertEqual(payload["unread"], 3)

    def test_readall_via_api_marks_everything(self):
        self.assertEqual(self.app.changes_payload()["events"], [])

    def test_changes_payload_lists_source_state(self):
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m1"])):
            watch_mod.run_watch(self.app)
        payload = self.app.changes_payload()
        self.assertEqual(payload["summary"]["total"], 2)
        self.assertEqual(payload["summary"]["ok"], 2)
        labels = {s["label"] for s in payload["sources"]}
        self.assertEqual(labels, {"渠道 a", "渠道 claude"})


class RunnerWiringTests(unittest.TestCase):
    def setUp(self):
        self.app = _make_app(self)

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        (self.path.parent / "change-snapshots.json").unlink(missing_ok=True)
        (self.path.parent / "change-events.jsonl").unlink(missing_ok=True)

    def test_watch_hours_parsed(self):
        raw = watch_config()
        raw["defaults"] = {"watch_hours": 0}
        self.assertEqual(parse_config(raw).defaults.watch_hours, 0)
        raw_unset = watch_config()
        raw_unset.pop("defaults")
        self.assertEqual(parse_config(raw_unset).defaults.watch_hours, 12)

    def test_task_registered_and_started(self):
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["defaults"]["watch_hours"] = 1
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        self.app = App(load_config(self.path), config_path=self.path)
        self.app.providers = registry_fixture()
        self.assertIsNone(self.app.watch_runner)
        self.app.start_background()
        self.app.stop_background()
        self.assertIsNotNone(self.app.watch_runner)
        self.assertGreater(self.app.watch_runner.next_run_at, 0)
        tasks = {t.get("key"): t for t in self.app.tasks_payload()["tasks"]}
        self.assertIn("watch", tasks)
        self.assertEqual(tasks["watch"]["label"], "信息源监视")

    def test_watch_disabled_when_zero(self):
        # watch_hours=0: the runner object exists (the task table still lists
        # it) but the loop never starts, so no sweep is scheduled.
        self.app.start_background()
        self.app.stop_background()
        self.assertIsNotNone(self.app.watch_runner)
        self.assertEqual(self.app.watch_runner.next_run_at, 0)

    def test_manual_run_uses_lock_and_reports_busy(self):
        with self.app.watch_lock:
            report = self.app.run_change_watch("manual")
        self.assertTrue(report["busy"])

    def test_manual_run_reports_trigger_and_summary(self):
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m1"])):
            report = self.app.run_change_watch("manual")
        self.assertEqual(report["trigger"], "manual")
        self.assertTrue(report["ok"])
        self.assertIn("监视", report["summary"])

    def test_persist_kind_label_registered(self):
        self.assertEqual(PERSIST_KIND_LABELS.get("change_log"),
                         "变更事件 change-events.jsonl")


if __name__ == "__main__":
    unittest.main()
