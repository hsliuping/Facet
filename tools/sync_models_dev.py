#!/usr/bin/env python3
"""Facet sync tool: upstream sources -> registry/model-registry.json.

Primary source: models.dev (widest provider coverage).
Secondary source: OpenRouter /api/v1/models (public, no auth) — merged in
to fill gaps models.dev leaves open (notably structured_output support).

Pipeline: fetch sources (or --offline files) -> merge -> identity grouping
(dedup across providers) -> winner selection (first-party priority,
completeness) -> alias collection -> currency/unit normalization -> emit table.

stdlib only. Usage:
    python tools/sync_models_dev.py                     # live fetch (both sources)
    python tools/sync_models_dev.py --skip-openrouter   # models.dev only
    python tools/sync_models_dev.py --offline raw.json  # cached models.dev snapshot;
                                                       # OpenRouter cache is read from
                                                       # .cache/openrouter_models.json
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
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CACHE = Path(".cache/openrouter_models.json")
GENERATOR = "sync_models_dev@0.2.0"
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

# Capability/metadata fields a winner record may borrow from sibling copies
# of the same model identity (fill-only, never overwrite). Cost fields are
# deliberately excluded: channel pricing genuinely differs.
FILLABLE_FIELDS = (
    "context_window", "max_output", "tool_call", "reasoning",
    "structured_output", "modalities", "attachment", "open_weights",
    "release_date", "family",
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


def apply_overrides(models_out: dict, overrides: dict) -> int:
    """Apply manual additions/corrections from manual-overrides.json.

    Keys are '<provider>/<model_id>' just like the table. A key missing from
    the table adds a fully manual entry (vendors upstream sources ignore,
    e.g. Baidu ERNIE); a key already present is patched field-by-field and
    the manual value wins — it represents a verified correction. Keys
    starting with '_' (documentation) are skipped. Returns entries touched.
    """
    applied = 0
    for key, patch in overrides.items():
        if key.startswith("_") or not isinstance(patch, dict):
            continue
        if key not in models_out:
            models_out[key] = prune(patch)
        else:
            rec = models_out[key]
            for f, v in patch.items():
                if v is None:
                    rec.pop(f, None)
                else:
                    rec[f] = v
        applied += 1
    return applied


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": f"{GENERATOR} (+https://github.com)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _or_price_per_mtok(value) -> float | None:
    """OpenRouter prices are USD per token as strings; convert to USD/MTok.
    Negative prices (subsidies) are treated as unknown to satisfy the
    non-negative cost contract."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return round(v * 1_000_000, 6)


def openrouter_to_modelsdev(or_data: dict) -> dict:
    """OpenRouter /api/v1/models -> models.dev-shaped {openrouter: {models: {...}}}."""
    models: dict[str, dict] = {}
    for entry in or_data.get("data", []):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        arch = entry.get("architecture") or {}
        top = entry.get("top_provider") or {}
        pricing = entry.get("pricing") or {}
        params = entry.get("supported_parameters") or []
        rec: dict = {
            "id": entry["id"],
            "limit": {
                "context": entry.get("context_length") or top.get("context_length"),
                "output": top.get("max_completion_tokens"),
            },
            "cost": {
                "input": _or_price_per_mtok(pricing.get("prompt")),
                "output": _or_price_per_mtok(pricing.get("completion")),
                "cache_read": _or_price_per_mtok(pricing.get("input_cache_read")),
            },
        }
        if arch.get("input_modalities") or arch.get("output_modalities"):
            rec["modalities"] = {
                "input": arch.get("input_modalities") or [],
                "output": arch.get("output_modalities") or [],
            }
        if "tools" in params:
            rec["tool_call"] = True
        if "structured_outputs" in params:
            rec["structured_output"] = True
        if "reasoning" in params:
            rec["reasoning"] = True
        models[entry["id"]] = rec
    return {"openrouter": {"models": models}}


def _merge_into(existing: dict, rec: dict) -> None:
    """Deep-merge rec into existing; existing values win, rec fills gaps."""
    for k, v in rec.items():
        if isinstance(v, dict) and isinstance(existing.get(k), dict):
            _merge_into(existing[k], v)
        elif existing.get(k) is None:
            existing[k] = v


def merge_source(raw: dict, extra: dict) -> int:
    """Merge a converted source into raw. Field-level: existing values win,
    the extra source only fills gaps and adds unseen model ids. Returns the
    number of newly added model entries."""
    added = 0
    for provider, pdata in extra.items():
        slot = raw.setdefault(provider, {"models": {}})
        models = slot.setdefault("models", {})
        for mid, rec in pdata.get("models", {}).items():
            existing = models.get(mid)
            if existing is None:
                models[mid] = rec
                added += 1
            else:
                _merge_into(existing, rec)
    return added


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
        ranked = sorted(copies.items(), key=winner_sort_key)
        provider, rec = ranked[0]
        # Cross-provider gap fill for capability fields: a model's
        # capabilities are the same on every channel, so a missing fact on
        # the winning copy may be borrowed from other copies (most trusted
        # first). Cost is NEVER borrowed — channel pricing genuinely differs.
        # Fill-only: an existing value is never overwritten.
        for field in FILLABLE_FIELDS:
            if rec.get(field) is None:
                for _, other in ranked[1:]:
                    if other.get(field) is not None:
                        rec[field] = other[field]
                        break
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
    ap = argparse.ArgumentParser(description="Sync upstream sources into the Facet model registry table.")
    ap.add_argument("--source", default=SOURCE_URL, help="URL of the models.dev api.json")
    ap.add_argument("--offline", help="path to a local raw api.json snapshot (no network)")
    ap.add_argument("--skip-openrouter", action="store_true",
                    help="do not merge the OpenRouter secondary source")
    ap.add_argument("--overrides", default="registry/manual-overrides.json",
                    help="path to manual additions/corrections (skipped if missing)")
    ap.add_argument("--out", default="registry/model-registry.json", help="output table path")
    ap.add_argument("--cny-rate", type=float, default=7.2, help="CNY->USD rate for price normalization")
    args = ap.parse_args(argv)

    if args.offline:
        raw = json.loads(Path(args.offline).read_text(encoding="utf-8"))
        print(f"loaded offline snapshot: {args.offline}")
    else:
        raw = fetch(args.source)
        print(f"fetched {args.source}")

    if not args.skip_openrouter:
        or_data = None
        if args.offline:
            if OPENROUTER_CACHE.exists():
                or_data = json.loads(OPENROUTER_CACHE.read_text(encoding="utf-8"))
                print(f"loaded offline OpenRouter snapshot: {OPENROUTER_CACHE}")
        else:
            try:
                or_data = fetch(OPENROUTER_URL)
                print(f"fetched {OPENROUTER_URL}")
            except (OSError, json.JSONDecodeError) as e:
                print(f"WARNING: OpenRouter source unavailable ({e}); continuing without it",
                      file=sys.stderr)
        if or_data is not None:
            added = merge_source(raw, openrouter_to_modelsdev(or_data))
            print(f"merged OpenRouter source: {added} new model entries + gap fills")

    table, stats = build_table(raw, args.cny_rate)
    out = Path(args.out)
    preserve_manual(table["models"], out)  # manual annotations survive re-sync

    ov_path = Path(args.overrides)
    if ov_path.exists():
        try:
            overrides = json.loads(ov_path.read_text(encoding="utf-8"))
            n = apply_overrides(table["models"], overrides)
            table["models"] = dict(sorted(table["models"].items()))
            print(f"applied {n} manual override entrie(s) from {ov_path}")
        except (OSError, json.JSONDecodeError) as e:
            print(f"ERROR: cannot read overrides {ov_path}: {e}", file=sys.stderr)
            return 1
    else:
        print(f"no manual overrides file at {ov_path} (skipped)")

    n_models = len(table["models"])
    payload = json.dumps(table, ensure_ascii=False, indent=None, separators=(",", ":"))
    size = len(payload.encode("utf-8"))

    print(f"providers={stats['providers']} raw_models={stats['raw_models']} "
          f"identities={stats['identities']} emitted={n_models} size={size / 1024:.1f} KB")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(payload + "\n", encoding="utf-8")
    print(f"wrote {out}")

    if n_models < 500:
        print(f"WARNING: only {n_models} models (< 500 target)", file=sys.stderr)
    if size > MAX_TABLE_BYTES:
        print(f"ERROR: table is {size} bytes, exceeds {MAX_TABLE_BYTES} budget", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
