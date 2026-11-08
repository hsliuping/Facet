"""Tests for the facts-table bridge (planrouter.facts) and its /api/facts endpoints."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from planrouter import facts as facts_mod  # noqa: E402
from planrouter.core import load_config  # noqa: E402
from planrouter.facts import Facts  # noqa: E402
from planrouter.server import App, Handler  # noqa: E402

TABLE = {
    "schema_version": 1,
    "updated_at": "2026-11-08",
    "models": {
        "groq/llama-3.3-70b": {
            "provider": "groq", "aliases": ["llama-3.3-70b"],
            "context_window": 128000, "tool_call": True,
            "quota_tier": "free_daily",
            "cost": {"input_per_mtok": 0, "output_per_mtok": 0},
        },
        "google/gemini-flash": {
            "provider": "google", "context_window": 1000000,
            "quota_tier": "free_daily",
        },
        "openai/gpt-x": {
            "provider": "openai", "context_window": 400000,
            "tool_call": True, "quota_tier": "paid",
            "cost": {"input_per_mtok": 2.0, "output_per_mtok": 8.0},
        },
        "zhipuai/glm-5.3": {
            "provider": "zhipuai", "aliases": ["glm-5.3", "glm5.3"],
            "context_window": 200000, "tool_call": True,
        },
    },
}

CONFIG = {
    "_readme": "facts endpoint test config",
    "listen": {"host": "127.0.0.1", "port": 0},
    "channels": [
        {
            "name": "plan-a", "protocol": "openai", "base_url": "http://127.0.0.1:9001",
            "api_key": "top-secret", "models": {"auto": "model-a"}, "priority": 10,
        },
        {
            "name": "plan-b", "protocol": "anthropic", "base_url": "http://127.0.0.1:9002",
            "api_key": "top-secret", "models": {"auto": "model-b"}, "priority": 10,
        },
    ],
    "routes": [
        {"name": "default", "protocol": "openai", "channels": ["plan-a"]},
        {"name": "default", "protocol": "anthropic", "channels": ["plan-b"]},
    ],
}


class FactsTests(unittest.TestCase):
    """Query façade over one table dict — naming questions only."""

    @classmethod
    def setUpClass(cls):
        cls.facts = Facts(TABLE)

    def test_empty_table_is_unavailable(self):
        for empty in ({}, None, {"models": {}}):
            self.assertFalse(Facts(empty).available)
        self.assertTrue(self.facts.available)

    def test_resolve_exact_key(self):
        self.assertEqual(self.facts.resolve("groq/llama-3.3-70b"), "groq/llama-3.3-70b")

    def test_resolve_via_alias_and_loose_spelling(self):
        self.assertEqual(self.facts.resolve("glm-5.3"), "zhipuai/glm-5.3")
        self.assertEqual(self.facts.resolve("GLM5.3"), "zhipuai/glm-5.3")

    def test_resolve_blank_and_unknown_return_none(self):
        self.assertIsNone(self.facts.resolve(""))
        self.assertIsNone(self.facts.resolve(None))
        self.assertIsNone(self.facts.resolve("no-such-model"))

    def test_record_returns_a_copy(self):
        rec = self.facts.record("groq/llama-3.3-70b")
        self.assertEqual(rec["provider"], "groq")
        rec["provider"] = "mutated"
        self.assertEqual(self.facts.record("groq/llama-3.3-70b")["provider"], "groq")

    def test_record_unknown_key_is_empty(self):
        self.assertEqual(self.facts.record("no/such"), {})

    def test_free_candidates_order_tool_call_first_then_context(self):
        rows = self.facts.free_candidates(limit=None)
        self.assertEqual([r["id"] for r in rows],
                         ["groq/llama-3.3-70b", "google/gemini-flash"])

    def test_free_candidates_tool_call_filter(self):
        rows = self.facts.free_candidates(limit=None, tool_call=True)
        self.assertEqual([r["id"] for r in rows], ["groq/llama-3.3-70b"])

    def test_free_candidates_limit_and_shape(self):
        rows = self.facts.free_candidates(limit=1)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], "groq/llama-3.3-70b")
        self.assertEqual(row["provider"], "groq")
        self.assertEqual(row["quota_tier"], "free_daily")
        self.assertIn("context_window", row)
        self.assertIn("cost", row)

    def test_coverage_counts_free_tier_models(self):
        cov = self.facts.coverage()
        self.assertEqual(cov, {
            "models": 4, "providers": 4, "free_models": 2,
            "updated_at": "2026-11-08",
        })

    def test_free_tiers_stay_in_sync_with_facet(self):
        from facet._find import FREE_TIERS as facet_tiers
        self.assertEqual(facts_mod.FREE_TIERS, facet_tiers)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self._old = facts_mod._CACHED
        facts_mod._CACHED = None

    def tearDown(self):
        facts_mod._CACHED = self._old

    def test_snapshot_is_cached_per_process(self):
        sentinel = Facts(TABLE)
        facts_mod._CACHED = sentinel
        self.assertIs(facts_mod.snapshot(), sentinel)

    def test_snapshot_degrades_to_unavailable_on_load_failure(self):
        with mock.patch.dict(sys.modules, {"facet._table": None}):
            snap = facts_mod.snapshot()
        self.assertIsInstance(snap, Facts)
        self.assertFalse(snap.available)


class FactsEndpointTests(unittest.TestCase):
    """/api/facts* over a real HTTP server, with the snapshot pinned to TABLE."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.httpd = None

    def tearDown(self):
        if self.httpd and getattr(self, "serving", False):
            self.httpd.shutdown()
            self.httpd.server_close()
        self.path.unlink(missing_ok=True)
        Path(str(self.path) + ".tmp").unlink(missing_ok=True)

    def start(self) -> str:
        self.path.write_text(json.dumps(CONFIG), encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        try:
            self.httpd.app = App(load_config(self.path), config_path=self.path)  # type: ignore[attr-defined]
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
            self.serving = True
        except BaseException:
            # serve_forever never ran, so shutdown() would deadlock forever
            self.httpd.server_close()
            raise
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def call(self, base: str, path: str):
        req = Request(base + path)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_facts_reports_coverage_and_tiers(self):
        with mock.patch.object(facts_mod, "snapshot", return_value=Facts(TABLE)):
            status, data = self.call(self.start(), "/api/facts")
        self.assertEqual(status, 200)
        self.assertTrue(data["available"])
        self.assertEqual(data["coverage"]["free_models"], 2)
        self.assertEqual(data["free_tiers"], list(facts_mod.FREE_TIERS))

    def test_facts_free_lists_candidates(self):
        with mock.patch.object(facts_mod, "snapshot", return_value=Facts(TABLE)):
            status, data = self.call(self.start(), "/api/facts/free")
        self.assertEqual(status, 200)
        rows = data["candidates"]
        self.assertEqual([r["id"] for r in rows],
                         ["groq/llama-3.3-70b", "google/gemini-flash"])

    def test_facts_resolve_hits_and_misses(self):
        with mock.patch.object(facts_mod, "snapshot", return_value=Facts(TABLE)):
            base = self.start()
            status, data = self.call(base, "/api/facts/resolve/glm5.3")
            self.assertEqual(status, 200)
            self.assertEqual(data["key"], "zhipuai/glm-5.3")
            self.assertEqual(data["query"], "glm5.3")
            self.assertEqual(data["record"]["provider"], "zhipuai")
            status, data = self.call(base, "/api/facts/resolve/no-such-model")
            self.assertEqual(status, 404)
            self.assertIn("unresolved", data["error"])


if __name__ == "__main__":
    unittest.main()
