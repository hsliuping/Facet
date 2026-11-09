"""Container entrypoint: seed /data/router.json on first run, then exec the gateway.

The default seed matches a normal (non-container) first run: the vendor-preset
channels from planrouter.seed, retargeted for the container (0.0.0.0 listen,
key references resolved from the environment). Set ROUTER_SEED=example to seed
the mock smoke-test config from router.example.json instead.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

TEMPLATE = Path("/opt/planrouter/router.example.json")
DATA = Path("/data")
CONFIG = DATA / "router.json"

EXAMPLE_SEED = ("example", "mock", "smoke")


def example_config() -> dict:
    """Read the shipped smoke-test template (mock upstreams, env keys)."""
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def preset_config() -> dict:
    """Vendor-preset seed, containerized.

    build_seed() writes a loopback listen; inside the container the gateway
    must be reachable from outside, and core.py refuses a keyless non-loopback
    listener, so both fields become environment references (the same style the
    example template uses with ROUTER_API_KEY / ROUTER_ADMIN_KEY).
    """
    from planrouter.seed import build_seed

    config = build_seed()
    listen = config.setdefault("listen", {})
    listen["host"] = "0.0.0.0"
    listen["port"] = 8787
    listen["api_key"] = "env:ROUTER_API_KEY"
    listen["admin_key"] = "env:ROUTER_ADMIN_KEY"
    return config


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        if os.environ.get("ROUTER_SEED", "").strip().lower() in EXAMPLE_SEED:
            seed = example_config()
            origin = "example (smoke-test config)"
        else:
            seed = preset_config()
            origin = "vendor presets (set ROUTER_SEED=example for the mock config)"
        CONFIG.write_text(
            json.dumps(seed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[entrypoint] seeded {CONFIG} with {origin}", flush=True)
    # All runtime files (state, usage, subscriptions, notifications, audit log)
    # are derived from the config path, so a single /data volume persists them.
    os.execvp("planrouter", ["planrouter", "--config", str(CONFIG), *sys.argv[1:]])


if __name__ == "__main__":
    main()
