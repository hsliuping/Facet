"""Persist live channel health so multi-hour quota cooldowns survive restarts."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

STATE_FIELDS = (
    "cooldown_until", "reason", "last_error", "consecutive_errors", "half_open",
    "total_requests", "served_requests", "failed_requests", "total_tokens",
    "handoffs", "last_used",
)
WINDOW_SECONDS = 86400


def capture(store, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    channels = {}
    for name, state in store.states.items():
        entry = {field: getattr(state, field) for field in STATE_FIELDS}
        entry["req_times"] = [t for t in state.req_times if 0 <= now - t < WINDOW_SECONDS]
        entry["tok_events"] = [[t, n] for t, n in state.tok_events
                               if 0 <= now - t < WINDOW_SECONDS]
        channels[name] = entry
    return {"saved_at": now, "channels": channels}


def restore(store, data: dict, now: float | None = None) -> int:
    """Apply a snapshot to an existing StateStore. Return restored channel count."""
    now = time.time() if now is None else now
    restored = 0
    for name, entry in (data.get("channels") or {}).items():
        state = store.states.get(name)
        if not state or not isinstance(entry, dict):
            continue
        for field in STATE_FIELDS:
            if field in entry:
                setattr(state, field, entry[field])
        state.req_times = deque(
            float(t) for t in (entry.get("req_times") or [])
            if isinstance(t, (int, float)) and 0 <= now - float(t) < WINDOW_SECONDS
        )
        state.tok_events = deque(
            (float(t), int(n)) for t, n in (entry.get("tok_events") or [])
            if isinstance(t, (int, float)) and 0 <= now - float(t) < WINDOW_SECONDS
        )
        if state.cooldown_until <= now:
            state.cooldown_until = 0.0
            state.reason = ""
            state.half_open = False
        restored += 1
    return restored


class StatePersistence:
    """Periodically write a snapshot; a background thread coalesces changes."""

    def __init__(self, path, store_getter, interval: float = 5.0):
        self.path = Path(path)
        self._get_store = store_getter
        self.interval = interval
        self._last = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="planrouter-state")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        self.flush()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.flush()

    def restore_from_disk(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            restore(self._get_store(), data)
            self._last = json.dumps(capture(self._get_store())["channels"], sort_keys=True)
        except (OSError, ValueError, TypeError):
            return

    def flush(self) -> None:
        with self._lock:
            snapshot = capture(self._get_store())
            compare = json.dumps(snapshot["channels"], sort_keys=True)
            if compare == self._last:
                return
            tmp = self.path.with_name(self.path.name + ".tmp")
            try:
                tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
                os.replace(tmp, self.path)
                self._last = compare
            except OSError:
                pass
