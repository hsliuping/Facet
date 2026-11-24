"""Unit tests for facet.benchmark_sync (Vals parse + merge, no network)."""
from __future__ import annotations

import pytest

from facet import benchmark_sync as bs

# A trimmed but representative Vals leaderboard HTML fragment. The table
# after the 'leaderboard' marker holds the real ranking; a second table
# (reasoning-effort splits) is appended to prove scoping ignores it.
FIXTURE_HTML = """
<html><body>
<h2>Terminal-Bench 4.0 leaderboard</h2>
<table class="lb">
<tr><th>Rank</th><th>Model</th><th>Harness</th><th>Score</th></tr>
<tr><td>23</td><td><a href="/models/zai_glm-5.3">GLM 5.3</a></td><td>Mini-SWE-agent</td><td>38.89%</td><td>$9.37</td></tr>
<tr><td>33</td><td><a href="/models/mistralai_mistral-large-4">Mistral Large 4 Preview</a></td><td>Mini-SWE-agent</td><td>27.78%</td><td>$5.18</td></tr>
<tr><td>34</td><td><a href="/models/mistralai_mistral-large-4-oss">Mistral Large 4</a></td><td>Mini-SWE-agent</td><td>26.26%</td><td>$2.73</td></tr>
<tr><td>41</td><td><a href="/models/deepseek_deepseek-v4.1-flash">DeepSeek V4.1 Flash</a></td><td>Mini-SWE-agent</td><td>19.70%</td><td>$0.50</td></tr>
<tr><td>99</td><td><a href="/models/somevendor_new-model-9">New Model 9</a></td><td>Mini-SWE-agent</td><td>12.34%</td><td>$1.00</td></tr>
</table>
<h2>Reasoning Effort</h2>
<table class="other">
<tr><td><a href="/models/should_be_ignored">Ignored</a></td><td>99.99%</td></tr>
</table>
<p>Updated 11/18/2026</p>
</body></html>
"""


def _live() -> dict:
    return {
        "schema": 1,
        "models": {
            "zai/glm-5.3": {"scores": [
                {"bench": "terminal_bench_4", "value": 31.9, "unit": "pct",
                 "setting": "unspecified", "harness": "anthropic",
                 "source_url": "old", "date": "2026-01-01"}]},
            "mistralai/mistral-large-4": {"scores": []},
            "deepseek/deepseek-v4.1-flash": {"scores": [
                {"bench": "terminal_bench_4", "value": 19.7, "unit": "pct",
                 "setting": "unspecified", "harness": "vals",
                 "source_url": "old-vals", "date": "2026-01-01"}]},
        },
    }


def test_parse_extracts_leaderboard_rows_only():
    rows = bs.parse_vals_terminal_bench(FIXTURE_HTML)
    # 5 leaderboard rows; the 'Reasoning Effort' table row must be ignored.
    assert len(rows) == 5
    slugs = {r["slug"] for r in rows}
    assert "zai_glm-5.3" in slugs
    assert "should_be_ignored" not in slugs
    glm = next(r for r in rows if r["slug"] == "zai_glm-5.3")
    assert glm["name"] == "GLM 5.3"
    assert glm["score"] == 38.89


def test_parse_vals_updated():
    assert bs.parse_vals_updated(FIXTURE_HTML) == "2026-11-18"
    assert bs.parse_vals_updated("no date here") is None


def test_merge_updates_matched_and_ignores_unmatched():
    rows = bs.parse_vals_terminal_bench(FIXTURE_HTML)
    live = _live()
    new_live, report = bs.merge_terminal_bench(live, rows, "https://vals/x", "2026-11-18")

    # matched exactly the 3 models present in live (new-model-9 is absent)
    assert set(report["matched"]) == {
        "zai/glm-5.3", "mistralai/mistral-large-4", "deepseek/deepseek-v4.1-flash"}
    # the new model only appears in the unmatched report, not in live
    assert not any("new-model-9" in k for k in new_live["models"])
    unmatched_names = {u["name"] for u in report["unmatched"]}
    assert "New Model 9" in unmatched_names


def test_merge_multivariant_takes_highest_score():
    rows = bs.parse_vals_terminal_bench(FIXTURE_HTML)
    live = _live()
    new_live, report = bs.merge_terminal_bench(live, rows, "u", "2026-11-18")
    # Mistral has two matching rows (Preview 27.78 via slug, OSS 26.26 via name);
    # the higher one wins.
    assert report["matched"]["mistralai/mistral-large-4"]["new"] == 27.78


def test_merge_vals_entry_updated_in_place_not_duplicated():
    rows = bs.parse_vals_terminal_bench(FIXTURE_HTML)
    live = _live()
    new_live, _ = bs.merge_terminal_bench(live, rows, "https://vals/new", "2026-11-18")
    scores = new_live["models"]["deepseek/deepseek-v4.1-flash"]["scores"]
    vals = [s for s in scores if s["harness"] == "vals" and s["bench"] == "terminal_bench_4"]
    assert len(vals) == 1  # updated in place, no duplicate
    assert vals[0]["value"] == 19.7  # value unchanged (Vals still reports 19.70)
    assert vals[0]["source_url"] == "https://vals/new"  # metadata refreshed


def test_merge_preserves_other_sources_entries():
    rows = bs.parse_vals_terminal_bench(FIXTURE_HTML)
    live = _live()
    new_live, _ = bs.merge_terminal_bench(live, rows, "u", "2026-11-18")
    scores = new_live["models"]["zai/glm-5.3"]["scores"]
    harnesses = {s["harness"] for s in scores}
    # anthropic entry kept, vals entry added
    assert "anthropic" in harnesses and "vals" in harnesses
    vals = next(s for s in scores if s["harness"] == "vals")
    assert vals["value"] == 38.89
    # required validate_benchmarks fields present on the new vals entry
    for field in ("source_url", "date", "setting"):
        assert vals[field], f"missing {field}"


def test_merge_handles_empty_rows():
    live = _live()
    new_live, report = bs.merge_terminal_bench(live, [], "u", "2026-11-18")
    assert report["matched"] == {} and report["unmatched"] == []
    assert new_live is live  # untouched


def test_sync_swallows_fetch_errors(monkeypatch):
    def boom(url, timeout=20):
        raise RuntimeError("network down")
    monkeypatch.setattr(bs, "fetch_url", boom)
    live = _live()
    new_live, report = bs.sync_terminal_bench(live)
    assert report["errors"]
    assert new_live is live  # not mutated on fetch failure
