"""Benchmark data for GET /api/models/compare.

Loads the three bundled data files (benchmark dictionary, per-model scores,
coverage list) and joins them with the model registry so the console can show
"which channels can actually call this model".
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .._identity import identity
from . import facts as facts_mod

DATA = Path(__file__).resolve().parent.parent / "data"
# Repo checkout: same files, source of truth under registry/ (data/ is a
# build-time snapshot directory and is gitignored).
REPO_DATA = DATA.parent.parent.parent.parent / "registry"


def _load(name: str, fallback: dict) -> dict:
    for d in (DATA, REPO_DATA):
        try:
            with open(d / name, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return fallback


def _norm_base(key: str) -> str:
    """Normalize a registry key or model id to a bare model name.

    Drops the provider prefix, version decorations (``@default``, ``:low-cost``)
    and non-alphanumerics, so ``amazon-bedrock/anthropic.claude-opus-5-5`` and
    ``anthropic/claude-opus-5-5`` compare equal.
    """
    base = key.split("/", 1)[1] if "/" in key else key
    base = base.split("@", 1)[0].split(":", 1)[0]
    return identity(base)


def _channel_rows() -> list[tuple[str, str, str]]:
    """(channel, provider, normalized basename) for every registry entry."""
    table = getattr(facts_mod.snapshot(), "_models", {}) or {}
    rows = []
    for key, rec in table.items():
        provider = (rec or {}).get("provider") or key.split("/", 1)[0]
        rows.append((key, provider, _norm_base(key)))
    return rows


def _resolve_channels(mid: str, rows: list[tuple[str, str, str]]) -> list[dict]:
    """Channels that expose this model.

    Exact normalized match plus vendor-namespaced variants such as
    ``au.anthropic.claude-opus-5-5`` (short prefix only, to avoid counting
    a different model that merely ends with the same name).
    """
    norm = _norm_base(mid)
    out = []
    for channel, provider, cnorm in rows:
        if cnorm == norm or (
            len(cnorm) > len(norm)
            and cnorm.endswith(norm)
            and len(cnorm) - len(norm) <= 20
        ):
            out.append({"channel": channel, "provider": provider})
    return out


@lru_cache(maxsize=1)
def payload() -> dict:
    benches = _load("benchmarks.json", {"benches": {}})
    scores = _load("model-scores.json", {"models": {}}).get("models", {})
    coverage = _load("model-coverage.json", {"groups": []}).get("groups", [])
    channel_rows = _channel_rows()
    bench_meta = benches.get("benches", {})

    groups = []
    flat = []
    for grp in coverage:
        entries = []
        for item in grp.get("models", []):
            mid = item["id"]
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
            channels = _resolve_channels(mid, channel_rows)
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
                "channels": channels,
                "channel_count": len(channels),
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
