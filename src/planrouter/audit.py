"""Append-only routing audit trail.

Only routing metadata is stored: never prompts, keys, headers or model output.
The file is a JSONL log so a tail is always readable even if a write is cut off.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_FIELDS = (
    "ts", "api", "protocol", "model", "mode", "reason", "route", "channel",
    "wire_model", "attempt", "status", "http_status", "cooldown_seconds",
    "tokens", "ms", "stream", "sticky", "model_unverified",
)


class RequestLog:
    def __init__(self, path, max_lines: int = 5000, max_bytes: int = 2 * 1024 * 1024):
        self.path = Path(path)
        self.max_lines = max(10, int(max_lines))
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.lock = threading.Lock()
        self._writes = 0

    def record(self, **fields) -> None:
        entry = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        for key in ALLOWED_FIELDS:
            if key in fields and fields[key] is not None:
                entry[key] = fields[key]
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                return
            self._writes += 1
            if self._writes % 25 == 0:
                self._trim()

    def _trim(self) -> None:
        """Bound the log by both row count and file size."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            size = self.path.stat().st_size
        except OSError:
            return
        if size <= self.max_bytes and len(lines) <= self.max_lines:
            return
        keep = lines[-max(1, self.max_lines // 2):]
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def recent(self, limit: int = 50, channel: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit or 50), 500))
        with self.lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.is_file() else []
            except OSError:
                return []
        entries: list[dict] = []
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            if channel and item.get("channel") != channel:
                continue
            entries.append(item)
            if len(entries) >= limit:
                break
        return entries

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0
