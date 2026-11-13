"""Container entrypoint: seed /data/router.json, wire the shipped DSH profile,
then exec the gateway.

The default seed matches a normal (non-container) first run: the vendor-preset
channels from facet.gateway.seed, retargeted for the container (0.0.0.0 listen,
key references resolved from the environment). Set ROUTER_SEED=example to seed
the mock smoke-test config from router.example.json instead.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
from pathlib import Path

TEMPLATE = Path("/opt/facet/gateway/router.example.json")
DATA = Path("/data")
CONFIG = DATA / "router.json"
DSH_PROFILE = Path("/opt/facet/gateway/dsh-profile")
DSH_PROFILE_NAME = "facet-web"

EXAMPLE_SEED = ("example", "mock", "smoke")


def seed_dsh_profile() -> None:
    """Install the shipped DSH profile into $HOME/.dsh on first run.

    DSH writes its profile state next to the profile, and $HOME is /data, so
    the seeded copy lands in the bind mount and survives a container recreate.
    An existing profile is left alone: the console keeps its model list fresh.
    """
    target = Path.home() / ".dsh" / "profiles" / DSH_PROFILE_NAME
    if (target / "package.json").is_file():
        return
    shutil.copytree(DSH_PROFILE, target)
    print(f"[entrypoint] seeded {target}", flush=True)


def _interface_ip() -> str:
    """Container interface address, i.e. what a published port DNATs to."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect only resolves the route; no packet leaves the host.
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def publish_dsh_web() -> None:
    """Tell the launcher how the DSH web UI is reachable from the browser.

    DSH refuses wildcard binds, and its printed URL points at its own
    loopback, which is the container's — not the host's — 127.0.0.1. So bind
    the container interface, advertise the published host address, and trust
    that authority at the browser-facing API fence.
    """
    port = os.environ.get("DSH_PORT", "").strip() or "3080"
    host = _interface_ip()
    if not host:
        return
    os.environ.setdefault("DSH_WEB_HOST", host)
    os.environ.setdefault("DSH_WEB_PUBLIC_URL", f"http://127.0.0.1:{port}/")


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
    from facet.gateway.seed import build_seed

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
    seed_dsh_profile()
    publish_dsh_web()
    os.execvp("facet", ["facet", "serve", "--config", str(CONFIG), *sys.argv[1:]])


if __name__ == "__main__":
    main()
