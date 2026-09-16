#!/usr/bin/env python3
"""Validate registry/model-registry.json against the Facet table contract.

Stdlib-only structural validator (no jsonschema dependency). Enforces the
hard rules that make the table trustworthy:

  1. single file < 1 MB
  2. schema_version == 1, required header fields
  3. every record has a provider
  4. ABSENT == null encoding: NO nulls anywhere in the table
  5. no empty aliases; tri-state fields are real booleans
  6. cost values are non-negative numbers; dates match YYYY-MM-DD
  7. quota_tier / quality_hint take only their documented values

Usage:
    python tools/validate_registry.py [--table registry/model-registry.json]
                                      [--min-models 500]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MAX_TABLE_BYTES = 1024 * 1024
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# record dates may be YYYY-MM when the upstream day is unknown
RECORD_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
KEY_RE = re.compile(r"^[A-Za-z0-9@~][A-Za-z0-9._/:@~-]*$")

BOOL_FIELDS = ("tool_call", "reasoning", "structured_output", "attachment", "open_weights")
QUOTA_TIERS = {"free_forever", "free_daily", "paid"}


def check_null_free(node, path: str, errors: list[str]) -> None:
    """Hard rule: an ABSENT field is null. A literal null in the file is a bug."""
    if node is None:
        errors.append(f"{path}: literal null (encoding rule says ABSENT == null)")
    elif isinstance(node, dict):
        for k, v in node.items():
            check_null_free(v, f"{path}.{k}", errors)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            check_null_free(v, f"{path}[{i}]", errors)


def validate(table: dict, min_models: int) -> list[str]:
    errors: list[str] = []
    if table.get("schema_version") != 1:
        errors.append(f"schema_version must be 1, got {table.get('schema_version')!r}")
    for field in ("updated_at", "generator"):
        if not table.get(field):
            errors.append(f"missing header field: {field}")
    if table.get("updated_at") and not DATE_RE.match(table["updated_at"]):
        errors.append(f"updated_at not YYYY-MM-DD: {table['updated_at']!r}")

    models = table.get("models")
    if not isinstance(models, dict) or not models:
        return errors + ["models must be a non-empty object"]
    if len(models) < min_models:
        errors.append(f"only {len(models)} models (< {min_models})")

    for key, rec in models.items():
        where = f"models[{key}]"
        if not KEY_RE.match(key):
            errors.append(f"{where}: key violates key pattern")
        if not isinstance(rec, dict):
            errors.append(f"{where}: record must be an object")
            continue
        check_null_free(rec, where, errors)
        if not isinstance(rec.get("provider"), str) or not rec.get("provider"):
            errors.append(f"{where}: provider missing/empty")
        aliases = rec.get("aliases")
        if aliases is not None:
            if not isinstance(aliases, list) or not aliases or \
               not all(isinstance(a, str) and a for a in aliases):
                errors.append(f"{where}: aliases must be a non-empty string array when present")
        for f in BOOL_FIELDS:
            if f in rec and not isinstance(rec[f], bool):
                errors.append(f"{where}: {f} must be boolean when present")
        cost = rec.get("cost")
        if cost is not None:
            if not isinstance(cost, dict) or not cost:
                errors.append(f"{where}: cost must be a non-empty object when present")
            else:
                for f, v in cost.items():
                    if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                        errors.append(f"{where}: cost.{f} must be a non-negative number")
        for f in ("release_date", "last_updated"):
            if f in rec and not (isinstance(rec[f], str) and RECORD_DATE_RE.match(rec[f])):
                errors.append(f"{where}: {f} not YYYY-MM[-DD]")
        qt = rec.get("quota_tier")
        if qt is not None and qt not in QUOTA_TIERS:
            errors.append(f"{where}: quota_tier {qt!r} not in {sorted(QUOTA_TIERS)}")
        qh = rec.get("quality_hint")
        if qh is not None and not (isinstance(qh, int) and not isinstance(qh, bool) and 1 <= qh <= 5):
            errors.append(f"{where}: quality_hint must be int 1-5 when present")
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate a Facet registry table.")
    ap.add_argument("--table", type=Path, default=Path("registry/model-registry.json"))
    ap.add_argument("--min-models", type=int, default=500)
    args = ap.parse_args(argv)

    if not args.table.exists():
        print(f"ERROR: table not found: {args.table}", file=sys.stderr)
        return 1
    size = args.table.stat().st_size
    try:
        table = json.loads(args.table.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON: {e}", file=sys.stderr)
        return 1

    errors = validate(table, args.min_models)
    if size > MAX_TABLE_BYTES:
        errors.append(f"table is {size} bytes (> {MAX_TABLE_BYTES} budget)")

    n = len(table.get("models", {}))
    if errors:
        print(f"INVALID: {args.table} ({n} models, {size / 1024:.1f} KB) — {len(errors)} error(s)")
        for e in errors[:50]:
            print(f"  - {e}")
        return 1
    print(f"OK: {args.table} ({n} models, {size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
