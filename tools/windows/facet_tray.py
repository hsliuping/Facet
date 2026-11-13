#!/usr/bin/env python3
"""Facet tray: keeps the gateway running behind a tray icon on Windows.

The bundle layout this expects (written by tools/build_windows.py):

    Facet\
      runtime\python.exe         embedded CPython
      app\facet_tray.py          this file
      app\run_gateway.py         gateway entry (facet.gateway.cli)
      app\tools\computer_use_agent.py
      app\facet.ico
      data\                      router.json, keys.json, logs\
      ms-playwright\             bundled Chromium for the browser agent

Run with `runtime\pythonw.exe app\facet_tray.py` — no console window.
Left-click the icon opens the console; the menu restarts/opens/quit.
The autostart entry passes `--quiet` so a boot does not open a browser tab.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
DATA_DIR = Path(os.environ.get("FACET_DATA_DIR") or ROOT / "data")
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "gateway.log"
PYTHON = ROOT / "runtime" / "python.exe"
GATEWAY_SCRIPT = APP_DIR / "run_gateway.py"
ICON_FILE = APP_DIR / "facet.ico"
MUTEX_NAME = "FacetTrayGateway"


def gateway_port() -> int:
    """Port comes from the user's own router.json so the tray never disagrees."""
    try:
        config = json.loads((DATA_DIR / "router.json").read_text(encoding="utf-8"))
        return int((config.get("listen") or {}).get("port") or 8787)
    except (OSError, ValueError, TypeError):
        return 8787


BASE_URL = f"http://127.0.0.1:{gateway_port()}"
QUIET = "--quiet" in sys.argv[1:]  # autostart: no browser tab on every boot


def child_env() -> dict:
    env = dict(os.environ)
    env.update({
        "FACET_DATA_DIR": str(DATA_DIR),
        "PLAYWRIGHT_BROWSERS_PATH": str(ROOT / "ms-playwright"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


class Gateway:
    """One gateway child process; a second start stops the previous one first."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._log = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.running():
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not (DATA_DIR / "router.json").is_file():
            # cli.main() seeds a first-run config when the file is absent.
            pass
        if self._log is not None:  # close the previous restart's handle
            try:
                self._log.close()
            except OSError:
                pass
        log = self._log = open(LOG_FILE, "ab", buffering=0)
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} starting gateway =====\n".encode())
        self.proc = subprocess.Popen(
            [str(PYTHON), str(GATEWAY_SCRIPT),
             "--config", str(DATA_DIR / "router.json")],
            cwd=str(ROOT), env=child_env(),
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def stop(self) -> None:
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def wait_health(self, seconds: float = 25.0) -> bool:
        """Poll /health until the gateway answers (first start seeds a config)."""
        import urllib.error
        import urllib.request
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(BASE_URL + "/health", timeout=2) as r:
                    return r.status == 200
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.5)
        return False


def already_running() -> bool:
    """A second tray start (double-click) should focus the first one instead."""
    # use_last_error=True is required: ctypes may call other Win32 functions
    # internally, which resets the error a plain GetLastError() would report.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return False
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return True
    globals()["_MUTEX"] = handle  # keep the handle alive for the process lifetime
    return False


def main() -> int:
    if already_running():
        webbrowser.open(BASE_URL + "/")
        return 0

    import pystray
    from PIL import Image

    gateway = Gateway()
    gateway.start()

    icon_image = Image.open(ICON_FILE) if ICON_FILE.is_file() else None
    if icon_image is None:
        icon_image = Image.new("RGB", (64, 64), "#2f6fed")

    def open_console(*_args) -> None:
        webbrowser.open(BASE_URL + "/")

    def open_data(*_args) -> None:
        os.startfile(str(DATA_DIR))  # noqa: S606 — opens Explorer

    def open_log(*_args) -> None:
        if LOG_FILE.is_file():
            os.startfile(str(LOG_FILE))  # noqa: S606
        else:
            open_data()

    def restart_gateway(*_args) -> None:
        gateway.restart()
        if gateway.wait_health():
            icon.notify("网关已重启", "Facet")

    def quit_app(*_args) -> None:
        gateway.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开控制台", open_console, default=True),
        pystray.MenuItem("重启网关", restart_gateway),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("数据目录", open_data),
        pystray.MenuItem("网关日志", open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon(MUTEX_NAME, icon_image, "Facet 网关", menu)

    def boot() -> None:
        # The gateway may need a few seconds on the first start (config seed).
        if gateway.wait_health(30):
            if not QUIET:
                webbrowser.open(BASE_URL + "/")
        else:
            icon.notify("网关启动失败，请查看 data\\logs\\gateway.log", "Facet")

    threading.Thread(target=boot, daemon=True).start()
    icon.run()
    gateway.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
