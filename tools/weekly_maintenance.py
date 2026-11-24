#!/usr/bin/env python3
"""Weekly self-maintenance for the Facet registry (R9).

One scheduled entry point that keeps the model fact table rolling without
manual steps; the human only reviews the git diff afterwards:

  1. sync upstream sources into a STAGING table (tools/sync_models_dev.py)
  2. validate the staging table with tools/validate_registry.py's contract —
     the live registry is only replaced when staging passes, so a bad
     upstream fetch can never clobber the last good table
  3. refresh the bundled package snapshot (tools/build_package.py --skip-build;
     dist/ wheel rebuilds stay a release-time action)
  4. cross-check the registry against the channel catalog in the router
     config: channel wire models unknown to the registry, and registry
     free-quota models not exposed by any enabled channel
  5. regenerate route suggestions from registry facts: per protocol, the
     facet-auto chain (agent work: tool_call + >=128k context, free channels
     first) and the facet-cheap chain (dispatch: free/cheap first) — written
     to registry/route-suggestions.json; adopting a chain into router.json
     stays a deliberate manual edit
  6. append one JSON line to registry/maintenance-log.jsonl

Note the two different "建议" in this codebase: the route suggestions
written to registry/route-suggestions.json are routing-chain proposals for a
human to review and merge into router.json by hand, nothing like the gateway
console's maintenance suggestions (维护建议, runtime channel-config advice
with one-click apply in the self-maintenance page).

stdlib only. Usage:
    python tools/weekly_maintenance.py                     # full run (network)
    python tools/weekly_maintenance.py --skip-sync         # reuse the live table
    python tools/weekly_maintenance.py --skip-bench-sync   # skip public score fetch
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
sys.path.insert(0, str(TOOLS))
import sync_models_dev  # noqa: E402  (identity, FIRST_PARTY)
import validate_registry  # noqa: E402

SYNC_SCRIPT = TOOLS / "sync_models_dev.py"
BENCH_SYNC_SCRIPT = TOOLS / "sync_benchmarks.py"
BUILD_SCRIPT = TOOLS / "build_package.py"
STAGING = REPO / "registry" / ".staging-model-registry.json"
SUGGESTIONS = REPO / "registry" / "route-suggestions.json"
MAINTENANCE_LOG = REPO / "registry" / "maintenance-log.jsonl"
GENERATOR = "weekly_maintenance@0.1.0"

# facet-auto serves agent work: a full working context plus tool calling.
AGENT_MIN_CONTEXT = 131072  # 128k

_DANGLING_RE = re.compile(r"skipped (\d+) dangling override annotation")

PROTOCOLS = ("openai", "anthropic")
BRANDED_ROUTES = {"facet-auto": "default", "facet-cheap": "cheap"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(base: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else base / p


def flagship_wire_model(models: dict | None) -> str | None:
    """Mirror core.dispatch_wire_model: the auto mapping wins, else the first
    explicit wire model; empty/'*' mappings never count."""
    models = models or {}
    auto = models.get("auto")
    if auto and auto != "*":
        return auto
    for key, value in models.items():
        if key not in ("auto", "*") and value and value != "*":
            return value
    return None


def build_resolver(table: dict):
    """resolve(wire_name) -> (registry_key, record) | None.

    Matches channel wire names against table keys, aliases and folded
    identities (case/punctuation-insensitive, provider prefix tolerated).
    First-party provider copies win ties, mirroring sync winner rules.
    """
    models = table.get("models", {})
    by_bare: dict[str, list] = {}
    by_alias: dict[str, list] = {}
    by_ident: dict[str, list] = {}
    for key, rec in models.items():
        bare = key.split("/", 1)[1] if "/" in key else key
        by_bare.setdefault(bare, []).append((key, rec))
        for alias in rec.get("aliases") or []:
            by_alias.setdefault(alias, []).append((key, rec))
        for ident_src in (bare, *(rec.get("aliases") or [])):
            by_ident.setdefault(sync_models_dev.identity(ident_src), []).append((key, rec))

    def resolve(name: str):
        pools = (by_bare.get(name), by_alias.get(name),
                 by_ident.get(sync_models_dev.identity(name)))
        for pool in pools:
            if pool:
                return sorted(pool, key=lambda kv: (
                    0 if kv[1].get("provider") in sync_models_dev.FIRST_PARTY else 1,
                    kv[0]))[0]
        return None

    return resolve


def crosscheck(config: dict, table: dict, resolve) -> dict:
    """Diff the channel catalog against the registry (report-only).

    - unknown_channel_models: enabled-channel wire names resolving to nothing
      (renamed / dropped upstream, or typo in the catalog)
    - flagships: per enabled channel, the wire flagship and its registry key
    - unexposed_free_models: registry entries annotated quota_tier free_* that
      no enabled channel model matches anymore (stale annotations)
    """
    enabled = [c for c in config.get("channels", []) if c.get("enabled")]
    exposed_idents: set[str] = set()
    unknown: dict[str, list[str]] = {}
    flagships: dict[str, dict] = {}
    for ch in enabled:
        name = ch.get("name", "?")
        models = ch.get("models") or {}
        # every wire value the channel can call: auto mapping included
        wires = sorted({w for w in models.values() if w and w != "*"})
        unknown[name] = []
        for wire in wires:
            hit = resolve(wire)
            if hit is None:
                unknown[name].append(wire)
            else:
                bare = hit[0].split("/", 1)[1]
                exposed_idents.add(sync_models_dev.identity(bare))
        fw = flagship_wire_model(models)
        if fw:
            hit = resolve(fw)
            flagships[name] = {"wire": fw, "registry_key": hit[0] if hit else None}
    unexposed = []
    for key, rec in table.get("models", {}).items():
        if str(rec.get("quota_tier", "")).startswith("free_"):
            bare = key.split("/", 1)[1]
            if sync_models_dev.identity(bare) not in exposed_idents:
                unexposed.append(key)
    return {
        "unknown_channel_models": {k: v for k, v in unknown.items() if v},
        "flagships": flagships,
        "unexposed_free_models": sorted(unexposed),
    }


def _evidence(name: str, facts: dict) -> dict:
    rec = facts["rec"]
    return {
        "tier": facts["tier"] or "unknown",
        "wire": facts["wire"],
        "model": facts["registry_key"],
        "context_window": rec.get("context_window"),
        "tool_call": rec.get("tool_call"),
        "output_per_mtok": (rec.get("cost") or {}).get("output_per_mtok"),
    }


def suggest_routes(config: dict, table: dict, resolve) -> dict:
    """Per protocol: facet-auto and facet-cheap chains ranked from registry
    facts. Report-only — adopting a chain into router.json is a manual edit.
    """
    facts: dict[str, dict] = {}
    for ch in config.get("channels", []):
        if not ch.get("enabled") or ch.get("protocol") not in PROTOCOLS:
            continue
        fw = flagship_wire_model(ch.get("models") or {})
        if not fw:
            continue
        hit = resolve(fw)
        facts[ch["name"]] = {
            "protocol": ch["protocol"],
            "tier": ch.get("tier") or "",
            "wire": fw,
            "registry_key": hit[0] if hit else None,
            "rec": hit[1] if hit else {},
        }

    def free_rank(name: str) -> int:
        return 0 if facts[name]["tier"] == "free" else 1

    auto_pool: dict[str, list[str]] = {p: [] for p in PROTOCOLS}
    cheap_pool: dict[str, list[str]] = {p: [] for p in PROTOCOLS}
    skipped = {"unresolved_flagship": [], "no_tool_call": [], "small_context": []}
    for name, f in facts.items():
        if not f["registry_key"]:
            skipped["unresolved_flagship"].append(name)
            continue
        rec = f["rec"]
        cheap_pool[f["protocol"]].append(name)
        ctx = rec.get("context_window")
        if rec.get("tool_call") is True and isinstance(ctx, int) and ctx >= AGENT_MIN_CONTEXT:
            auto_pool[f["protocol"]].append(name)
        elif rec.get("tool_call") is not True:
            skipped["no_tool_call"].append(name)
        else:
            skipped["small_context"].append(name)

    def order(names: list[str], cheap_mode: bool) -> list[str]:
        def key(n: str):
            rec = facts[n]["rec"]
            out_cost = (rec.get("cost") or {}).get("output_per_mtok")
            if cheap_mode:
                return (free_rank(n), out_cost if out_cost is not None else float("inf"),
                        rec.get("context_window") or 1 << 40, n)
            return (free_rank(n), -(rec.get("context_window") or 0), n)
        return sorted(names, key=key)

    routes = {(r.get("protocol"), r.get("name")): r.get("channels", [])
              for r in config.get("routes", [])}
    suggestions = {}
    for proto in PROTOCOLS:
        block = {}
        for branded, route_name in BRANDED_ROUTES.items():
            pool = auto_pool[proto] if branded == "facet-auto" else cheap_pool[proto]
            chain = order(pool, cheap_mode=(branded == "facet-cheap"))
            block[branded] = {
                "current": routes.get((proto, route_name)),
                "suggested": chain,
                "evidence": {n: _evidence(n, facts[n]) for n in chain},
            }
        suggestions[proto] = block
    return {
        "generated_at": now_iso(),
        "generator": GENERATOR,
        "policy": {
            "free_first": True,
            "agent_min_context": AGENT_MIN_CONTEXT,
            "note": "report-only; adopting a chain into router.json is a manual edit",
        },
        "suggestions": suggestions,
        "skipped": skipped,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="weekly registry self-maintenance (R9)")
    ap.add_argument("--config", default="data/router.json",
                    help="router config holding the channel catalog")
    ap.add_argument("--registry", default="registry/model-registry.json")
    ap.add_argument("--skip-sync", action="store_true",
                    help="skip the upstream fetch; reuse the live table")
    ap.add_argument("--skip-bench-sync", action="store_true",
                    help="skip fetching public benchmark scores (Vals)")
    args = ap.parse_args(argv)

    registry_path = _path(REPO, args.registry)
    config_path = _path(REPO, args.config)
    log = {"ts": now_iso(), "ok": False, "steps": {}}

    try:
        if args.skip_sync:
            live = json.loads(registry_path.read_text(encoding="utf-8"))
        else:
            proc = subprocess.run(
                [sys.executable, str(SYNC_SCRIPT), "--out", str(STAGING)],
                cwd=REPO, capture_output=True, text=True)
            log["steps"]["sync"] = {
                "rc": proc.returncode,
                "tail": (proc.stdout or "")[-300:],
            }
            if proc.returncode != 0:
                raise RuntimeError(f"sync failed rc={proc.returncode}: {(proc.stderr or '')[-400:]}")
            table = json.loads(STAGING.read_text(encoding="utf-8"))
            errors = validate_registry.validate(table, min_models=500)
            if errors:
                raise RuntimeError(f"staging table invalid: {errors[:5]}")
            size = STAGING.stat().st_size
            if size > sync_models_dev.MAX_TABLE_BYTES:
                raise RuntimeError(f"staging table {size} bytes exceeds budget")
            os.replace(STAGING, registry_path)
            m = _DANGLING_RE.search(proc.stdout or "")
            log["steps"]["promote"] = {
                "models": len(table.get("models", {})),
                "size_bytes": size,
                "dangling_annotations": int(m.group(1)) if m else 0,
            }
            live = table

        # Refresh public benchmark scores (Vals Terminal-Bench) into
        # registry/model-scores.json before the snapshot is copied, so the
        # bundled data ships the latest scores. Non-fatal: a network/parse
        # failure keeps the last-good table and is logged, never blocks the run.
        if not args.skip_bench_sync:
            bp = subprocess.run([sys.executable, str(BENCH_SYNC_SCRIPT)],
                                cwd=REPO, capture_output=True, text=True)
            log["steps"]["bench_sync"] = {
                "rc": bp.returncode,
                "tail": (bp.stdout or "")[-300:],
            }
            if bp.returncode != 0:
                log.setdefault("warnings", []).append(
                    f"bench_sync failed rc={bp.returncode}: "
                    f"{(bp.stderr or bp.stdout or '')[-200:]}")

        proc = subprocess.run([sys.executable, str(BUILD_SCRIPT), "--skip-build"],
                              cwd=REPO, capture_output=True, text=True)
        log["steps"]["package_snapshot"] = {"rc": proc.returncode}
        if proc.returncode != 0:
            raise RuntimeError(f"build_package failed rc={proc.returncode}: {(proc.stderr or proc.stdout or '')[-400:]}")

        config = json.loads(config_path.read_text(encoding="utf-8"))
        resolve = build_resolver(live)
        cross = crosscheck(config, live, resolve)
        doc = suggest_routes(config, live, resolve)
        SUGGESTIONS.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        log["catalog"] = {
            "unknown_channel_models": cross["unknown_channel_models"],
            "unresolved_flagships": {c: f["wire"] for c, f in cross["flagships"].items()
                                     if not f["registry_key"]},
            "unexposed_free_models": cross["unexposed_free_models"][:30],
            "unexposed_free_count": len(cross["unexposed_free_models"]),
        }
        log["suggestions"] = {
            f"{proto}/{branded}": len(block[branded]["suggested"])
            for proto, block in doc["suggestions"].items()
            for branded in block
        }
        log["ok"] = True
    except Exception as exc:  # noqa: BLE001 — a failed run must still be logged
        log["error"] = str(exc)[:500]
    finally:
        if STAGING.exists():
            STAGING.unlink(missing_ok=True)

    MAINTENANCE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with MAINTENANCE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(log, ensure_ascii=False, separators=(",", ":")) + "\n")

    if log["ok"]:
        n_models = log.get("steps", {}).get("promote", {}).get("models")
        print(f"weekly maintenance OK"
              + (f": {n_models} models" if n_models else " (no sync)")
              + f"; suggestions -> {SUGGESTIONS.name}; log -> {MAINTENANCE_LOG.name}")
        return 0
    print(f"weekly maintenance FAILED: {log.get('error', '?')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
