"""Tests for the facet-models package — stdlib unittest, run from repo root:

    python -m unittest discover -s tests

Bundled-snapshot tests need the build step to have run at least once:

    python tools/build_package.py --skip-build
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import facet  # noqa: E402
import facet.cli  # noqa: E402
from facet import _table  # noqa: E402

FIXTURE = {
    "schema_version": 1,
    "updated_at": "2026-09-16",
    "generator": "test",
    "models": {
        "openai/gpt-9": {
            "provider": "openai", "aliases": ["gpt9"],
            "context_window": 400000, "max_output": 128000,
            "tool_call": True, "reasoning": True,
            "cost": {"input_per_mtok": 2.0, "output_per_mtok": 8.0},
            "modalities": {"input": ["text"], "output": ["text"]},
        },
        "zhipuai/glm-5.3": {
            "provider": "zhipuai", "aliases": ["glm5.3"],
            "context_window": 200000, "max_output": 32000,
            "tool_call": True, "structured_output": True,
            "cost": {"input_per_mtok": 0.6, "output_per_mtok": 2.2},
            "modalities": {"input": ["text", "image"], "output": ["text"]},
        },
        "volcengine/glm-5.3": {
            "provider": "volcengine", "context_window": 200000,
            "tool_call": True,
            "modalities": {"input": ["text"], "output": ["text"]},
        },
        "free/mirror-model": {
            "provider": "free", "aliases": ["mirror-model"],
            "context_window": 1000000, "tool_call": True,
            "cost": {"input_per_mtok": 0, "output_per_mtok": 0},
        },
    },
}


def write_fixture(tmp: str) -> Path:
    path = Path(tmp) / "table.json"
    path.write_text(json.dumps(FIXTURE), encoding="utf-8")
    return path


class LoadTests(unittest.TestCase):
    def test_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_fixture(tmp)
            for source in (path, str(path)):
                table = facet.load(source)
                self.assertEqual(table["schema_version"], 1)
                self.assertIn("openai/gpt-9", table["models"])

    def test_bad_schema_version_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(json.dumps({"schema_version": 2, "models": {}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                facet.load(path)

    def test_missing_models_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            with self.assertRaises(ValueError):
                facet.load(path)

    @unittest.skipUnless(_table.BUNDLED_TABLE.exists(), "bundled snapshot absent (run build_package.py)")
    def test_bundled_snapshot_loads(self):
        table = facet.load()
        self.assertEqual(table["schema_version"], 1)
        self.assertGreater(len(table["models"]), 0)


class ResolveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = FIXTURE

    def test_exact_key(self):
        self.assertEqual(facet.resolve(self.table, "openai/gpt-9"), "openai/gpt-9")

    def test_bare_name(self):
        self.assertEqual(facet.resolve(self.table, "gpt-9"), "openai/gpt-9")

    def test_alias_and_loose_spelling(self):
        self.assertEqual(facet.resolve(self.table, "glm5.3"), "zhipuai/glm-5.3")
        self.assertEqual(facet.resolve(self.table, "GLM-5.3"), "zhipuai/glm-5.3")

    def test_prefixed_name(self):
        self.assertEqual(facet.resolve(self.table, "volcengine/glm5.3"), "zhipuai/glm-5.3")

    def test_first_key_wins_for_shared_identity(self):
        # zhipuai/glm-5.3 precedes volcengine/glm-5.3 -> canonical bare name hits the first
        self.assertEqual(facet.resolve(self.table, "glm-5.3"), "zhipuai/glm-5.3")

    def test_not_found(self):
        self.assertIsNone(facet.resolve(self.table, "no-such-model"))


class FindTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = FIXTURE

    def keys(self, rows):
        return [k for k, _ in rows]

    def test_rank_cheapest_output_first(self):
        rows = facet.find(self.table, tool_call=True, limit=None)
        self.assertEqual(self.keys(rows), ["zhipuai/glm-5.3", "openai/gpt-9",
                                           "free/mirror-model", "volcengine/glm-5.3"])

    def test_price_zero_is_unknown_and_sorts_last(self):
        rows = facet.find(self.table, limit=None)
        self.assertEqual(self.keys(rows)[-2:], ["free/mirror-model", "volcengine/glm-5.3"])

    def test_bigger_context_wins_among_unknown_prices(self):
        rows = facet.find(self.table, limit=None)
        self.assertEqual(self.keys(rows)[-2], "free/mirror-model")  # 1M ctx > 200k

    def test_capability_flag_and_absent_is_unknown(self):
        rows = facet.find(self.table, json_output=True, limit=None)
        self.assertEqual(self.keys(rows), ["zhipuai/glm-5.3"])

    def test_image_input(self):
        rows = facet.find(self.table, image_input=True, limit=None)
        self.assertEqual(self.keys(rows), ["zhipuai/glm-5.3"])

    def test_min_context(self):
        rows = facet.find(self.table, min_context=400_000, limit=None)
        self.assertEqual(sorted(self.keys(rows)), ["free/mirror-model", "openai/gpt-9"])

    def test_provider_filter(self):
        rows = facet.find(self.table, provider="zhipuai", limit=None)
        self.assertEqual(self.keys(rows), ["zhipuai/glm-5.3"])

    def test_limit(self):
        self.assertEqual(len(facet.find(self.table, tool_call=True, limit=2)), 2)
        self.assertEqual(len(facet.find(self.table, tool_call=True, limit=None)), 4)

    def test_max_output_price_passes_zero_cost(self):
        # mirror semantics of pick_model.check: 0 is a number, 0 <= 1.0 passes
        rows = facet.find(self.table, max_output_price=1.0, limit=None)
        self.assertEqual(self.keys(rows), ["free/mirror-model"])

    def test_check_reports_failures(self):
        failed = facet.check({"context_window": 100}, min_context=200_000, tool_call=True)
        self.assertEqual(failed, ["context>=200000", "tool_call"])


class RefreshTests(unittest.TestCase):
    """Refresh is exercised offline via file:// URLs (urllib supports them)."""

    def setUp(self):
        self.no_default = mock.patch.object(_table, "DEFAULT_TABLE_URL", None)
        self.no_default.start()
        self.addCleanup(self.no_default.stop)

    def test_refresh_without_url_raises_clearly(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                facet.load(refresh=True)
        self.assertIn("FACET_TABLE_URL", str(cm.exception))

    def test_refresh_via_env_var(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"FACET_TABLE_URL": write_fixture(tmp).as_uri()}):
                table = facet.load(refresh=True)
        self.assertEqual(table["schema_version"], 1)

    def test_refresh_url_param_beats_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = write_fixture(tmp).as_uri()
            with mock.patch.dict(os.environ, {"FACET_TABLE_URL": "file:///definitely/not/here.json"}):
                table = facet.load(refresh=True, url=good)
        self.assertEqual(table["schema_version"], 1)

    def test_refresh_unreachable_url_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                facet.load(refresh=True, url="file:///definitely/not/here.json")

    def test_refresh_invalid_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError):
                    facet.load(refresh=True, url=path.as_uri())


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = facet.cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_resolve_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self.run_cli(["gpt-9", "--table", str(write_fixture(tmp))])
        self.assertEqual(code, 0)
        self.assertIn("openai/gpt-9", out)

    def test_resolve_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli(["no-such", "--table", str(write_fixture(tmp))])
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_filter_and_emit_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = self.run_cli(
                ["--tools", "--min-context", "200000", "--table", str(write_fixture(tmp)), "--emit-json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(sorted(data), ["free/mirror-model", "openai/gpt-9",
                                        "volcengine/glm-5.3", "zhipuai/glm-5.3"])

    def test_nothing_matched_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            # no fixture model has both reasoning and structured_output
            code, out, _ = self.run_cli(["--reasoning", "--json", "--table", str(write_fixture(tmp))])
        self.assertEqual(code, 0)
        self.assertIn("Nothing matched", out)


class VersionTests(unittest.TestCase):
    def test_version_is_semver_string(self):
        self.assertIsInstance(facet.__version__, str)
        if _table.BUNDLED_TABLE.exists():
            self.assertRegex(facet.__version__, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
