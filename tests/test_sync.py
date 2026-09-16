"""Tests for tools/sync_models_dev.py — stdlib unittest, run from repo root:

    python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import sync_models_dev as sync  # noqa: E402


class IdentityTests(unittest.TestCase):
    def test_strip_provider_prefix(self):
        self.assertEqual(sync.strip_provider_prefix("anthropic/claude-sonnet-4.5"), "claude-sonnet-4.5")
        self.assertEqual(sync.strip_provider_prefix("gpt-5"), "gpt-5")

    def test_identity_folds_case_and_dots(self):
        self.assertEqual(sync.identity("Claude-Sonnet-4.5"), sync.identity("claude-sonnet-4-5"))
        self.assertEqual(sync.identity("openai/gpt-9"), sync.identity("GPT.9"))

    def test_identity_tolerates_missing_hyphens(self):
        # 'glm5.3' (no hyphen before 5) must group with 'glm-5.3'
        for spelling in ("glm-5.3", "glm5.3", "GLM5.3", "glm5-3", "GLM-5.3", "zhipuai/glm5.3"):
            self.assertEqual(sync.identity(spelling), "glm53", spelling)

    def test_base_alias_strips_date_suffix(self):
        self.assertEqual(sync.base_alias("doubao-seed-1-6-251015"), "doubao-seed-1-6")
        self.assertEqual(sync.base_alias("gpt-5-20260101"), "gpt-5")
        self.assertIsNone(sync.base_alias("gpt-5"))
        self.assertIsNone(sync.base_alias("doubao-seed-1-6"))  # 6 is not a date


class CurrencyTests(unittest.TestCase):
    def test_usd_passthrough(self):
        self.assertEqual(sync.to_usd_per_mtok(3.0), 3.0)

    def test_cny_converted(self):
        self.assertAlmostEqual(sync.to_usd_per_mtok(7.2, currency="CNY", cny_rate=7.2), 1.0)

    def test_per_token_scaled(self):
        self.assertEqual(sync.to_usd_per_mtok(0.000003, unit="tokens"), 3.0)

    def test_unsupported_rejected(self):
        with self.assertRaises(ValueError):
            sync.to_usd_per_mtok(1.0, currency="EUR")
        with self.assertRaises(ValueError):
            sync.to_usd_per_mtok(1.0, unit="kg")


class PruneTests(unittest.TestCase):
    def test_nulls_and_empty_containers_dropped(self):
        out = sync.prune({
            "provider": "openai", "aliases": [], "tool_call": None,
            "cost": {"input_per_mtok": None, "output_per_mtok": None, "cache_read_per_mtok": None},
        })
        self.assertEqual(out, {"provider": "openai"})

    def test_partial_cost_kept(self):
        out = sync.prune({"cost": {"input_per_mtok": 1.0, "output_per_mtok": None, "cache_read_per_mtok": 0}})
        self.assertEqual(out, {"cost": {"input_per_mtok": 1.0, "cache_read_per_mtok": 0}})

    def test_zero_cost_value_is_kept_in_partial(self):
        out = sync.prune({"cost": {"input_per_mtok": 0.0, "output_per_mtok": None, "cache_read_per_mtok": None}})
        self.assertEqual(out, {"cost": {"input_per_mtok": 0.0}})


RAW = {
    "openai": {
        "models": {
            "gpt-9": {
                "limit": {"context": 400000, "output": 128000},
                "cost": {"input": 2.0, "output": 8.0, "cache_read": 0.2},
                "tool_call": True, "reasoning": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "release_date": "2026-01-01", "family": "gpt",
            },
        },
    },
    # Aggregator copy: weaker limits + different spelling -> alias, must lose to openai.
    "openrouter": {
        "models": {
            "openai/gpt.9": {
                "id": "openai/gpt.9",
                "limit": {"context": 200000, "output": 32000},
                "cost": {"input": 2.5, "output": 9.0},
                "tool_call": True,
            },
        },
    },
    # Free plan mirror: 0-cost carries no pricing info -> less complete than openai.
    "gitlab": {
        "models": {
            "gpt-9": {"limit": {"context": 1000000}, "cost": {"input": 0, "output": 0}, "tool_call": True},
        },
    },
}


class BuildTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table, cls.stats = sync.build_table(RAW, cny_rate=7.2)

    def test_dedup_and_counts(self):
        self.assertEqual(self.stats["raw_models"], 3)
        self.assertEqual(self.stats["identities"], 1)
        self.assertEqual(list(self.table["models"]), ["openai/gpt-9"])

    def test_winner_is_first_party_most_complete(self):
        rec = self.table["models"]["openai/gpt-9"]
        self.assertEqual(rec["provider"], "openai")
        self.assertEqual(rec["context_window"], 400000)  # not the aggregator's 200000

    def test_alias_from_aggregator_spelling(self):
        rec = self.table["models"]["openai/gpt-9"]
        self.assertIn("gpt.9", rec["aliases"])

    def test_absent_encoding_no_nulls_no_manual_fields(self):
        rec = self.table["models"]["openai/gpt-9"]
        self.assertNotIn("quota_tier", rec)
        self.assertNotIn("quality_hint", rec)
        self.assertNotIn("attachment", rec)  # unknown -> absent, never null/default
        self.assertIn("family", rec)

    def test_size_budget_flagged(self):
        # guard: tiny fixture trivially fits; keeps MAX_TABLE_BYTES wired
        self.assertGreater(sync.MAX_TABLE_BYTES, 0)


class PreserveManualTests(unittest.TestCase):
    def test_manual_annotations_survive_resync(self):
        with tempfile.TemporaryDirectory() as tmp:
            prev_path = Path(tmp) / "model-registry.json"
            prev = {"schema_version": 1, "models": {
                "openai/gpt-9": {"provider": "openai", "quota_tier": "paid", "quality_hint": 4},
            }}
            prev_path.write_text(json.dumps(prev), encoding="utf-8")

            table, _ = sync.build_table(RAW, cny_rate=7.2)
            sync.preserve_manual(table["models"], prev_path)
            rec = table["models"]["openai/gpt-9"]
            self.assertEqual(rec["quota_tier"], "paid")
            self.assertEqual(rec["quality_hint"], 4)

    def test_missing_previous_table_is_noop(self):
        table, _ = sync.build_table(RAW, cny_rate=7.2)
        sync.preserve_manual(table["models"], Path("Z:/definitely/not/here.json"))
        self.assertNotIn("quota_tier", table["models"]["openai/gpt-9"])


class OpenRouterSourceTests(unittest.TestCase):
    def test_conversion_shape_and_units(self):
        or_data = {"data": [{
            "id": "deepseek/deepseek-v4-pro-0813",
            "context_length": 1024000,
            "top_provider": {"max_completion_tokens": 384000},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "pricing": {"prompt": "0.00000066", "completion": "0.00000198",
                        "input_cache_read": "0.000000022"},
            "supported_parameters": ["tools", "structured_outputs", "reasoning", "temperature"],
        }]}
        out = sync.openrouter_to_modelsdev(or_data)
        rec = out["openrouter"]["models"]["deepseek/deepseek-v4-pro-0813"]
        self.assertEqual(rec["limit"]["context"], 1024000)
        self.assertEqual(rec["limit"]["output"], 384000)
        self.assertEqual(rec["cost"]["input"], 0.66)      # per-token -> per-MTok
        self.assertEqual(rec["cost"]["output"], 1.98)
        self.assertEqual(rec["cost"]["cache_read"], 0.022)
        self.assertTrue(rec["tool_call"])
        self.assertTrue(rec["structured_output"])
        self.assertTrue(rec["reasoning"])
        self.assertEqual(rec["modalities"]["input"], ["text"])

    def test_negative_price_becomes_unknown(self):
        self.assertIsNone(sync._or_price_per_mtok("-0.000001"))
        self.assertIsNone(sync._or_price_per_mtok(None))
        self.assertEqual(sync._or_price_per_mtok("0"), 0.0)

    def test_merge_source_fills_gaps_only(self):
        raw = {"openrouter": {"models": {
            "x": {"limit": {"context": 100}, "tool_call": True, "family": "keep"},
        }}}
        extra = {"openrouter": {"models": {
            "x": {"limit": {"context": 999, "output": 50}, "cost": {"input": 1.0}},
            "y": {"tool_call": True},
        }}}
        added = sync.merge_source(raw, extra)
        self.assertEqual(added, 1)
        x = raw["openrouter"]["models"]["x"]
        self.assertEqual(x["limit"]["context"], 100)   # existing wins
        self.assertEqual(x["limit"]["output"], 50)     # gap filled (nested)
        self.assertEqual(x["cost"]["input"], 1.0)      # gap filled
        self.assertEqual(x["family"], "keep")
        self.assertIn("y", raw["openrouter"]["models"])

    def test_cross_provider_fill_for_capability_fields(self):
        # winner (openai) lacks structured_output + release_date; sibling copy has them
        raw = {
            "openai": {"models": {"gpt-x": {
                "limit": {"context": 100, "output": 10},
                "tool_call": True, "modalities": {"input": ["text"], "output": ["text"]},
            }}},
            "mirror": {"models": {"gpt-x": {
                "limit": {"context": 100, "output": 10},
                "tool_call": True, "structured_output": True,
                "release_date": "2026-01-01", "family": "gpt",
                "cost": {"input": 5, "output": 5},
            }}},
        }
        table, _ = sync.build_table(raw, cny_rate=7.2)
        rec = table["models"]["openai/gpt-x"]
        self.assertTrue(rec["structured_output"])     # borrowed from sibling
        self.assertEqual(rec["release_date"], "2026-01-01")
        self.assertNotIn("cost", rec)                 # cost is never borrowed

    def test_cross_provider_fill_never_overwrites(self):
        raw = {
            "openai": {"models": {"gpt-x": {"reasoning": False, "limit": {"context": 100}}}},
            "mirror": {"models": {"gpt-x": {"reasoning": True}}},
        }
        table, _ = sync.build_table(raw, cny_rate=7.2)
        self.assertFalse(table["models"]["openai/gpt-x"]["reasoning"])


if __name__ == "__main__":
    unittest.main()
