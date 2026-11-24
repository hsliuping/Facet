"""Fetch public benchmark scores and merge them into the model score table.

Single source of truth shared by:
  * the dev-time registry updater (``tools/sync_benchmarks.py``, writes
    ``registry/model-scores.json``) and
  * the runtime self-maintenance task (``App.refresh_benchmarks`` in
    ``server.py``, writes a user-dir override that ``benchmarks.py`` layers
    on top of the bundled snapshot).

Only Vals Terminal-Bench 4.0 is wired up today (the most relevant Agent
benchmark for ranking free models); the parser/merger are source-agnostic so
more sources can be added later without touching the callers.

stdlib only — no third-party deps, so it runs inside the installed bundle.
"""
from __future__ import annotations

import re
import urllib.request

from ._identity import identity
from .gateway.benchmarks import _norm_base  # same normalization agent_score uses

VALS_TERMINAL_BENCH_4_URL = "https://www.vals.ai/benchmarks/terminal-bench-4"

# Merge contract — must stay consistent with validate_benchmarks.py rules:
# bench id exists in benches dict, setting in its whitelist, unit in the
# 0-100 comparable set (pct), and every entry carries source_url + date.
BENCH_ID = "terminal_bench_4"
VALS_HARNESS = "vals"
VALS_SETTING = "unspecified"
VALS_UNIT = "pct"
VALS_SOURCE_NAME = "Vals AI Terminal-Bench 4.0"
VALS_SOURCE_TYPE = "independent"  # allowed by test whitelist {vendor, independent, aggregator}

# <tr> ... <a href="/models/<slug>">Name</a> ... <td>NN.NN%</td> ... </tr>
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_LINK_RE = re.compile(r'href=["\']/?models/([^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
_UPDATED_RE = re.compile(r"Updated\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def fetch_url(url: str, timeout: float = 20.0) -> str:
    """Fetch a URL as text (utf-8, decode errors ignored)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "Facet-benchmark-sync/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def parse_vals_updated(html: str) -> str | None:
    """'Updated 11/18/2026' -> '2026-11-18' (None if absent)."""
    m = _UPDATED_RE.search(html)
    if not m:
        return None
    mo, d, y = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def _leaderboard_table(html: str) -> str:
    """Restrict parsing to the leaderboard <table> after the heading.

    The page also renders smaller tables (reasoning-effort splits) whose rows
    carry model links; scoping to the first table past the 'leaderboard'
    marker keeps the row set exactly the published ranking.
    """
    idx = html.lower().find("leaderboard")
    if idx == -1:
        return html
    rest = html[idx:]
    m = re.search(r"<table\b[^>]*>(.*?)</table>", rest, re.S | re.I)
    return m.group(1) if m else rest


def parse_vals_terminal_bench(html: str) -> list[dict]:
    """Parse the Vals leaderboard table into [{slug, name, score}, ...].

    Only rows carrying a ``/models/<slug>`` link are kept — that filters out
    chart/section rows. The first percentage in a row is the accuracy score
    (cost/tokens/duration columns use $ / m / h, never a trailing %).
    """
    rows: list[dict] = []
    for tr in _TR_RE.findall(_leaderboard_table(html)):
        m = _LINK_RE.search(tr)
        if not m:
            continue
        slug = m.group(1).strip()
        name = _TAG_RE.sub("", m.group(2)).strip()
        sm = _PCT_RE.search(tr)
        if not sm:
            continue
        try:
            score = float(sm.group(1))
        except ValueError:
            continue
        if not (0 <= score <= 100):
            continue
        rows.append({"slug": slug, "name": name, "score": score})
    return rows


def _candidate_bases(slug: str, name: str) -> set[str]:
    """Normalized model-name candidates a Vals row could match.

    The slug is ``<vendor>_<model-name>``; the part after the first underscore
    is the model name. The display name is a fallback (e.g. 'GLM 5.3' and
    'glm-5.3' both normalize to the same identity).
    """
    out: set[str] = set()
    if "_" in slug:
        out.add(identity(slug.split("_", 1)[1]))
    out.add(identity(name))
    out.discard("")
    return out


def merge_terminal_bench(
    live: dict,
    rows: list[dict],
    source_url: str,
    date: str,
) -> tuple[dict, dict]:
    """Merge Vals Terminal-Bench 4.0 scores into a live model-scores dict.

    Policy (see plan):
      * only models already present in ``live['models']`` are updated (matched
        by normalized base, identical to ``benchmarks._norm_base``);
      * when several Vals rows match one model, the highest score wins;
      * merge is by (bench, setting, harness) — an existing ``vals`` entry is
        updated in place, otherwise a new entry is appended; other sources'
        entries are left untouched.

    Returns ``(live, report)`` where ``live`` is mutated in place and
    ``report`` describes what changed (and which Vals rows matched nothing).
    """
    models = live.get("models", {})
    base_to_keys: dict[str, list[str]] = {}
    for key in models:
        base_to_keys.setdefault(_norm_base(key), []).append(key)

    key_to_best: dict[str, dict] = {}
    matched_idx: set[int] = set()
    for idx, row in enumerate(rows):
        bases = _candidate_bases(row["slug"], row["name"])
        hit_keys: set[str] = set()
        for b in bases:
            hit_keys.update(base_to_keys.get(b, ()))
        if not hit_keys:
            continue
        matched_idx.add(idx)
        for k in hit_keys:
            cur = key_to_best.get(k)
            if cur is None or row["score"] > cur["score"]:
                key_to_best[k] = row

    report: dict = {"fetched_rows": len(rows), "matched": {}, "unmatched": [], "errors": []}

    def make_entry(value: float) -> dict:
        return {
            "bench": BENCH_ID,
            "value": value,
            "unit": VALS_UNIT,
            "setting": VALS_SETTING,
            "harness": VALS_HARNESS,
            "source_type": VALS_SOURCE_TYPE,
            "source_name": VALS_SOURCE_NAME,
            "source_url": source_url,
            "date": date,
        }

    for key, row in key_to_best.items():
        mdl = models.get(key) or {}
        scores = mdl.setdefault("scores", [])
        old_val = None
        for s in scores:
            if (s.get("bench") == BENCH_ID
                    and s.get("setting") == VALS_SETTING
                    and s.get("harness") == VALS_HARNESS):
                old_val = s.get("value")
                s["value"] = row["score"]
                s["date"] = date
                s["source_url"] = source_url
                s["source_name"] = VALS_SOURCE_NAME
                s["source_type"] = VALS_SOURCE_TYPE
                break
        else:
            scores.append(make_entry(row["score"]))
        models[key] = mdl
        report["matched"][key] = {
            "name": row["name"], "slug": row["slug"],
            "old": old_val, "new": row["score"],
        }

    for idx, row in enumerate(rows):
        if idx not in matched_idx:
            report["unmatched"].append(
                {"name": row["name"], "slug": row["slug"], "score": row["score"]})

    return live, report


def sync_terminal_bench(
    live: dict,
    url: str = VALS_TERMINAL_BENCH_4_URL,
    timeout: float = 20.0,
) -> tuple[dict, dict]:
    """Fetch + parse + merge in one call; network/parse errors go to report."""
    report: dict = {"fetched_rows": 0, "matched": {}, "unmatched": [], "errors": []}
    try:
        html = fetch_url(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — surface, don't crash caller
        report["errors"].append(f"fetch failed: {exc!r}")
        return live, report
    date = parse_vals_updated(html)
    if not date:
        report["errors"].append("could not parse 'Updated' date from page")
        # still proceed with a fallback date so data is never undated
        from datetime import datetime, timezone
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        rows = parse_vals_terminal_bench(html)
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"parse failed: {exc!r}")
        return live, report
    if not rows:
        report["errors"].append("parsed 0 leaderboard rows (page layout changed?)")
        return live, report
    return merge_terminal_bench(live, rows, source_url=url, date=date)
