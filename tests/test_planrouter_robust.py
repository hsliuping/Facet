"""Batch-6 robustness fixes: lock discipline, failure isolation, i18n, UI parity.

Covers:
- persistence.capture takes the store lock; flush survives capture errors (H1)
- freealign keeps the original mapping when a shared free-quota command fails (H2)
- notifier loop survives check() exceptions and reports them (M2)
- /api/changes/run answers 200 with busy:true so the console can show it (M3)
- subscriptions remind_days=0 means "remind on the last day", not 7 (M5)
- benchmarks REPO_DATA points at the repo registry dir; entries without an
  id are skipped instead of crashing the compare table (M6, L10)
- _probe_free_quota applies the same per-provider reserve as the sweep (M7)
- zhipu coding-plan always reports auto_renew as a bool (L11)
- source-level assertions for the mechanical fixes (M1, M4, L1-L9, L12, M8)
"""
from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from facet.gateway import benchmarks as bench_mod
from facet.gateway import freealign
from facet.gateway import notifier as notifier_mod
from facet.gateway import persistence as persistence_mod
from facet.gateway import subscriptions as subs
from facet.gateway import zhipu_quota
from facet.gateway.core import load_config, parse_config
from facet.gateway.server import App, Handler

SRC = Path(__file__).resolve().parents[1] / "src" / "facet" / "gateway"
STATIC = SRC / "static"
DOCS = Path(__file__).resolve().parents[1] / "docs"
FUTURE = "2099-12-31"


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
            {"name": "claude-x", "protocol": "anthropic",
             "base_url": "https://api.anthropic.com", "api_key": key,
             "models": {"claude-sonnet-x": "claude-sonnet-x"}, "priority": 90},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["百炼免费"]},
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
    }]


def bl_rows() -> list[dict]:
    # 27b has 5 tokens left: inside the 10k reserve it must never enter the
    # mapping, with or without an explicit reserve.
    return [
        {"model": "qwen3.8-flash", "type": "Text", "remaining": 20000,
         "total": 500000, "consumed": 480000, "expires": FUTURE},
        {"model": "qwen3.8-27b", "type": "Text", "remaining": 5,
         "total": 500000, "consumed": 499995, "expires": FUTURE},
    ]


class CountingLock:
    def __init__(self):
        self.acquires = 0

    def __enter__(self):
        self.acquires += 1
        return self

    def __exit__(self, *exc):
        return False


def fake_state(**overrides) -> object:
    fields = {
        "cooldown_until": 0.0, "model_cooldown_until": {}, "reason": "",
        "last_error": "", "consecutive_errors": 0, "half_open": False,
        "total_requests": 0, "served_requests": 0, "failed_requests": 0,
        "total_tokens": 0, "handoffs": 0, "last_used": 0.0, "quota": {},
        "latency_ewma": None,
    }
    fields.update(overrides)
    fields["req_times"] = []
    fields["tok_events"] = []
    return type("FakeState", (), fields)()


class LockStore:
    """Minimal store with the attributes capture() iterates."""

    def __init__(self, lock=None):
        self.lock = lock
        self.states = {"a": fake_state()}
        self.catalogs = {}
        self.rr_cursor = {}
        self.scores = {}


class PersistenceLockTests(unittest.TestCase):
    def test_capture_takes_store_lock_when_present(self):
        lock = CountingLock()
        store = LockStore(lock=lock)
        snap = persistence_mod.capture(store, now=1000.0)
        self.assertEqual(lock.acquires, 1)
        self.assertEqual(snap["channels"]["a"]["cooldown_until"], 0.0)

    def test_capture_works_without_lock(self):
        snap = persistence_mod.capture(LockStore(lock=None), now=1000.0)
        self.assertIn("a", snap["channels"])

    def test_flush_reports_capture_failure_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            errors = []
            p = persistence_mod.StatePersistence(
                path, lambda: LockStore(), interval=60.0,
                on_error=lambda kind, exc: errors.append((kind, exc)))
            with mock.patch.object(persistence_mod, "capture",
                                   side_effect=RuntimeError("boom")):
                p.flush()  # must not raise
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0][0], "state")
            self.assertIsInstance(errors[0][1], RuntimeError)
            self.assertFalse(path.exists())

    def test_loop_survives_capture_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            errors = []
            calls = {"n": 0}
            real_capture = persistence_mod.capture

            def flaky_capture(store, now=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("boom")
                return real_capture(store, now)

            p = persistence_mod.StatePersistence(
                path, lambda: LockStore(), interval=0.05,
                on_error=lambda kind, exc: errors.append((kind, exc)))
            with mock.patch.object(persistence_mod, "capture", new=flaky_capture):
                p.start()
                time.sleep(0.3)
                p.stop()
            self.assertTrue(any(e[0] == "state" for e in errors))
            self.assertTrue(path.is_file())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("channels", data)


class FreeAlignFailureTests(unittest.TestCase):
    def _app(self, raw=None):
        raw = raw or align_config()
        app = App(parse_config(raw))
        app.providers = registry_fixture()
        return app

    def test_failed_command_keeps_mapping(self):
        def run(cmd, timeout=120, extra_env=None):
            raise RuntimeError("CLI 不可用")

        app = self._app()
        report = freealign.run_free_align(app, "manual", _run_cmd=run)
        self.assertEqual(len(report["alerts"]), 1)
        self.assertEqual(report["patch"], {})
        channel = app.config.channel("百炼免费")
        self.assertEqual(channel.models.get("old-paid"), "old-paid")
        self.assertEqual(channel.models.get("qwen3.8-flash"), "qwen3.8-flash")

    def test_shared_failure_marks_later_channels_skipped(self):
        """One command, two channels on the same (cmd, creds): the first
        failure alerts, the sibling is skipped — not aligned to an empty
        mapping that would wipe auto pointing at the paid fallback."""
        seen = []

        def run(cmd, timeout=120, extra_env=None):
            seen.append(extra_env)
            raise RuntimeError("CLI 不可用")

        raw = align_config()
        dup = dict(raw["channels"][0])
        dup["name"] = "百炼免费B"
        raw["channels"].insert(1, dup)
        raw["credentials"] = {"bailian": {"BL_TOKEN": "abc"}}
        app = self._app(raw)
        report = freealign.run_free_align(app, "manual", _run_cmd=run)
        self.assertEqual(len(seen), 1)  # shared command runs once
        self.assertEqual(len(report["alerts"]), 1)
        self.assertEqual(report["patch"], {})
        skipped = [s["channel"] for s in report["skipped"]]
        self.assertIn("百炼免费B", skipped)
        for entry in report["aligned"]:
            self.assertNotEqual(entry["channel"], "百炼免费B")

    def test_success_still_runs_once_for_shared_channels(self):
        seen = []

        def run(cmd, timeout=120, extra_env=None):
            seen.append(extra_env)
            return json.dumps(bl_rows())

        raw = align_config()
        dup = dict(raw["channels"][0])
        dup["name"] = "百炼免费B"
        raw["channels"].insert(1, dup)
        raw["credentials"] = {"bailian": {"BL_TOKEN": "abc"}}
        app = self._app(raw)
        report = freealign.run_free_align(app, "manual", _run_cmd=run)
        self.assertEqual(len(seen), 1)
        self.assertEqual(report["alerts"], [])
        self.assertEqual(report["quota"]["百炼免费"], report["quota"]["百炼免费B"])


class FakeStopEvent:
    """Programmable threading.Event so the loop runs without real waits."""

    def __init__(self, schedule):
        self.schedule = list(schedule)
        self.waits: list[float | None] = []

    def set(self):
        self.schedule = [True]

    def is_set(self):
        return not self.schedule

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.schedule.pop(0) if self.schedule else True


class NotifierLoopTests(unittest.TestCase):
    def test_loop_survives_check_exception(self):
        errors = []
        stop = FakeStopEvent([False, False])  # initial wait, then one cycle
        boom = RuntimeError("check boom")
        svc = notifier_mod.ReminderService(
            None, None,
            get_records=lambda: [], get_settings=lambda: {"interval_minutes": 1},
            on_write_error=lambda kind, exc: errors.append((kind, exc)))
        svc._stop = stop  # swap before start: no real sleeps, no global patch
        svc.check = mock.Mock(side_effect=boom)
        svc.check_runtime = mock.Mock()
        svc.start()
        svc._thread.join(timeout=5)
        self.assertFalse(svc._thread.is_alive())
        self.assertIn(("notifier", boom), errors)
        # The loop ran more than one cycle: the crash reported and the thread
        # kept ticking until the stop event fired.
        self.assertGreaterEqual(svc.check_runtime.call_count, 2)


class ChangesRunApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "router.json"
        self.config_path.write_text(json.dumps(align_config()), encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(load_config(self.config_path),
                             config_path=self.config_path)  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_busy_reports_200_with_busy_flag(self):
        self.httpd.app.run_change_watch = lambda trigger: {
            "busy": True, "trigger": trigger, "summary": "已有巡检在跑"}
        status, body = self.call("POST", "/api/changes/run")
        self.assertEqual(status, 200)
        self.assertTrue(body["busy"])

    def test_idle_reports_200(self):
        self.httpd.app.run_change_watch = lambda trigger: {
            "trigger": trigger, "events": [], "ok": True}
        status, body = self.call("POST", "/api/changes/run")
        self.assertEqual(status, 200)
        self.assertFalse(body.get("busy", False))


class SubsRemindDaysTests(unittest.TestCase):
    def test_zero_remind_days_is_not_swallowed(self):
        expires = (date.today() + timedelta(days=3)).isoformat()
        rec = {"vendor": "x", "account": "a", "plan": "p",
               "expires_at": expires, "remind_days": 0}
        self.assertEqual(subs.enrich(rec)["status"], "active")

    def test_default_remind_days_still_expiring(self):
        expires = (date.today() + timedelta(days=3)).isoformat()
        rec = {"vendor": "x", "account": "a", "plan": "p", "expires_at": expires}
        self.assertEqual(subs.enrich(rec)["status"], "expiring")

    def test_zero_reminds_on_last_day(self):
        expires = date.today().isoformat()
        rec = {"vendor": "x", "account": "a", "plan": "p",
               "expires_at": expires, "remind_days": 0}
        self.assertEqual(subs.enrich(rec)["status"], "expiring")


class BenchmarksPathTests(unittest.TestCase):
    def test_repo_data_points_at_registry(self):
        self.assertEqual(bench_mod.REPO_DATA,
                         bench_mod.DATA.parents[2] / "registry")
        self.assertTrue(bench_mod.REPO_DATA.is_dir())
        self.assertTrue((bench_mod.REPO_DATA / "benchmarks.json").is_file())


class BenchmarksPayloadTests(unittest.TestCase):
    def setUp(self):
        bench_mod.payload.cache_clear()

    def tearDown(self):
        bench_mod.payload.cache_clear()

    def test_entries_without_id_are_skipped(self):
        benches = {"benches": {"mmlu": {"name": "MMLU", "category": "knowledge"}}}
        scores = {"models": {"m1": {"scores": [{"bench": "mmlu", "value": 80}]}}}
        coverage = {"groups": [{"vendor": "v", "display": "V", "models": [
            {"id": "m1"}, {"id": ""}, {}, "junk"]},
        ]}
        loads = [benches, scores, coverage]
        with mock.patch.object(bench_mod, "_load", side_effect=loads), \
                mock.patch.object(bench_mod, "_channel_rows", return_value=[]):
            data = bench_mod.payload()
        self.assertEqual([m["id"] for m in data["models"]], ["m1"])
        self.assertEqual(len(data["groups"][0]["models"]), 1)
        self.assertEqual(data["totals"]["models"], 1)


class ProbeReserveTests(unittest.TestCase):
    def test_probe_free_quota_applies_reserve(self):
        app = App(parse_config(align_config()))
        channel = align_config()["channels"][0]
        with mock.patch.object(freealign, "run_vendor_cmd",
                               return_value=json.dumps(bl_rows())), \
                mock.patch.object(app, "_free_quota_credentials",
                                  return_value={"BL_TOKEN": "abc"}):
            result = app._probe_free_quota(channel, ["fake-bl", "usage", "free"],
                                           ["BL_TOKEN"])
        self.assertTrue(result["ok"])
        # 27b sits inside the 10k reserve: the console probe must agree with
        # the sweep and never offer it as a free model.
        self.assertEqual(result["models"], ["qwen3.8-flash"])
        self.assertEqual(result["count"], 1)


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ZhipuAutoRenewTests(unittest.TestCase):
    def test_missing_autoRenew_reports_false(self):
        resp = _FakeResp({"code": 200, "success": True,
                          "data": {"planType": "Lite", "status": "Running"}})
        result = zhipu_quota.fetch_coding_plan(
            "k", _urlopen=lambda req, timeout=None: resp)
        self.assertIs(result["auto_renew"], False)

    def test_autoRenew_true_reports_true(self):
        resp = _FakeResp({"code": 200, "success": True,
                          "data": {"planType": "Lite", "autoRenew": True}})
        result = zhipu_quota.fetch_coding_plan(
            "k", _urlopen=lambda req, timeout=None: resp)
        self.assertIs(result["auto_renew"], True)


class SourceAssertionsTests(unittest.TestCase):
    """Mechanical fixes verified against the shipped source files."""

    @classmethod
    def setUpClass(cls):
        cls.server = (SRC / "server.py").read_text(encoding="utf-8")
        cls.ui_src = (SRC / "ui.py").read_text(encoding="utf-8")
        cls.keys = (SRC / "keys.py").read_text(encoding="utf-8")
        cls.watch = (SRC / "watch.py").read_text(encoding="utf-8")
        cls.subs_src = (SRC / "subscriptions.py").read_text(encoding="utf-8")
        cls.appjs = (STATIC / "app.js").read_text(encoding="utf-8")
        cls.index = (STATIC / "index.html").read_text(encoding="utf-8")

    def test_available_calls_hold_store_lock(self):
        pattern = re.compile(
            r"with self\.store\.lock:\n"
            r"\s+ok, why = self\.store\.states\[name\]\.available\("
            r"time\.time\(\), channel\.rate\)")
        self.assertEqual(len(pattern.findall(self.server)), 5)
        sticky = re.compile(
            r"with self\.store\.lock:\n"
            r"\s+sticky = self\.store\.session_channel\(session, time\.time\(\)\)")
        self.assertEqual(len(sticky.findall(self.server)), 1)

    def test_no_repr_in_catalog_error(self):
        self.assertNotIn('repr(exc)[:200]', self.server)
        self.assertIn('str(exc)[:200]', self.server)

    def test_record_usage_passes_handler(self):
        self.assertNotIn('self.record_usage(channel.name, False, model=wire)\n',
                         self.server.replace(
                             'self.record_usage(channel.name, False, model=wire, h=h)', ''))

    def test_error_messages_are_chinese(self):
        self.assertNotIn("not multipart/form-data", self.server)
        self.assertNotIn("config file not found or unreadable", self.server)
        self.assertNotIn("config path unknown", self.server)
        self.assertIn("multipart/form-data", self.server)  # check itself remains
        self.assertIn("配置文件不存在或无法读取", self.server)
        self.assertEqual(self.server.count("配置路径未知"), 2)

    def test_keys_error_is_chinese(self):
        self.assertNotIn("name is required", self.keys)
        self.assertIn("名称不能为空", self.keys)

    def test_facts_resolve_error_envelope(self):
        self.assertNotIn('{"error": f"unresolved:', self.ui_src)
        self.assertIn('"error": {"message":', self.ui_src)

    def test_reset_channel_404_has_error_envelope(self):
        self.assertIn('"error": {"message": f"渠道不存在：{name}"}}', self.ui_src)

    def test_wizard_survives_corrupted_config(self):
        self.assertIn("配置文件损坏", self.ui_src)

    def test_quickstart_panel_has_no_redundant_hidden(self):
        self.assertNotIn('id="panel-quickstart" hidden', self.index)

    def test_member_usage_note_is_dynamic(self):
        self.assertNotIn("统计最近 30 天用量", self.index)
        self.assertIn("member-usage-note", self.appjs)
        self.assertIn("memberRange", self.appjs)

    def test_watch_keeps_event_key_cap(self):
        self.assertEqual(self.watch.count('entry["last_event_keys"] = keys[-EVENT_KEY_KEEP:]'), 2)
        self.assertNotIn('entry["last_event_keys"] = keys\n', self.watch)

    def test_remind_days_zero_not_swallowed(self):
        self.assertNotIn('int(record.get("remind_days") or DEFAULT_REMIND_DAYS)',
                         self.subs_src)

    def test_appjs_numbers_formatted(self):
        for absent in (
                "['累计请求', requests,",
                ">${c.handoffs}<",
                "合计 ${req.reduce((a, b) => a + b, 0)}）",
                "合计 ${totalRequests}）",
                "${c.requests} 次",
                "${r.requests} / ${r.failed}",
                "${r.total.requests}",
                "${s.usage.requests}",
        ):
            self.assertNotIn(absent, self.appjs, absent)
        self.assertIn("requests.toLocaleString()", self.appjs)
        self.assertIn("${Number(c.handoffs || 0).toLocaleString()}", self.appjs)

    def test_docs_no_stale_paths(self):
        stale = [p for p in DOCS.glob("*.md")
                 if "../src/planrouter/" in p.read_text(encoding="utf-8")]
        self.assertEqual(stale, [])
        ark = (DOCS / "planrouter-ark-free-quota.md").read_text(encoding="utf-8")
        self.assertNotIn("默认 6 小时", ark)


if __name__ == "__main__":
    unittest.main()
