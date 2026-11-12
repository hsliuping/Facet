"""Persist live channel health so multi-hour quota cooldowns survive restarts."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

STATE_FIELDS = (
    "cooldown_until", "model_cooldown_until", "reason", "last_error",
    "consecutive_errors", "half_open", "total_requests", "served_requests",
    "failed_requests", "total_tokens", "handoffs", "last_used", "quota",
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
    catalogs = {
        name: dict(entry) for name, entry in getattr(store, "catalogs", {}).items()
    }
    rr_cursor = {
        key: int(offset) for key, offset in getattr(store, "rr_cursor", {}).items()
    }
    scores = {
        key: [float(value[0]), float(value[1])]
        for key, value in getattr(store, "scores", {}).items()
    }
    return {"saved_at": now, "channels": channels, "catalogs": catalogs,
            "rr_cursor": rr_cursor, "scores": scores}


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
                value = entry[field]
                if field == "quota" and not isinstance(value, dict):
                    value = {}
                setattr(state, field, value)
        if not isinstance(state.model_cooldown_until, dict):
            state.model_cooldown_until = {}
        state.model_cooldown_until = {
            str(wire): float(until)
            for wire, until in state.model_cooldown_until.items()
            if isinstance(until, (int, float)) and not isinstance(until, bool)
            and float(until) > now
        }
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
    if hasattr(store, "catalogs"):
        for name, entry in (data.get("catalogs") or {}).items():
            if not isinstance(entry, dict) or not isinstance(entry.get("models"), list):
                continue
            changes = entry.get("changes")
            store.catalogs[name] = {
                "fetched_at": float(entry.get("fetched_at") or 0),
                "endpoint": str(entry.get("endpoint") or ""),
                "models": [str(m) for m in entry["models"]],
                "changes": dict(changes) if isinstance(changes, dict) else {},
            }
    if hasattr(store, "rr_cursor"):
        restored_cursor = {}
        for key, offset in (data.get("rr_cursor") or {}).items():
            if isinstance(key, str) and isinstance(offset, int):
                restored_cursor[key] = offset
        store.rr_cursor = restored_cursor
    if hasattr(store, "scores"):
        # Drop scores of channels that no longer exist (deleted or renamed).
        restored_scores = {}
        for key, raw in (data.get("scores") or {}).items():
            if not isinstance(key, str) or key.split("|", 1)[0] not in store.states:
                continue
            if isinstance(raw, list) and len(raw) == 2:
                try:
                    restored_scores[key] = [float(raw[0]), float(raw[1])]
                except (TypeError, ValueError):
                    continue
            elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
                # Legacy snapshot kept a bare running total with no timestamp:
                # treat it as just-recorded so upgrading never wipes history.
                restored_scores[key] = [float(raw), float(now)]
        store.scores = restored_scores
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
            snap = capture(self._get_store())
            self._last = json.dumps(self._fingerprint(snap), sort_keys=True)
        except (OSError, ValueError, TypeError):
            return

    def flush(self) -> None:
        with self._lock:
            snapshot = capture(self._get_store())
            compare = json.dumps(self._fingerprint(snapshot), sort_keys=True)
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

    @staticmethod
    def _fingerprint(snapshot: dict) -> dict:
        return {"channels": snapshot.get("channels", {}),
                "catalogs": snapshot.get("catalogs", {}),
                "rr_cursor": snapshot.get("rr_cursor", {}),
                "scores": snapshot.get("scores", {})}
