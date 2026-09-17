"""CLI: ``facet "glm-5.3"`` resolves one name; ``facet --tools ...`` filters + ranks.

Output format matches examples/pick_model.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import _find, _identity, _table


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
    ap = argparse.ArgumentParser(
        prog="facet", description="Pick models from the Facet registry table."
    )
    ap.add_argument("name", nargs="?", help="resolve one model by name or alias and print its facts")
    ap.add_argument("--table", type=Path, default=None, help="registry table path (default: bundled snapshot)")
    ap.add_argument("--min-context", type=int, default=None, help="minimum context window (tokens)")
    ap.add_argument("--max-input-price", type=float, default=None, help="max USD per MTok input")
    ap.add_argument("--max-output-price", type=float, default=None, help="max USD per MTok output")
    ap.add_argument("--tools", action="store_true", help="require tool calling")
    ap.add_argument("--reasoning", action="store_true", help="require reasoning")
    ap.add_argument("--json", action="store_true", help="require structured output")
    ap.add_argument("--image-input", action="store_true", help="require image input")
    ap.add_argument("--provider", default=None, help="only models served by this provider")
    ap.add_argument("--limit", type=int, default=10, help="how many rows to print")
    ap.add_argument("--emit-json", action="store_true", help="output JSON instead of text")
    args = ap.parse_args(argv)

    filters = dict(
        min_context=args.min_context,
        max_input_price=args.max_input_price,
        max_output_price=args.max_output_price,
        tool_call=args.tools,
        reasoning=args.reasoning,
        json_output=args.json,
        image_input=args.image_input,
        provider=args.provider,
    )

    try:
        table = _table.load(args.table)
    except (RuntimeError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.name:
        key = _identity.resolve(table, args.name)
        if key is None:
            print(f"not found: {args.name!r} (name not in table keys or aliases)", file=sys.stderr)
            return 1
        entries = [(key, table["models"][key])]
    else:
        entries = _find.find(table, limit=None, **filters)

    rows = entries[: args.limit]
    if args.emit_json:
        print(json.dumps({k: r for k, r in rows}, ensure_ascii=False, indent=2))
        return 0

    label = args.table.name if args.table else "bundled snapshot"
    print(
        f"{len(entries)} match(es), showing {len(rows)} (table {label}, "
        f"updated {table['updated_at']})\n"
    )
    for key, rec in rows:
        print(key)
        print(f"    {explain(rec)}")
        if args.name:
            failed = _find.check(rec, **filters)
            if failed:
                print(f"    (matches requested name; fails: {', '.join(failed)})")
    if not entries:
        print("Nothing matched. Loosen a constraint — every flag ANDs together.")
    return 0
