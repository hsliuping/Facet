"""Scheduling-batch regressions: queueing gates, anchored cadence, catalog reuse.

Covers the 调度整固批: the shared sweep gates queue contenders instead of
dropping them with a busy reply, the periodic task loop anchors the next
cycle on the previous cycle's start (no cumulative drift), and a maintenance
pass reuses a recent catalog sweep instead of fetching every upstream
/models endpoint a second time.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from facet.gateway import maintenance as maint
from facet.gateway.core import load_config
from facet.gateway.server import GATE_QUEUE_WAIT, App, ScheduledTask, SerialGate


def sweep_config() -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "a", "protocol": "openai", "base_url": "http://127.0.0.1:9201",
             "api_key": "sk-fixture", "models": {"auto": "m1"}},
            {"name": "b", "protocol": "openai", "base_url": "http://127.0.0.1:9202",
             "api_key": "sk-fixture", "models": {"auto": "m2"}},
            {"name": "claude", "protocol": "anthropic",
             "base_url": "http://127.0.0.1:9204", "models": {"auto": "claude-x"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["a", "b"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
        ],
    }


class SerialGateTests(unittest.TestCase):
    def test_acquire_release_roundtrip(self):
        gate = SerialGate(wait=0.2)
        self.assertTrue(gate.acquire())
        self.assertFalse(gate.acquire())  # busy: no queueing inside the window
        gate.release()
        self.assertTrue(gate.acquire())
        gate.release()

    def test_waiter_runs_after_holder_releases(self):
        gate = SerialGate()
        self.assertTrue(gate.acquire())
        done: list[float] = []

        def contender():
            if gate.acquire():
                done.append(time.time())
                gate.release()

        thread = threading.Thread(target=contender)
        thread.start()
        time.sleep(0.1)  # the contender is queued now, not rejected
        gate.release()
        thread.join(2)
        self.assertEqual(len(done), 1)

    def test_waiters_run_in_arrival_order(self):
        gate = SerialGate()
        self.assertTrue(gate.acquire())
        order: list[int] = []
        threads = []

        for n in range(3):
            def contender(n=n):
                if gate.acquire():
                    order.append(n)
                    gate.release()
            thread = threading.Thread(target=contender)
            threads.append(thread)
            thread.start()
            time.sleep(0.05)  # deterministic arrival order
        gate.release()
        for thread in threads:
            thread.join(2)
        self.assertEqual(order, [0, 1, 2])

    def test_default_wait_comes_from_the_batch_constant(self):
        gate = SerialGate()
        self.assertEqual(gate._wait, GATE_QUEUE_WAIT)
        self.assertGreater(GATE_QUEUE_WAIT, 0)


class MaintenanceQueueTests(unittest.TestCase):
    """A manual pass queues behind a running sweep instead of a busy reply."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "router.json"
        self.path.write_text(json.dumps(sweep_config()), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_manual_pass_queues_behind_a_running_sweep(self):
        app = App(load_config(self.path), config_path=self.path)
        entered: list[str] = []
        release = threading.Event()

        def slow_pass(trigger):
            entered.append(trigger)
            release.wait(3)
            return {"trigger": trigger}

        app._run_maintenance = slow_pass  # patched wholesale: no network here
        first = threading.Thread(
            target=lambda: app.run_maintenance("scheduled"))
        first.start()
        while not entered:
            time.sleep(0.01)
        result: dict = {}

        def manual():
            result["report"] = app.run_maintenance("manual")

        second = threading.Thread(target=manual)
        second.start()
        time.sleep(0.1)
        # queued, not answered with the busy reply
        self.assertEqual(result, {})
        release.set()
        first.join(3)
        second.join(3)
        self.assertEqual(entered, ["scheduled", "manual"])
        self.assertEqual(result["report"]["trigger"], "manual")

    def test_config_apply_gate_is_shared_by_both_passes(self):
        app = App(load_config(self.path), config_path=self.path)
        # a short wait keeps the busy path testable: holding the gate, the
        # alignment sweep queues and only answers busy past the wait
        app.config_apply_lock = SerialGate(wait=0.2)
        # run_free_align and run_maintenance serialize on the same gate
        self.assertTrue(app.config_apply_lock.acquire())
        self.assertEqual(app.run_free_align("scheduled")["busy"], True)
        app.config_apply_lock.release()


class AnchoredCadenceTests(unittest.TestCase):
    """The loop anchors the next cycle on the previous cycle's start."""

    def test_next_cycle_anchors_on_cycle_start(self):
        stamps: list[float] = []

        class Fast(ScheduledTask):
            key = "fast"
            label = "fast"
            description = ""
            hours_key = "unused"
            FIRST_DELAY = 0.0

            def interval(self):
                return 0.15

            def run_once(self):
                stamps.append(time.time())
                time.sleep(0.05)  # cycle work must not drift the beat

        task = Fast(None)
        thread = threading.Thread(target=task._loop, daemon=True)
        thread.start()
        deadline = time.time() + 3
        while len(stamps) < 3 and time.time() < deadline:
            time.sleep(0.01)
        task.stop()
        thread.join(2)
        self.assertEqual(len(stamps), 3)
        span = stamps[-1] - stamps[0]
        # anchored: 2 * interval = 0.30; the old completion-anchored loop
        # drifted by the 0.05s of work per cycle (0.40 and growing)
        self.assertGreaterEqual(span, 0.28)
        self.assertLess(span, 0.36)

    def test_anchored_stamp_is_reported_as_next_run(self):
        stamps: list[float] = []

        class Once(ScheduledTask):
            key = "once"
            label = "once"
            description = ""
            hours_key = "unused"
            FIRST_DELAY = 0.0

            def interval(self):
                return 0.2

            def run_once(self):
                stamps.append(time.time())

        task = Once(None)
        thread = threading.Thread(target=task._loop, daemon=True)
        thread.start()
        deadline = time.time() + 2
        while len(stamps) < 2 and time.time() < deadline:
            time.sleep(0.01)
        task.stop()
        thread.join(2)
        self.assertEqual(len(stamps), 2)
        # next_run_at tracks the anchored schedule, not the completion time
        self.assertAlmostEqual(task.next_run_at, stamps[-1] + 0.2, delta=0.15)


class CatalogReuseTests(unittest.TestCase):
    """A maintenance pass reuses a fresh catalog sweep instead of re-fetching."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "router.json"
        self.path.write_text(json.dumps(sweep_config()), encoding="utf-8")
        # the built-in vendor registry would fetch real doc/model URLs in
        # refresh_providers; the batch under test is the catalog sweep only
        patcher = mock.patch.object(maint, "refresh_providers")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_catalogs_skip_the_second_sweep(self):
        app = App(load_config(self.path), config_path=self.path)
        app.store.record_catalog("a", ["m1"], "http://up", now=time.time())
        app.store.record_catalog("b", ["m2"], "http://up", now=time.time())
        calls: list[int] = []

        def no_sweep():
            calls.append(1)
            return []

        app.refresh_all_catalogs = no_sweep
        report = app.run_maintenance("scheduled")
        self.assertEqual(calls, [])
        self.assertTrue(report.get("catalogs_reused"))

    def test_stale_catalogs_are_refetched(self):
        app = App(load_config(self.path), config_path=self.path)
        app.store.record_catalog("a", ["m1"], "http://up", now=time.time() - 4000)
        app.store.record_catalog("b", ["m2"], "http://up", now=time.time())
        refetched: list[str] = []

        def fake_probe(payload):
            name = payload.get("name") if isinstance(payload, dict) else payload
            refetched.append(name)
            models = ["m1"] if name == "a" else ["m2"]
            return {"ok": True, "name": name, "models": models,
                    "endpoint": "http://up"}

        app._fetch_catalog = fake_probe
        app._maintenance_direct_call = (
            lambda channel, model, prompt, max_tokens=6000: "OK")
        report = app.run_maintenance("scheduled")
        self.assertEqual(sorted(refetched), ["a", "b"])  # the sweep ran
        self.assertNotIn("catalogs_reused", report)

    def test_channel_without_a_catalog_forces_the_sweep(self):
        app = App(load_config(self.path), config_path=self.path)
        app.store.record_catalog("a", ["m1"], "http://up", now=time.time())
        # channel b has never had a catalog fetched: not fresh
        calls: list[int] = []

        def no_sweep():
            calls.append(1)
            return []

        app.refresh_all_catalogs = no_sweep
        report = app.run_maintenance("scheduled")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("catalogs_reused", report)


if __name__ == "__main__":
    unittest.main()
