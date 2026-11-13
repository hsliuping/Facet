#!/usr/bin/env python3
"""Installer hook: write the chosen port into data/router.json.

Called by Inno Setup at ssPostInstall as:
    runtime\python.exe app\set_port.py --apply <port>

The wizard always defaults to 8787, so on an upgrade "8787" may simply mean
"the user did not touch the box". In that case an existing custom port is
kept instead of being clobbered back to 8787. Any other chosen value (or a
missing config on first install) is written as-is. The console shortcut is
rewritten to match; the tray and the browser agent read the same file.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CFG = DATA / "router.json"
URL = ROOT / "打开控制台.url"


def read_port(cfg: dict) -> int:
    try:
        return int((cfg.get("listen") or {}).get("port") or 0)
    except (ValueError, TypeError):
        return 0


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "--apply":
        print("usage: set_port.py --apply <port>", file=sys.stderr)
        return 2
    try:
        chosen = int(sys.argv[2])
    except ValueError:
        print("port must be an integer", file=sys.stderr)
        return 2
    if not 1024 <= chosen <= 65535:
        print("port must be 1024-65535", file=sys.stderr)
        return 2

    if not CFG.is_file():
        # First install: seed the zero-key config here so listen.port lands
        # in the same file the gateway will serve from.
        from facet.gateway.seed import write_seed
        write_seed(CFG)

    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    existing = read_port(cfg)
    if chosen == 8787 and 1024 <= existing <= 65535 and existing != 8787:
        # Upgrade with the untouched wizard default: keep the custom port.
        port = existing
    else:
        port = chosen
    cfg.setdefault("listen", {})["port"] = port
    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    URL.write_text(f"[InternetShortcut]\r\nURL=http://127.0.0.1:{port}/\r\n", encoding="utf-8")
    print(f"router.json listen.port={port}; console shortcut updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
