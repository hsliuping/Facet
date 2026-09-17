"""Table loading: bundled snapshot, explicit local path, or refresh over HTTP."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

#: Snapshot shipped inside the wheel (copied at build time by tools/build_package.py).
BUNDLED_TABLE = Path(__file__).resolve().parent / "data" / "model-registry.json"

#: Default refresh source: this repo's raw table on main.
DEFAULT_TABLE_URL: str | None = (
    "https://raw.githubusercontent.com/hsliuping/Facet/main/registry/model-registry.json"
)

#: Environment variable consulted by load(refresh=True), between url= and the default.
TABLE_URL_ENV = "FACET_TABLE_URL"


def _validate(table: object) -> dict:
    """Minimal contract check for anything facet loads (facts must be v1 shape)."""
    if not isinstance(table, dict):
        raise ValueError("table must be a JSON object")
    if table.get("schema_version") != 1:
        raise ValueError(f"unsupported schema_version: {table.get('schema_version')!r} (expected 1)")
    if not isinstance(table.get("models"), dict):
        raise ValueError("table is missing a 'models' object")
    return table


def load(source: str | Path | None = None, *, refresh: bool = False, url: str | None = None) -> dict:
    """Load the registry table.

    source   explicit local JSON path; defaults to the bundled snapshot
    refresh  fetch the latest table over HTTP(S) instead of reading a file
    url      override the refresh URL; else the FACET_TABLE_URL env var;
             else DEFAULT_TABLE_URL

    Failures raise — a facts table must never silently fall back to stale
    data. Refresh without any URL raises RuntimeError with a fix-it hint.
    """
    if refresh:
        target = url or os.environ.get(TABLE_URL_ENV) or DEFAULT_TABLE_URL
        if not target:
            raise RuntimeError(
                f"refresh requested but no table URL: pass url=... or set the "
                f"{TABLE_URL_ENV} environment variable"
            )
        try:
            with urllib.request.urlopen(target, timeout=30) as resp:
                raw = resp.read()
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"failed to fetch table from {target}: {e}") from e
        try:
            table = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise RuntimeError(f"table fetched from {target} is not valid JSON: {e}") from e
        return _validate(table)

    path = BUNDLED_TABLE if source is None else Path(source)
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        if source is None:
            raise RuntimeError(
                "bundled snapshot missing; run 'python tools/build_package.py' "
                "or pass an explicit path: facet.load('registry/model-registry.json')"
            ) from e
        raise
    return _validate(table)
