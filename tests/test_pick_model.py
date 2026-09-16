"""Tests for examples/pick_model.py — stdlib unittest, run from repo root:

    python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))
import pick_model as demo  # noqa: E402


def make_table():
    return {
        "schema_version": 1,
        "updated_at": "2026-09-16",
        "generator": "test",
        "models": {
            "openai/gpt-9": {
                "provider": "openai", "aliases": ["gpt.9"],
                "context_window": 400000, "max_output": 128000,
                "tool_call": True, "reasoning": True, "structured_output": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "cost": {"input_per_mtok": 2.0, "output_per_mtok": 8.0},
            },
            "google/gemini-9-pro": {
                "provider": "google",
                "context_window": 1000000, "max_output": 64000,
                "tool_call": True,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "cost": {"input_per_mtok": 1.0, "output_per_mtok": 4.0},
            },
            "gateway/cheap-1": {
                "provider": "gateway",
                "tool_call": True,
                "cost": {"input_per_mtok": 0.5, "output_per_mtok": 1.0},
            },
            "mirror/free-1": {
                "provider": "mirror",
                "context_window": 900000, "tool_call": True,
                "cost": {"input_per_mtok": 0, "output_per_mtok": 0},  # plan mirror
            },
        },
    }


class ResolveTests(unittest.TestCase):
    def test_resolve_exact_and_alias(self):
        table = make_table()
        self.assertEqual(demo.resolve(table, "gpt-9"), "openai/gpt-9")
        self.assertEqual(demo.resolve(table, "gpt.9"), "openai/gpt-9")
        self.assertEqual(demo.resolve(table, "GPT-9"), "openai/gpt-9")

    def test_resolve_miss(self):
        self.assertIsNone(demo.resolve(make_table(), "gpt-8"))


class CheckTests(unittest.TestCase):
    def test_all_pass(self):
        table = make_table()
        req = {"min_context": 200000, "tools": True, "reasoning": True, "json": True,
               "image_input": False, "max_input_price": None, "max_output_price": None}
        self.assertEqual(demo.check(table["models"]["openai/gpt-9"], req), [])

    def test_absent_is_unknown_not_false(self):
        # gemini has no structured_output field: a --json requirement must exclude it
        table = make_table()
        req = {"min_context": None, "tools": False, "reasoning": False, "json": True,
               "image_input": False, "max_input_price": None, "max_output_price": None}
        self.assertEqual(demo.check(table["models"]["google/gemini-9-pro"], req), ["structured_output"])

    def test_price_ceiling_with_unknown_price_fails(self):
        table = make_table()
        req = {"min_context": None, "tools": False, "reasoning": False, "json": False,
               "image_input": False, "max_input_price": 1.0, "max_output_price": None}
        rec = {"provider": "x"}  # no cost at all -> cannot verify -> fails
        self.assertEqual(demo.check(rec, req), ["input-price<=1.0"])
        self.assertEqual(demo.check(table["models"]["gateway/cheap-1"], req), [])


class RankTests(unittest.TestCase):
    def test_cheapest_first_and_zero_is_unknown(self):
        table = make_table()
        ranked = demo.rank(list(table["models"].items()))
        keys = [k for k, _ in ranked]
        # real prices ascending; the $0 mirror counts as unknown -> last bucket
        self.assertEqual(keys, ["gateway/cheap-1", "google/gemini-9-pro", "openai/gpt-9",
                                "mirror/free-1"])

    def test_unknown_price_tie_broken_by_context(self):
        ranked = demo.rank([("a", {"context_window": 100}), ("b", {"context_window": 900})])
        self.assertEqual([k for k, _ in ranked], ["b", "a"])


class LoadTableTests(unittest.TestCase):
    def test_rejects_unknown_schema_version(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.json"
            p.write_text(json.dumps({"schema_version": 99, "models": {}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                demo.load_table(p)


class ExplainSmokeTest(unittest.TestCase):
    def test_absent_fields_show_placeholder(self):
        line = demo.explain({"provider": "x"})
        self.assertIn("ctx=?", line)
        line2 = demo.explain({"provider": "x", "context_window": 400000,
                              "cost": {"input_per_mtok": 2.0, "output_per_mtok": None}})
        self.assertIn("ctx=400k", line2)
        self.assertIn("$2/$?", line2)


if __name__ == "__main__":
    unittest.main()
