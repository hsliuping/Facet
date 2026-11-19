"""Source watch (watch.py): diff rules, excerpts, dedup, persistence, API.

The watch only observes: every test here asserts that config and registry
data survive a sweep untouched.
"""
from __future__ import annotations

import dataclasses
import hashlib
import http.client
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from facet.gateway import watch as watch_mod
from facet.gateway.core import load_config, parse_config
from facet.gateway.notifier import (ReminderService, change_digest,
                                    webhook_kind, webhook_payload)
from facet.gateway.server import App, PERSIST_KIND_LABELS


def watch_config() -> dict:
    key = "sk-" + "fixture"
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        # watch-only fixture: with both catalog sweeps off the watch keeps
        # covering channel endpoints (the dedup path has its own test).
        "defaults": {"watch_hours": 0, "catalog_refresh_hours": 0,
                     "maintenance_hours": 0},
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

    def test_channels_are_left_to_the_catalog_sweep(self):
        """A running catalog sweep already fetches every channel's models
        endpoint and reports its model-set diff; the watch must not fetch
        the same endpoints again (traffic) and double-report the changes."""
        defaults = dataclasses.replace(self.app.config.defaults,
                                       catalog_refresh_hours=24)
        self.app.config = dataclasses.replace(self.app.config, defaults=defaults)
        sources = {s["key"] for s in watch_mod.collect_sources(self.app)}
        self.assertFalse(any(k.startswith("channel:") for k in sources))
        # the registry and subscription layers are the watch's own job
        self.assertIn("provider:volcengine:coding:models", sources)
        # run_maintenance sweeps catalogs too — maintenance_hours alone
        # already leaves the channels to it
        defaults = dataclasses.replace(self.app.config.defaults,
                                       catalog_refresh_hours=0,
                                       maintenance_hours=24)
        self.app.config = dataclasses.replace(self.app.config, defaults=defaults)
        sources = {s["key"] for s in watch_mod.collect_sources(self.app)}
        self.assertFalse(any(k.startswith("channel:") for k in sources))
        # with both sweeps off the watch is the only observer
        defaults = dataclasses.replace(self.app.config.defaults,
                                       maintenance_hours=0)
        self.app.config = dataclasses.replace(self.app.config, defaults=defaults)
        sources = {s["key"] for s in watch_mod.collect_sources(self.app)}
        self.assertIn("channel:a", sources)

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

    def test_models_json_follows_pagination(self):
        # Bailian-style payload: the first page under-reports the advertised
        # total, so the watch must follow page_no/page_size like the registry
        # refresh does instead of watching a truncated catalog forever.
        source = {"key": "k", "kind": "models", "origin": "provider",
                  "label": "L", "url": "http://up/v1/models",
                  "api_key": "sk-fix", "proxy": "", "protocol": "openai"}

        def fake_open(req, timeout, proxy):
            if "page_no=2" in req.full_url:
                return _FakeResp(b'{"output":{"total":3,"models":[{"id":"m3"}]}}')
            return _FakeResp(b'{"output":{"total":3,'
                             b'"models":[{"id":"m1"},{"id":"m2"}]}}')

        with mock.patch("facet.gateway.watch.open_with_proxy", fake_open):
            snap = watch_mod.snapshot_source(source)
        self.assertEqual(snap["models"], ["m1", "m2", "m3"])

    def test_incomplete_read_is_recorded_not_raised(self):
        # http.client.IncompleteRead inherits HTTPException, not OSError —
        # the fetch boundary must still swallow it ("never raises" contract).
        source = {"key": "k", "kind": "models", "origin": "channel",
                  "label": "L", "url": "http://up/v1/models",
                  "api_key": "sk-fix", "proxy": "", "protocol": "openai"}
        with mock.patch("facet.gateway.watch.open_with_proxy",
                        side_effect=http.client.IncompleteRead(b"cut")):
            snap = watch_mod.snapshot_source(source)
        self.assertFalse(snap["ok"])
        self.assertIn("IncompleteRead", snap["error"])
        self.assertEqual(snap["models"], [])

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

    def test_failed_snapshot_without_models_rebaselines_silently(self):
        # A failed sweep with nothing inherited is not a baseline: the success
        # after it must not flood new_model events for the whole catalog.
        old = {"ok": False, "sha256": "", "models": []}
        new = {"ok": True, "sha256": "b", "models": ["m1", "m2"]}
        self.assertEqual(watch_mod.diff_snapshot(old, new, "docs"), [])

    def test_failed_snapshot_with_inherited_models_still_diffs(self):
        # A failure that inherited the last known catalog keeps reporting:
        # only the "never seen anything" case is a re-baseline.
        old = {"ok": False, "sha256": "a", "models": ["m1"]}
        new = {"ok": True, "sha256": "b", "models": ["m1", "m2"]}
        events = watch_mod.diff_snapshot(old, new, "docs")
        self.assertEqual([e["kind"] for e in events], ["new_model"])


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

    def test_incomplete_read_does_not_abort_the_sweep(self):
        # One exotic exception type must not kill the sweep or surface as an
        # unhandled API error: every source fails, the report still lands.
        exc = http.client.IncompleteRead(b"cut")
        with mock.patch("facet.gateway.watch.open_with_proxy",
                        side_effect=exc), \
                mock.patch("facet.gateway.maintenance.fetch_plan_doc",
                           side_effect=exc):
            report = watch_mod.run_watch(self.app)
        self.assertTrue(report["ok"])
        self.assertEqual(report["appended"], 0)
        self.assertGreater(report["sources"], 0)
        self.assertEqual(report["error_sources"], report["sources"])

    def test_flip_flop_does_not_re_report_recent_events(self):
        # A/B flip-flopping replays the exact same added+removed diff; the
        # retained key list (not just the newest key) must recognize it.
        def sweep(models_for_a):
            with mock.patch.object(
                    watch_mod, "snapshot_source",
                    lambda s: _snap(s, models_for_a
                                    if s["key"] == "channel:a" else ["m1"])):
                return watch_mod.run_watch(self.app)

        sweep(["m1"])                                    # baseline
        self.assertEqual(sweep(["m2"])["appended"], 2)   # +m2, -m1
        self.assertEqual(sweep(["m1"])["appended"], 2)   # +m1, -m2
        self.assertEqual(sweep(["m2"])["appended"], 0)   # keys still known
        self.assertEqual(len(watch_mod.load_events(self.app)), 4)

    def test_sweep_does_not_rollback_read_marks(self):
        # Events marked read during (or after) a sweep must survive the
        # sweep's snapshots write-back.
        def sweep(models_for_a):
            with mock.patch.object(
                    watch_mod, "snapshot_source",
                    lambda s: _snap(s, models_for_a
                                    if s["key"] == "channel:a" else ["m1"])):
                return watch_mod.run_watch(self.app)

        sweep(["m1"])
        sweep(["m2"])  # +m2, -m1 on channel:a
        events = self.app.changes_payload()["events"]
        self.assertTrue(events)
        self.app.mark_changes_read([e["id"] for e in events])
        sweep(["m2"])  # unchanged sweep; previously wiped the marks
        self.assertEqual(self.app.changes_payload()["unread"], 0)


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

    def test_mark_read_keeps_sweep_snapshots_intact(self):
        # mark_read rewrites only read_events onto the freshest on-disk
        # state: a concurrent sweep's snapshots and dedup keys survive.
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m1"])):
            watch_mod.run_watch(self.app)
        before = watch_mod.load_snapshots(self.app)["snapshots"]
        self.app.mark_changes_read(["does-not-exist"])
        after = watch_mod.load_snapshots(self.app)
        self.assertEqual(after["snapshots"], before)
        self.assertIn("does-not-exist", after["read_events"])

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


class MuteTests(unittest.TestCase):
    """按信息源静默：快照照常推进，只是不再产生事件。"""

    def setUp(self):
        self.app = _make_app(self)
        self.app.providers = []

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        (self.path.parent / "change-snapshots.json").unlink(missing_ok=True)
        (self.path.parent / "change-events.jsonl").unlink(missing_ok=True)

    def sweep(self, models):
        """models: a list for every source, or {source key: list} per source."""
        def snap(source):
            picked = models.get(source["key"], []) if isinstance(models, dict) else models
            return _snap(source, picked)
        with mock.patch.object(watch_mod, "snapshot_source", snap):
            return watch_mod.run_watch(self.app)

    def test_muted_source_advances_snapshot_without_events(self):
        both = {"channel:a": ["m1"], "channel:claude": ["m1"]}
        self.sweep(both)                                         # baseline
        self.assertEqual(self.app.set_changes_muted(["channel:a"])["changed"], 1)
        muted_delta = {"channel:a": ["m1", "m2"], "channel:claude": ["m1"]}
        changed = self.sweep(muted_delta)                        # muted delta
        self.assertEqual(changed["appended"], 0)
        # Snapshot still advanced: resuming reports only future changes.
        snaps = watch_mod.load_snapshots(self.app)["snapshots"]
        self.assertEqual(snaps["channel:a"]["models"], ["m1", "m2"])
        # Unmute: the swallowed delta does not flood back in.
        self.app.set_changes_muted(["channel:a"], muted=False)
        self.assertEqual(self.sweep(muted_delta)["appended"], 0)

    def test_unmuted_channel_keeps_reporting(self):
        self.sweep(["m1"])
        self.app.set_changes_muted(["channel:a"])
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m1", "m2"]
                                               if s["key"] == "channel:claude" else ["m1"])):
            report = watch_mod.run_watch(self.app)
        events = watch_mod.load_events(self.app)
        self.assertEqual(report["appended"], len(events))
        self.assertTrue(events)
        self.assertEqual({e["source"]["key"] for e in events}, {"channel:claude"})

    def test_payload_marks_muted_sources(self):
        self.sweep(["m1"])
        self.app.set_changes_muted(["channel:a"])
        payload = self.app.changes_payload()
        self.assertEqual(payload["summary"]["muted"], 1)
        states = {s["key"]: s for s in payload["sources"]}
        self.assertTrue(states["channel:a"]["muted"])
        self.assertFalse(states["channel:claude"]["muted"])

    def test_mute_flipped_during_sweep_survives_save(self):
        # A mute arriving while the sweep fetches must not be rolled back by
        # the sweep's write-back (same discipline as read marks).
        self.sweep(["m1"])

        def flipper(source):
            if source["key"] == "channel:claude":
                watch_mod.set_muted(self.app, ["channel:a"])
            return _snap(source, ["m1"])

        with mock.patch.object(watch_mod, "snapshot_source", flipper):
            watch_mod.run_watch(self.app)
        self.assertIn("channel:a", watch_mod.load_snapshots(self.app)["muted"])

    def test_empty_keys_rejected(self):
        with self.assertRaises(ValueError):
            self.app.set_changes_muted([])


class MapTests(unittest.TestCase):
    """新增模型一键映射：只建议目标渠道，写入始终走维护映射路径。"""

    def setUp(self):
        self.app = _make_app(self)

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        (self.path.parent / "change-snapshots.json").unlink(missing_ok=True)
        (self.path.parent / "change-events.jsonl").unlink(missing_ok=True)

    def _new_model_event(self) -> dict:
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m1"])):
            watch_mod.run_watch(self.app)
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m1", "m2"]
                                               if s["key"] == "channel:a" else ["m1"])):
            watch_mod.run_watch(self.app)
        events = self.app.changes_payload()["events"]
        return next(e for e in events if e["kind"] == "new_model"
                    and e["source"]["key"] == "channel:a")

    def test_map_targets_channel_and_provider(self):
        event = self._new_model_event()
        self.assertEqual(event["targets"], ["a"])
        # Provider-catalog source maps to channels bound to that provider,
        # with the matching billing mode ranked first.
        targets = watch_mod.map_targets(self.app, {
            "key": "provider:volcengine:coding:models", "origin": "provider"})
        self.assertEqual(targets, ["a"])
        self.assertEqual(watch_mod.map_targets(self.app, {
            "key": "channel:zz", "origin": "channel"}), [])

    def test_map_targets_subscription_uses_linked_channels(self):
        self.app.subscriptions = [{"id": "s1", "plan": "Coding 套餐",
                                   "channels": ["a"]}]
        self.assertEqual(watch_mod.map_targets(self.app, {
            "key": "subscription:s1", "origin": "subscription"}), ["a"])

    def test_map_change_models_writes_config_and_marks_read(self):
        event = self._new_model_event()
        result = self.app.map_change_models({"id": event["id"]})
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["channel"], "a")
        self.assertEqual(result["models"], ["m2"])
        # Verbatim identity mapping, exactly like maintenance suggestions.
        self.assertEqual(self.app.config.channels[0].models,
                         {"gpt-x": "gpt-x", "m2": "m2"})
        after = self.app.changes_payload()
        self.assertEqual(next(e for e in after["events"]
                              if e["id"] == event["id"])["status"], "read")

    def test_map_requires_known_event_and_new_model_kind(self):
        with self.assertRaises(ValueError):
            self.app.map_change_models({"id": "nope"})
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m1"])):
            watch_mod.run_watch(self.app)
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, [] if s["key"] == "channel:a" else ["m1"])):
            watch_mod.run_watch(self.app)
        removed = next(e for e in self.app.changes_payload()["events"]
                       if e["kind"] == "removed_model")
        with self.assertRaises(ValueError):
            self.app.map_change_models({"id": removed["id"], "channel": "a"})

    def test_map_without_target_channel_is_rejected(self):
        # An event whose source no longer maps to any channel must not guess.
        self._new_model_event()
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, ["m1", "m9"]
                                               if s["key"] == "channel:claude" else ["m1"])):
            watch_mod.run_watch(self.app)
        event = next(e for e in self.app.changes_payload()["events"]
                     if e["kind"] == "new_model"
                     and e["source"]["key"] == "channel:claude")
        self.assertEqual(event["targets"], ["claude"])
        with mock.patch.object(
                watch_mod, "map_targets",
                mock.Mock(return_value=[])):
            with self.assertRaises(ValueError):
                self.app.map_change_models({"id": event["id"]})


class ChangeNotifyTests(unittest.TestCase):
    """变更事件通知：一次扫描一条摘要，通过共享 webhook 发送。"""

    def setUp(self):
        self.app = _make_app(self)
        self.app.providers = []
        self.sent: list[dict] = []
        notifier = mock.Mock()
        notifier.notify_changes = mock.Mock(
            side_effect=self._capture)
        notifier.delivery = mock.Mock(return_value={
            "ok": True, "reason": "configured", "hint": ""})
        self.app.notifier = notifier

    def _capture(self, events):
        self.sent.append(events)
        return {"sent": 1, "events": len(events)}

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        (self.path.parent / "change-snapshots.json").unlink(missing_ok=True)
        (self.path.parent / "change-events.jsonl").unlink(missing_ok=True)

    def sweep(self, models):
        with mock.patch.object(watch_mod, "snapshot_source",
                               lambda s: _snap(s, models)):
            return watch_mod.run_watch(self.app)

    def test_sweep_with_events_sends_one_digest(self):
        self.sweep(["m1"])
        changed = self.sweep(["m1", "m2"])
        self.assertEqual(changed["notify"], {"sent": 1, "events": 2})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.sent[0]), 2)

    def test_sweep_without_events_does_not_notify(self):
        self.sweep(["m1"])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.sweep(["m1"])["notify"]["sent"], 0)

    def test_broken_notifier_does_not_break_the_sweep(self):
        self.sweep(["m1"])
        self.app.notifier.notify_changes = mock.Mock(
            side_effect=RuntimeError("webhook down"))
        report = self.sweep(["m1", "m2"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["appended"], 2)
        self.assertIn("error", report["notify"])
        self.assertIn("通知推送失败", report["summary"])

    def test_no_notifier_reports_unavailable(self):
        self.app.notifier = None
        payload = self.app.changes_payload()
        self.assertEqual(payload["notify"]["reason"], "unavailable")

    def test_payload_notify_reflects_delivery_state(self):
        self.sweep(["m1"])
        payload = self.app.changes_payload()
        self.assertTrue(payload["notify"]["ok"])


class ChangeDigestTests(unittest.TestCase):
    """webhook 载荷与摘要文本：按 host 识别平台，收件人无需改造。"""

    def test_webhook_kind_by_host(self):
        self.assertEqual(webhook_kind(
            "https://open.feishu.cn/open-apis/bot/v2/hook/x"), "feishu")
        self.assertEqual(webhook_kind(
            "https://open.larksuite.com/open-apis/bot/v2/hook/x"), "feishu")
        self.assertEqual(webhook_kind(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k"), "wecom")
        self.assertEqual(webhook_kind("https://ci.example.com/hook"), "generic")

    def test_payload_shapes(self):
        feishu = webhook_payload("https://open.feishu.cn/hook", "hello")
        self.assertEqual(feishu["msg_type"], "text")
        self.assertEqual(feishu["content"]["text"], "hello")
        wecom = webhook_payload("https://qyapi.weixin.qq.com/hook", "hello")
        self.assertEqual(wecom["msgtype"], "text")
        self.assertEqual(wecom["text"]["content"], "hello")
        # Unknown hosts keep the wecom-compatible envelope and namespace the
        # structured fields, so existing receivers never see a shape change.
        generic = webhook_payload("https://ci.example.com/hook", "hello",
                                  {"events": [{"kind": "new_model"}]})
        self.assertEqual(generic["msgtype"], "text")
        self.assertEqual(generic["planrouter"]["events"][0]["kind"], "new_model")
        self.assertNotIn("planrouter",
                         webhook_payload("https://ci.example.com/hook", "hi"))

    def test_digest_text_and_structured_events(self):
        text, structured = change_digest([
            {"kind": "new_model", "source": {"key": "channel:a", "label": "渠道 a"},
             "detail": {"added": [f"m{i}" for i in range(8)], "removed": []}},
            {"kind": "removed_model", "source": {"key": "channel:b", "label": "渠道 b"},
             "detail": {"added": [], "removed": ["m1"]}},
        ])
        self.assertIn("[PlanRouter 上游变更] 2 条", text)
        self.assertIn("[新增模型] 渠道 a：新增 m0、m1", text)
        self.assertIn("等 8 个", text)
        self.assertIn("[模型下架] 渠道 b：下架 m1", text)
        self.assertEqual(len(structured), 2)
        self.assertEqual(structured[0]["added"], [f"m{i}" for i in range(8)])

    def test_digest_long_list_is_trimmed(self):
        events = [{"kind": "new_model", "source": {"key": f"channel:{i}", "label": f"渠道 {i}"},
                   "detail": {"added": ["m"], "removed": []}} for i in range(15)]
        text, structured = change_digest(events)
        self.assertIn("另有 5 条", text)
        self.assertEqual(len(structured), 10)


class ReminderServiceNotifyTests(unittest.TestCase):
    """ReminderService.notify_changes：走真实 _post，校验平台回执。"""

    def _service(self, url: str, sender) -> ReminderService:
        return ReminderService(None, None, lambda: [],
                               lambda: {"notify_enabled": True, "webhook_url": url},
                               sender=sender)

    def test_notify_changes_posts_wecom_payload(self):
        posts = []

        def sender(request):
            posts.append(json.loads(request.data.decode("utf-8")))
            return _FakeResp(b'{"errcode":0}')

        service = self._service(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k", sender)
        result = service.notify_changes([{
            "kind": "new_model", "source": {"key": "channel:a", "label": "渠道 a"},
            "detail": {"added": ["m2"], "removed": []}}])
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["kind"], "wecom")
        self.assertEqual(posts[0]["msgtype"], "text")
        self.assertIn("新增模型", posts[0]["text"]["content"])

    def test_notify_changes_disabled_reports_reason(self):
        service = ReminderService(
            None, None, lambda: [], lambda: {"notify_enabled": False,
                                             "webhook_url": "https://x.example.com/hook"},
            sender=lambda request: _FakeResp(b""))
        self.assertEqual(service.notify_changes([{"kind": "new_model"}]),
                         {"sent": 0, "reason": "notify_disabled"})

    def test_platform_error_code_is_reported(self):
        # Feishu/WeCom answer HTTP 200 with a non-zero code for a broken bot
        # URL; without the check the console would claim delivery.
        def sender(request):
            return _FakeResp(b'{"code":19021,"msg":"invalid token"}')

        service = self._service("https://open.feishu.cn/open-apis/bot/v2/hook/x",
                                sender)
        result = service.notify_changes([{"kind": "new_model",
                                          "detail": {"added": ["m"], "removed": []}}])
        self.assertEqual(result["sent"], 0)
        self.assertIn("19021", result["error"])


if __name__ == "__main__":
    unittest.main()
