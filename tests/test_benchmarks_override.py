"""Tests for the runtime score override layer in facet.gateway.benchmarks."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from facet.gateway import benchmarks as bm


def _override_entry(value: float) -> dict:
    return {
        "bench": "terminal_bench_4", "value": value, "unit": "pct",
        "setting": "unspecified", "harness": "vals",
        "source_url": "test", "date": "2026-11-24",
        "source_type": "independent", "source_name": "t",
    }


def _write_override(path: Path, key: str, value: float) -> None:
    data = {"models": {key: {"scores": [_override_entry(value)]}}}
    path.write_text(json.dumps(data), encoding="utf-8")


def _best_mean(scores_models: dict, key: str) -> float | None:
    """Mirror agent_score exactly: best-per-bench mean over Agent-category benches."""
    benches = bm._load("benchmarks.json", {"benches": {}}).get("benches", {})
    agent = {k for k, v in benches.items() if (v or {}).get("category") == "Agent"}
    best: dict[str, float] = {}
    for s in (scores_models.get(key) or {}).get("scores") or []:
        if s.get("bench") not in agent or s.get("unit") not in ("pct", "score100"):
            continue
        val = s.get("value")
        if not isinstance(val, (int, float)) or not 0 <= val <= 100:
            continue
        bench = s["bench"]
        if bench not in best or val > best[bench]:
            best[bench] = float(val)
    return round(sum(best.values()) / len(best), 1) if best else None


@pytest.fixture()
def clean_override():
    bm.set_override_path(None)
    bm.clear_cache()
    yield
    bm.set_override_path(None)
    bm.clear_cache()


def test_no_override_matches_bundled(clean_override):
    bm.set_override_path(None)
    bundled = bm._load("model-scores.json", {"models": {}})
    key = "zai/glm-5.3"
    assert bm.agent_score(key) == _best_mean(bundled["models"], key)


def test_override_layers_on_top_and_clears(clean_override):
    key = "zai/glm-5.3"
    base = bm.agent_score(key)
    assert base is not None

    tmp = Path(tempfile.mkdtemp()) / "model-scores.override.json"
    _write_override(tmp, key, 99.9)

    bm.set_override_path(tmp)
    merged = bm._load("model-scores.json", {"models": {}})
    expected = _best_mean(merged["models"], key)
    assert bm.agent_score(key) == expected
    assert expected > base  # the override raised the aggregate

    # disabling reverts to the bundled value
    bm.set_override_path(None)
    assert bm.agent_score(key) == base


def test_override_preserves_other_benches(clean_override):
    """Overriding one bench must not wipe other benches' scores."""
    key = "zai/glm-5.3"
    tmp = Path(tempfile.mkdtemp()) / "ov.json"
    _write_override(tmp, key, 50.0)
    bm.set_override_path(tmp)
    merged = bm._load("model-scores.json", {"models": {}})
    benches = {s["bench"] for s in merged["models"][key]["scores"]}
    # terminal_bench_3 and agents_last_exam must survive (only tb4 touched)
    assert "terminal_bench_3" in benches
    assert "agents_last_exam" in benches


def test_clear_cache_picks_up_override_change(clean_override):
    key = "zai/glm-5.3"
    tmp = Path(tempfile.mkdtemp()) / "ov.json"
    bm.set_override_path(tmp)

    _write_override(tmp, key, 60.0)
    first = bm.agent_score(key)

    _write_override(tmp, key, 90.0)
    bm.clear_cache()
    second = bm.agent_score(key)
    assert second > first
