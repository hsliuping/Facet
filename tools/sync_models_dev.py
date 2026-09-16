#!/usr/bin/env python3
"""Facet sync tool: models.dev -> registry/model-registry.json.

Pipeline: fetch (or --offline file) -> identity grouping (dedup across
providers) -> winner selection (first-party priority, completeness) ->
alias collection -> currency/unit normalization -> emit table.

stdlib only. Usage:
    python tools/sync_models_dev.py                    # live fetch
    python tools/sync_models_dev.py --offline raw.json # from cached file
    python tools/sync_models_dev.py --out registry/model-registry.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SOURCE_URL = "https://models.dev/api.json"
GENERATOR = "sync_models_dev@0.1.0"
MAX_TABLE_BYTES = 1024 * 1024  # table-level hard rule: single file < 1 MB

# Tier 0 = model vendor's own endpoint. Everything else (aggregators,
# gateways, token-plan mirrors) is tier 1 and only wins when no tier-0
# copy of the same model identity exists.
FIRST_PARTY = frozenset({
    "openai", "anthropic", "google", "google-vertex", "xai", "deepseek",
    "moonshotai", "zhipuai", "alibaba", "alibaba-cn", "volcengine",
    "baidu", "minimax", "stepfun", "sensenova", "mistral", "cohere",
    "nvidia", "perplexity", "amazon-bedrock", "azure", "azure-cognitive-services",
})

# Some providers mirror a first-party one under a suffix
# (e.g. volcengine-coding-plan); they never outrank the base provider.
# '~' prefixes (kilo's '~vendor/') mark unofficial channel copies.
_PROVIDER_STRIP_RE = re.compile(r"^[a-z0-9_~-]+/")
_DATE_SUFFIX_RE = re.compile(r"-(\d{8}|\d{6})$")


def strip_provider_prefix(model_id: str) -> str:
    """'anthropic/claude-sonnet-4.5' -> 'claude-sonnet-4.5'."""
    return _PROVIDER_STRIP_RE.sub("", model_id.lower())


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def identity(model_id: str) -> str:
    """Canonical identity used to group copies of the same model.

    Aggregator naming differs from vendor naming in case, punctuation and
    hyphenation ('claude-sonnet-4.5' vs 'claude-sonnet-4-5', 'glm-5.3' vs
    'glm5.3' vs 'GLM5.3'). We lowercase and drop every non-alphanumeric
    character: case, dots and hyphens are presentation, they never
    distinguish capabilities.
    """
    return _NON_ALNUM_RE.sub("", strip_provider_prefix(model_id).lower())


def base_alias(model_id: str) -> str | None:
    """Strip a trailing date stamp: 'doubao-seed-1-6-251015' -> 'doubao-seed-1-6'.

    Vendors ship dated snapshots; the bare family name is the alias people
    actually type. Returns None when there is no date suffix.
    """
    m = _DATE_SUFFIX_RE.search(model_id)
    if not m:
        return None
    return model_id[: m.start()]


def to_usd_per_mtok(amount: float, currency: str = "USD", unit: str = "mtok", cny_rate: float = 7.2) -> float:
    """Normalize a price to USD per million tokens.

    The table forbids any other currency/unit (hard rule 3). models.dev is
    already USD/MTok; this is the guard for upstream format drift and for
    vendors that quote CNY / per-1k-token prices.
    """
    currency = currency.upper()
    if currency == "CNY":
        amount = amount / cny_rate
    elif currency != "USD":
        raise ValueError(f"unsupported currency: {currency}")
    unit = unit.lower()
    if unit in ("tok", "token", "tokens", "1"):
        amount = amount * 1_000_000
    elif unit in ("k", "1k", "ktok"):
        amount = amount * 1_000
    elif unit in ("m", "1m", "mtok", "mtokens"):
        pass
    else:
        raise ValueError(f"unsupported unit: {unit}")
    return round(amount, 6)


def _num(value):
    """Coerce to float or None; tolerate numeric strings."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_cost(raw: dict | None, cny_rate: float) -> dict | None:
    """models.dev cost {input, output, cache_read} -> table cost shape."""
    if not isinstance(raw, dict):
        return None
    return {
        "input_per_mtok": _num(raw.get("input")),
        "output_per_mtok": _num(raw.get("output")),
        "cache_read_per_mtok": _num(raw.get("cache_read")),
    }


FACT_FIELDS = (
    "context_window", "max_output", "tool_call", "reasoning",
    "structured_output", "modalities", "attachment", "open_weights",
    "cost_input", "cost_output",
)


def completeness(rec: dict) -> int:
    """Count non-null facts; used to prefer richer copies of a model."""
    n = 0
    m = rec
    for f in FACT_FIELDS:
        v = m.get(f)
        if v is None:
            continue
        if f == "modalities" and not (v.get("input") or v.get("output")):
            continue
        if f in ("cost_input", "cost_output") and v == 0.0:
            # 0-cost plan mirrors carry no pricing information
            continue
        n += 1
    return n


def winner_sort_key(item: tuple[str, dict]):
    """Lower is better: (tier, -completeness, cn-preference, provider, model_id)."""
    provider, rec = item
    tier = 0 if provider in FIRST_PARTY else 1
    cn = 0 if provider.endswith("-cn") else 1  # prefer -cn endpoint on ties (CN consumers)
    return (tier, -completeness(rec), cn, provider, rec["model_id"])


def extract_record(provider: str, model_id: str, raw: dict, cny_rate: float) -> dict:
    """One models.dev (provider, model) pair -> flat normalized record."""
    limit = raw.get("limit") or {}
    cost = normalize_cost(raw.get("cost"), cny_rate)
    modalities = raw.get("modalities")
    if not isinstance(modalities, dict):
        modalities = None

    def tri_bool(v):
        return v if isinstance(v, bool) else None

    return {
        "provider": provider,
        "model_id": model_id,
        "bare_id": strip_provider_prefix(model_id),
        "context_window": limit.get("context") if isinstance(limit.get("context"), int) else None,
        "max_output": limit.get("output") if isinstance(limit.get("output"), int) else None,
        "tool_call": tri_bool(raw.get("tool_call")),
        "reasoning": tri_bool(raw.get("reasoning")),
        "structured_output": tri_bool(raw.get("structured_output")),
        "modalities": {
            "input": [str(x) for x in modalities.get("input", [])],
            "output": [str(x) for x in modalities.get("output", [])],
        } if modalities else None,
        "attachment": tri_bool(raw.get("attachment")),
        "open_weights": tri_bool(raw.get("open_weights")),
        "cost_input": _num(cost.get("input_per_mtok")) if cost else None,
        "cost_output": _num(cost.get("output_per_mtok")) if cost else None,
        "cost_cache_read": _num(cost.get("cache_read_per_mtok")) if cost else None,
        "release_date": raw.get("release_date"),
        "last_updated": raw.get("last_updated"),
        "family": raw.get("family"),
    }


# Manual-annotation fields: sync never writes them, but it must preserve
# values from the previous table so weekly refreshes don't wipe manual work.
MANUAL_FIELDS = ("quota_tier", "quality_hint")


def prune(rec: dict) -> dict:
    """Encoding rule: ABSENT field == null. Drop nulls and empty containers.

    Keeps the table under the 1 MB budget and removes the ambiguity of
    'null vs default' — consumers just use .get() and treat absence as unknown.
    """
    out = {}
    for k, v in rec.items():
        if v is None:
            continue
        if k == "aliases" and not v:
            continue
        if k == "cost":
            cost = {ck: cv for ck, cv in v.items() if cv is not None}
            if not cost:
                continue
            v = cost
        out[k] = v
    return out


def preserve_manual(models_out: dict, existing: Path) -> None:
    """Carry manual-annotation fields over from the previous table."""
    if not existing.exists():
        return
    try:
        prev = json.loads(existing.read_text(encoding="utf-8")).get("models", {})
    except (OSError, json.JSONDecodeError):
        return
    carried = 0
    for key, rec in models_out.items():
        old = prev.get(key)
        if not isinstance(old, dict):
            continue
        for f in MANUAL_FIELDS:
            if old.get(f) is not None:
                rec[f] = old[f]
                carried += 1
    if carried:
        print(f"preserved {carried} manual annotation value(s) from previous table")


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": f"{GENERATOR} (+https://github.com)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_table(raw: dict, cny_rate: float) -> tuple[dict, dict]:
    """Group -> dedup -> emit. Returns (table, stats)."""
    groups: dict[str, dict[str, dict]] = {}  # identity -> {provider: record}
    raw_models = 0

    for provider, pdata in raw.items():
        models = pdata.get("models") if isinstance(pdata, dict) else None
        if not isinstance(models, dict):
            continue
        for model_id, m in models.items():
            if not isinstance(m, dict):
                continue
            raw_models += 1
            mid = m.get("id") or model_id
            rec = extract_record(provider, str(mid), m, cny_rate)
            groups.setdefault(identity(mid), {})[provider] = rec

    all_bare_ids = {rec["bare_id"] for g in groups.values() for rec in g.values()}

    models_out: dict[str, dict] = {}
    for ident, copies in groups.items():
        provider, rec = sorted(copies.items(), key=winner_sort_key)[0]
        # aliases: every other spelling seen for this identity + date-stripped base names
        aliases: list[str] = []
        seen = {rec["bare_id"]}
        for other in copies.values():
            b = other["bare_id"]
            if b not in seen:
                seen.add(b)
                aliases.append(b)
            base = base_alias(b)
            if base and base not in seen and base not in all_bare_ids:
                seen.add(base)
                aliases.append(base)

        key = f"{provider}/{rec['bare_id']}"
        models_out[key] = prune({
            "provider": rec["provider"],
            "aliases": sorted(set(aliases)),
            "context_window": rec["context_window"],
            "max_output": rec["max_output"],
            "tool_call": rec["tool_call"],
            "reasoning": rec["reasoning"],
            "structured_output": rec["structured_output"],
            "modalities": rec["modalities"],
            "attachment": rec["attachment"],
            "open_weights": rec["open_weights"],
            "cost": {
                "input_per_mtok": rec["cost_input"],
                "output_per_mtok": rec["cost_output"],
                "cache_read_per_mtok": rec["cost_cache_read"],
            },
            "release_date": rec["release_date"],
            "last_updated": rec["last_updated"],
            "family": rec["family"],
        })  # quota_tier / quality_hint: manual only, added back by preserve_manual()

    models_out = dict(sorted(models_out.items()))
    table = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generator": GENERATOR,
        "models": models_out,
    }
    stats = {
        "providers": sum(1 for p in raw.values() if isinstance(p, dict) and isinstance(p.get("models"), dict)),
        "raw_models": raw_models,
        "identities": len(groups),
        "models": len(models_out),
    }
    return table, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sync models.dev into the Facet model registry table.")
    ap.add_argument("--source", default=SOURCE_URL, help="URL of the models.dev api.json")
    ap.add_argument("--offline", help="path to a local raw api.json snapshot (no network)")
    ap.add_argument("--out", default="registry/model-registry.json", help="output table path")
    ap.add_argument("--cny-rate", type=float, default=7.2, help="CNY->USD rate for price normalization")
    args = ap.parse_args(argv)

    if args.offline:
        raw = json.loads(Path(args.offline).read_text(encoding="utf-8"))
        print(f"loaded offline snapshot: {args.offline}")
    else:
        raw = fetch(args.source)
        print(f"fetched {args.source}")

    table, stats = build_table(raw, args.cny_rate)
    out = Path(args.out)
    preserve_manual(table["models"], out)  # manual annotations survive re-sync
    payload = json.dumps(table, ensure_ascii=False, indent=None, separators=(",", ":"))
    size = len(payload.encode("utf-8"))

    print(f"providers={stats['providers']} raw_models={stats['raw_models']} "
          f"identities={stats['identities']} emitted={stats['models']} size={size / 1024:.1f} KB")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(payload + "\n", encoding="utf-8")
    print(f"wrote {out}")

    if stats["models"] < 500:
        print(f"WARNING: only {stats['models']} models (< 500 target)", file=sys.stderr)
    if size > MAX_TABLE_BYTES:
        print(f"ERROR: table is {size} bytes, exceeds {MAX_TABLE_BYTES} budget", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
