#!/usr/bin/env python3
"""Post-install hook: wait until the gateway is reachable, then open the console URL.

Called by Inno Setup [Run] as:
    runtime\pythonw.exe app\open_console.py

It reads the port from data/router.json (default 8787), polls the TCP endpoint
until it accepts a connection or a generous timeout elapses, then opens the browser
to http://127.0.0.1:<port>/ so the operator lands on a ready console rather than
an error page.
"""
from __future__ import annotations

import json
import socket
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # .../build/Windows/Facet
DATA = ROOT / "data"
CFG = DATA / "router.json"

DEFAULT_PORT = 8787
MAX_WAIT = 30.0      # give up after 30 s (more than enough; usually 1‑3 s)
INTERVAL = 0.5       # poll every 500 ms


def read_port() -> int:
    if CFG.is_file():
        try:
            cfg = json.loads(CFG.read_text(encoding="utf-8"))
            return int((cfg.get("listen") or {}).get("port") or DEFAULT_PORT)
        except (ValueError, TypeError, KeyError):
            pass
    return DEFAULT_PORT


def main() -> int:
    port = read_port()
    url = f"http://127.0.0.1:{port}/"
    end_time = time.time() + MAX_WAIT
    while time.time() < end_time:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=1.0)
            sock.close()
            # endpoint accepting connections means the gateway is up
            webbrowser.open(url, new=2)  # open in new tab if possible
            print(f"控制台已打开: {url}")
            return 0
        except (socket.timeout, ConnectionRefusedError, OSError):
            time.sleep(INTERVAL)
    # timeout – still try to open so the operator has a chance; they can
    # manually refresh later or re‑run if the tray never started.
    webbrowser.open(url, new=2)
    print(f"已尝试打开控制台（耗时 {MAX_WAIT:.1f}s），若出现错误请刷新或重启 Facet。")
    return 0


if __name__ == "__main__":
    sys.exit(main())