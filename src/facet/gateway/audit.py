"""Append-only routing audit trail with bounded, size-based rotation.

Only routing metadata is stored: never prompts, keys, headers or model output.
The file is a JSONL log so a tail is always readable even if a write is cut off.
When the active file crosses `max_bytes` it is rotated into a numbered archive
(requests.jsonl.1, .2, ...) and the oldest archive beyond `keep_files` is
dropped: disk use stays bounded while recent history is preserved in archives
instead of being truncated away. Rotation state is just the archive file names,
so it survives a restart for free.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_FIELDS = (
    "ts", "api", "protocol", "model", "mode", "reason", "route", "channel",
    "wire_model", "attempt", "status", "http_status", "cooldown_seconds",
    "tokens", "ms", "stream", "sticky", "model_unverified", "vision",
)


class RequestLog:
    def __init__(self, path, max_bytes: int = 2 * 1024 * 1024, keep_files: int = 5):
        self.path = Path(path)
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.keep_files = max(1, int(keep_files))
        self.lock = threading.Lock()

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
                if self.path.stat().st_size > self.max_bytes:
                    self._rotate()
            except OSError:
                return

    def _slot(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _rotate(self) -> None:
        """Archive the active file as slot 1, shifting older slots back.

        Called with the lock held. The oldest slot beyond `keep_files` is
        dropped, so total disk use stays bounded by (keep_files + 1) files.
        """
        try:
            self._slot(self.keep_files).unlink(missing_ok=True)
        except OSError:
            pass
        for index in range(self.keep_files - 1, 0, -1):
            src = self._slot(index)
            if src.is_file():
                try:
                    src.replace(self._slot(index + 1))
                except OSError:
                    pass
        try:
            self.path.replace(self._slot(1))
        except OSError:
            pass

    def _files(self) -> list[Path]:
        """Active log plus archives, newest first."""
        return [self.path, *(self._slot(i) for i in range(1, self.keep_files + 1))]

    def recent(self, limit: int = 50, channel: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit or 50), 500))
        entries: list[dict] = []
        with self.lock:
            for path in self._files():
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
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
                        return entries
        return entries

    def size(self) -> int:
        """Bytes used by the active log plus all archives."""
        total = 0
        with self.lock:
            for path in self._files():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total
