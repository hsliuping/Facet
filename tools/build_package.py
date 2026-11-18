#!/usr/bin/env python3
"""Build the facet-models distribution from the repo's registry table.

Steps:
  1. validate registry/model-registry.json (quality gate, size + min-models)
  2. copy the table into src/facet/data/ (bundled snapshot)
  3. run `python -m build` to produce sdist + wheel

Usage:
    python tools/build_package.py [--skip-build]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import validate_registry  # noqa: E402
import validate_benchmarks  # noqa: E402

REGISTRY = REPO / "registry" / "model-registry.json"
DATA_DIR = REPO / "src" / "facet" / "data"
BENCH_FILES = ("benchmarks.json", "model-scores.json", "model-coverage.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate, snapshot and build the facet-models package.")
    ap.add_argument("--skip-build", action="store_true",
                    help="stop after copying the table")
    args = ap.parse_args(argv)

    if not REGISTRY.exists():
        print(f"ERROR: table not found: {REGISTRY}", file=sys.stderr)
        return 1
    raw = REGISTRY.read_text(encoding="utf-8")
    size = len(raw.encode())
    if size > validate_registry.MAX_TABLE_BYTES:
        print(f"ERROR: table is {size} bytes (> {validate_registry.MAX_TABLE_BYTES} budget)", file=sys.stderr)
        return 1
    table = json.loads(raw)
    errors = validate_registry.validate(table, min_models=500)
    if errors:
        print(f"ERROR: registry failed validation ({len(errors)} error(s)):", file=sys.stderr)
        for e in errors[:20]:
            print(f"  - {e}", file=sys.stderr)
        return 1

    missing = [f for f in ("model-registry.json",) + BENCH_FILES
               if not (REGISTRY.parent / f).exists()]
    if missing:
        print(f"ERROR: source files missing under registry/: {', '.join(missing)}", file=sys.stderr)
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for fname in ("model-registry.json",) + BENCH_FILES:
        shutil.copyfile(REGISTRY.parent / fname, DATA_DIR / fname)
    print(f"table: {len(table['models'])} models, updated {table['updated_at']} -> src/facet/data/")

    # Benchmark data files ship in the wheel too: gate them before building.
    if validate_benchmarks.main() != 0:
        print("ERROR: benchmark data failed validation", file=sys.stderr)
        return 1

    if args.skip_build:
        print("skipping python -m build (--skip-build)")
        return 0
    proc = subprocess.run([sys.executable, "-m", "build"], cwd=REPO)
    if proc.returncode != 0:
        print("ERROR: python -m build failed", file=sys.stderr)
        return proc.returncode
    print("dist/ ready: sdist + wheel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
