#!/usr/bin/env python3
"""Fetch public benchmark scores and merge into registry/model-scores.json.

Dev-time half of the ability-score auto-refresh (the runtime half lives in
`facet.gateway.server.App.refresh_benchmarks`). Pulls Vals Terminal-Bench 4.0,
merges only into models already present in the table, writes a staging file,
and only promotes it to the live table when `validate_benchmarks` passes — so
a bad upstream fetch can never clobber the last-good scores.

stdlib only. Usage:
    python tools/sync_benchmarks.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))   # `import facet`
sys.path.insert(0, str(REPO / "tools"))  # `import validate_benchmarks`

from facet import benchmark_sync as bs  # noqa: E402
import validate_benchmarks as vb  # noqa: E402

LIVE = REPO / "registry" / "model-scores.json"
STAGING = REPO / "registry" / ".staging-model-scores.json"
BENCHES = REPO / "registry" / "benchmarks.json"
COVERAGE = REPO / "registry" / "model-coverage.json"
REGISTRY = REPO / "registry" / "model-registry.json"


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _validate(data: dict) -> bool:
    """Run the same gate build_package uses, against the staging dict."""
    benches = _load(BENCHES)
    coverage = _load(COVERAGE)
    registry = _load(REGISTRY)
    ok = True
    checks = [
        ("schema", lambda: vb.check_schema(data, 1, "model-scores")),
        ("bench_in_dict", lambda: vb.check_scores_keys_in_bench_dict(data, benches)),
        ("required_fields", lambda: vb.check_scores_required_fields(data)),
        ("setting_whitelist", lambda: vb.check_bench_setting_in_whitelist(data, benches)),
        ("coverage", lambda: vb.check_coverage_consistency(data, coverage, registry)),
    ]
    for name, fn in checks:
        try:
            if not fn():
                ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR validating {name}: {exc}")
            ok = False
    return ok


def main(argv=None) -> int:
    if not LIVE.exists():
        print(f"ERROR: live scores not found: {LIVE}", file=sys.stderr)
        return 1
    live = _load(LIVE)
    new_live, report = bs.sync_terminal_bench(live)

    for err in report.get("errors", []):
        print(f"WARN sync: {err}")

    STAGING.write_text(
        json.dumps(new_live, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if report.get("errors"):
        # Fetch/parse failed — do not promote, even if staging happens to be
        # valid (it would just be the unchanged old data).
        print("sync had errors; staging discarded, live table untouched", file=sys.stderr)
        if STAGING.exists():
            STAGING.unlink(missing_ok=True)
        return 1

    if not _validate(new_live):
        print("FAIL staging validation failed; live table untouched", file=sys.stderr)
        return 1

    os.replace(STAGING, LIVE)
    matched = report.get("matched", {})
    print(f"sync_benchmarks OK: updated {len(matched)} model(s); "
          f"{len(report.get('unmatched', []))} Vals row(s) matched no registry model")
    for key, info in sorted(matched.items()):
        arrow = f"{info.get('old')} -> {info.get('new')}" if info.get("old") is not None \
            else f"new {info.get('new')}"
        print(f"  {key}: {arrow}  ({info.get('name')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
