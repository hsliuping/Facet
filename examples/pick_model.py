#!/usr/bin/env python3
"""Facet demo: pick a model from the registry table for your constraints.

This is the reference consumer. It shows the whole contract in ~150 lines of
stdlib Python: load the table, resolve a name (identity + aliases), filter by
hard requirements, rank what survives, and print WHY each row passed.

An ABSENT field is unknown — every lookup uses .get() and treats absence as
null. Nothing is ever filled with a default.

Usage:
    python examples/pick_model.py --min-context 200000 --tools
    python examples/pick_model.py claude-sonnet-4.5
    python examples/pick_model.py --max-output-price 1 --image-input --json
    python examples/pick_model.py --table path/to/model-registry.json --tools
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_TABLE = Path(__file__).resolve().parent.parent / "registry" / "model-registry.json"

# Same normalization as tools/sync_models_dev.py: case, dots and hyphens are
# presentation, they never distinguish capabilities ('glm-5.3' == 'glm5.3').
_PREFIX_RE = re.compile(r"^[a-z0-9_~-]+/")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def identity(name: str) -> str:
    """'OpenRouter/Anthropic/Claude-Sonnet-4.5' -> 'claudesonnet45'."""
    return _NON_ALNUM_RE.sub("", _PREFIX_RE.sub("", name.strip().lower()))


def load_table(path: Path) -> dict:
    table = json.loads(Path(path).read_text(encoding="utf-8"))
    if table.get("schema_version") != 1:
        raise ValueError(f"unsupported schema_version: {table.get('schema_version')}")
    return table


def build_lookup(table: dict) -> dict[str, str]:
    """normalized name -> table key. Canonical keys first, then aliases."""
    lookup: dict[str, str] = {}
    for key, rec in table["models"].items():
        bare = key.split("/", 1)[1]
        lookup.setdefault(identity(bare), key)
        for alias in rec.get("aliases", []):
            lookup.setdefault(identity(alias), key)
    return lookup


def resolve(table: dict, name: str) -> str | None:
    return build_lookup(table).get(identity(name))


def check(rec: dict, req: dict) -> list[str]:
    """Return list of failed requirements (empty = model qualifies)."""
    failed = []
    if req["min_context"] is not None and (rec.get("context_window") or 0) < req["min_context"]:
        failed.append(f"context>={req['min_context']}")
    for flag, field in (("tools", "tool_call"), ("reasoning", "reasoning"), ("json", "structured_output")):
        if req[flag] and rec.get(field) is not True:
            failed.append(field)
    if req["image_input"] and "image" not in (rec.get("modalities") or {}).get("input", []):
        failed.append("image-input")
    cost = rec.get("cost") or {}
    if req["max_input_price"] is not None:
        p = cost.get("input_per_mtok")
        if p is None or p > req["max_input_price"]:
            failed.append(f"input-price<={req['max_input_price']}")
    if req["max_output_price"] is not None:
        p = cost.get("output_per_mtok")
        if p is None or p > req["max_output_price"]:
            failed.append(f"output-price<={req['max_output_price']}")
    return failed


def rank(entries: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Cheapest output first; unknown price last; then bigger context wins.

    Price 0 is treated as unknown (plan mirrors don't expose real pricing —
    same convention as the sync tool's completeness metric).
    """
    def key(item):
        _, rec = item
        out = (rec.get("cost") or {}).get("output_per_mtok")
        if not out:
            out = None
        return (out is None, out or 0.0, -(rec.get("context_window") or 0))
    return sorted(entries, key=key)


def explain(rec: dict) -> str:
    """One human-readable line of facts (absent = ?)."""
    ctx = rec.get("context_window")
    cost = rec.get("cost") or {}
    parts = [
        f"ctx={ctx/1000:.0f}k" if ctx else "ctx=?",
        f"out<={rec['max_output']/1000:.0f}k" if rec.get("max_output") else "out=?",
        "tools" if rec.get("tool_call") else "-tools",
        "reason" if rec.get("reasoning") else "-reason",
        "json" if rec.get("structured_output") else "-json",
        "img-in" if "image" in (rec.get("modalities") or {}).get("input", []) else "-img-in",
    ]
    if cost.get("input_per_mtok") is not None or cost.get("output_per_mtok") is not None:
        i = cost.get("input_per_mtok")
        o = cost.get("output_per_mtok")
        parts.append(f"${'?' if i is None else f'{i:g}'}/${'?' if o is None else f'{o:g}'} per MTok")
    if rec.get("release_date"):
        parts.append(f"released {rec['release_date']}")
    return "  ".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pick models from the Facet registry table.")
    ap.add_argument("name", nargs="?", help="resolve one model by name or alias and print its facts")
    ap.add_argument("--table", type=Path, default=DEFAULT_TABLE, help="registry table path")
    ap.add_argument("--min-context", type=int, default=None, help="minimum context window (tokens)")
    ap.add_argument("--max-input-price", type=float, default=None, help="max USD per MTok input")
    ap.add_argument("--max-output-price", type=float, default=None, help="max USD per MTok output")
    ap.add_argument("--tools", action="store_true", help="require tool calling")
    ap.add_argument("--reasoning", action="store_true", help="require reasoning")
    ap.add_argument("--json", action="store_true", help="require structured output")
    ap.add_argument("--image-input", action="store_true", help="require image input")
    ap.add_argument("--limit", type=int, default=10, help="how many rows to print")
    ap.add_argument("--emit-json", action="store_true", help="output JSON instead of text")
    args = ap.parse_args(argv)

    req = vars(args)
    table = load_table(args.table)

    if args.name:
        key = resolve(table, args.name)
        if key is None:
            print(f"not found: {args.name!r} (name not in table keys or aliases)", file=sys.stderr)
            return 1
        entries = [(key, table["models"][key])]
    else:
        entries = [(k, r) for k, r in table["models"].items() if not check(r, req)]
        entries = rank(entries)

    rows = entries[: args.limit]
    if args.emit_json:
        print(json.dumps({k: r for k, r in rows}, ensure_ascii=False, indent=2))
        return 0

    print(f"{len(entries)} match(es), showing {len(rows)} (table {args.table.name}, "
          f"updated {table['updated_at']})\n")
    for key, rec in rows:
        print(f"{key}")
        print(f"    {explain(rec)}")
        failed = check(rec, req)
        if failed:
            print(f"    (matches requested name; fails: {', '.join(failed)})")
    if not entries:
        print("Nothing matched. Loosen a constraint — every flag ANDs together.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
