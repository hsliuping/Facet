"""Benchmark data for GET /api/models/compare.

Loads the three bundled data files (benchmark dictionary, per-model scores,
coverage list). Which of the user's own channels can call each model is NOT
part of this cached payload: it changes with the config, so the API handler
overlays live channel rows on every request (see overlay_channels).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .._identity import identity

DATA = Path(__file__).resolve().parent.parent / "data"
# Repo checkout: same files, source of truth under registry/ (data/ is a
# build-time snapshot directory and is gitignored).
REPO_DATA = DATA.parent.parent.parent / "registry"

# Runtime override: a user-writable file (next to config.json) holding scores
# fetched by the self-maintenance task, layered on top of the bundled snapshot
# so new public scores appear without reinstalling. Merged at the
# (bench, setting, harness) level so a refreshed value replaces the old one
# while other sources' entries are preserved.
_override_path: Path | None = None


def set_override_path(path: "Path | str | None") -> None:
    """Point the loader at a user-dir override file (or None to disable)."""
    global _override_path
    _override_path = Path(path) if path else None
    clear_cache()


def clear_cache() -> None:
    """Drop cached score maps so a freshly written override is picked up."""
    _agent_score_map.cache_clear()
    payload.cache_clear()


def _apply_override(data: dict) -> dict:
    if not _override_path:
        return data
    try:
        override = json.loads(_override_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return data
    models = data.setdefault("models", {})
    for key, omodel in (override.get("models") or {}).items():
        base = models.setdefault(key, {})
        base_scores = base.setdefault("scores", [])
        idx = {(s.get("bench"), s.get("setting"), s.get("harness")): i
               for i, s in enumerate(base_scores)}
        for s in (omodel.get("scores") or []):
            k = (s.get("bench"), s.get("setting"), s.get("harness"))
            if k in idx:
                base_scores[idx[k]] = s
            else:
                base_scores.append(s)
                idx[k] = len(base_scores) - 1
    return data


def _load(name: str, fallback: dict) -> dict:
    data: dict | None = None
    for d in (DATA, REPO_DATA):
        try:
            with open(d / name, encoding="utf-8") as f:
                data = json.load(f)
            break
        except (OSError, ValueError):
            continue
    if data is None:
        return fallback
    if name == "model-scores.json":
        _apply_override(data)
    return data


def norm_base(key: str) -> str:
    """Normalize a registry key or model id to a bare model name.

    Drops the provider prefix, version decorations (``@default``, ``:low-cost``)
    and non-alphanumerics, so ``amazon-bedrock/anthropic.claude-opus-5-5`` and
    ``anthropic/claude-opus-5-5`` compare equal.
    """
    base = key.split("/", 1)[1] if "/" in key else key
    base = base.split("@", 1)[0].split(":", 1)[0]
    return identity(base)


# Historical private alias (kept for in-package callers).
_norm_base = norm_base


def resolve_user_channels(mid: str, rows) -> list[dict]:
    """The user's own channels able to call this model.

    ``rows`` come from App.compare_channel_rows(): (name, provider, normalized
    explicit model bases, wildcard) — already filtered to enabled channels
    with a real key. A wildcard channel passes any model name through; an
    explicit key matches when its normalized base equals the registry id's
    (provider namespaces and ``:free``-style suffixes normalize away).
    """
    norm = norm_base(mid)
    out = []
    for name, provider, bases, wildcard in rows:
        if wildcard or norm in bases:
            out.append({"channel": name, "provider": provider})
    return out


def overlay_channels(data: dict, rows) -> None:
    """Attach live channel availability to a compare payload, in place.

    payload() is lru-cached static data (scores / coverage); which channels
    can call a model changes with the config, so the API handler overlays
    fresh rows on every request instead of caching them.
    """
    for m in data.get("models", []):
        chs = resolve_user_channels(m.get("id", ""), rows)
        m["channels"] = chs
        m["channel_count"] = len(chs)


#: Units that share a 0-100 percentage scale. Other units (usd amounts,
#: 0-1 fractions) cannot be averaged into one number and are excluded
#: from the aggregate agent score.
SCORE_UNITS = ("pct", "score100")


@lru_cache(maxsize=1)
def _agent_score_map() -> dict[str, float]:
    """normalized model base → mean of best-per-bench Agent-category scores.

    Cross-setting / cross-harness values of one bench are not directly
    comparable, so the best observed value per bench is kept (an upper
    bound of capability); the mean across benches is the aggregate score
    facet-agent ranks by.
    """
    benches = _load("benchmarks.json", {"benches": {}}).get("benches", {})
    agent_benches = {k for k, v in benches.items()
                     if (v or {}).get("category") == "Agent"}
    scores = _load("model-scores.json", {"models": {}}).get("models", {})
    out: dict[str, float] = {}
    for mid, mdl in scores.items():
        best: dict[str, float] = {}
        for s in mdl.get("scores") or []:
            if (s.get("bench") not in agent_benches
                    or s.get("unit") not in SCORE_UNITS):
                continue
            value = s.get("value")
            if not isinstance(value, (int, float)) or not 0 <= value <= 100:
                continue
            bench = s["bench"]
            if bench not in best or value > best[bench]:
                best[bench] = float(value)
        if best:
            out[_norm_base(mid)] = round(sum(best.values()) / len(best), 1)
    return out


def agent_score(registry_key: str) -> float | None:
    """Aggregate Agent-benchmark score for one registry model, 0-100.

    None when the bundled score table has no comparable Agent-category
    entry for the model — callers rank unknown scores after known ones
    instead of guessing a capability.
    """
    if not registry_key:
        return None
    return _agent_score_map().get(_norm_base(registry_key))


@lru_cache(maxsize=1)
def payload() -> dict:
    benches = _load("benchmarks.json", {"benches": {}})
    scores = _load("model-scores.json", {"models": {}}).get("models", {})
    coverage = _load("model-coverage.json", {"groups": []}).get("groups", [])
    bench_meta = benches.get("benches", {})

    groups = []
    flat = []
    for grp in coverage:
        entries = []
        for item in grp.get("models", []):
            # Coverage feeds are hand-maintained; a stray entry without an
            # id (or a bare string) must be skipped, not crash the compare
            # table for every model.
            mid = item.get("id") if isinstance(item, dict) else None
            if not mid:
                continue
            mdl = scores.get(mid, {})
            raw_scores = mdl.get("scores", [])
            enriched = []
            for s in raw_scores:
                meta = bench_meta.get(s.get("bench", ""), {})
                enriched.append({
                    "bench": s.get("bench", ""),
                    "bench_name": meta.get("name", s.get("bench", "")),
                    "bench_category": meta.get("category", ""),
                    "value": s.get("value"),
                    "unit": s.get("unit", ""),
                    "setting": s.get("setting", ""),
                    "harness": s.get("harness", ""),
                    "source_type": s.get("source_type", "vendor"),
                    "source_name": s.get("source_name", ""),
                    "source_url": s.get("source_url", ""),
                    "date": s.get("date", ""),
                    "note": s.get("note", ""),
                })
            entry = {
                "id": mid,
                "label": item.get("label") or mdl.get("display_name") or mid,
                "tier": item.get("tier") or mdl.get("tier", ""),
                "vendor": grp.get("vendor", ""),
                "vendor_display": grp.get("display", ""),
                "release_date": mdl.get("release_date"),
                "notes": mdl.get("notes", ""),
                "scores": enriched,
                "score_count": len(enriched),
            }
            entries.append(entry)
            flat.append(entry)
        groups.append({"vendor": grp.get("vendor", ""), "display": grp.get("display", ""),
                       "models": entries})

    return {
        "schema": 1,
        "groups": groups,
        "models": flat,
        "benchmarks": bench_meta,
        "totals": {
            "models": len(flat),
            "scores": sum(m["score_count"] for m in flat),
            "benches": len(bench_meta),
            "without_scores": sum(1 for m in flat if not m["score_count"]),
        },
    }
