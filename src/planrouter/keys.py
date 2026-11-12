"""Per-member API keys behind the shared gateway (attribution, not permissions).

A small team shares one gateway; each member gets a key so usage can be
counted per person. Keys are labels + extra credentials — they never gate
routes or models. Revocation flips `enabled` so historical stats keep the
name; the record itself is kept.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

KEY_PREFIX = "sk-pr-"


class KeyStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.records: list[dict] = self._read()

    def _read(self) -> list[dict]:
        if self.path.is_file():
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(document, dict) and isinstance(document.get("keys"), list):
                    return [r for r in document["keys"]
                            if isinstance(r, dict) and isinstance(r.get("key"), str)]
            except (OSError, ValueError):
                pass
        return []

    def _write(self) -> None:
        payload = json.dumps({"keys": self.records}, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    def create(self, name: str) -> dict:
        name = str(name or "").strip()[:60]
        if not name:
            raise ValueError("name is required")
        record = {
            "key": KEY_PREFIX + secrets.token_hex(16),
            "name": name,
            "created": time.time(),
            "enabled": True,
        }
        with self.lock:
            self.records.append(record)
            self._write()
        return dict(record)

    def revoke(self, key: str) -> bool:
        """Disable a key; True only when it flipped from enabled this call."""
        with self.lock:
            for record in self.records:
                if hmac.compare_digest(record["key"], str(key or "")):
                    if not record.get("enabled"):
                        return False
                    record["enabled"] = False
                    self._write()
                    return True
        return False

    def match(self, provided: str) -> str | None:
        """Member name when `provided` is an active key, else None."""
        if not provided or not provided.startswith(KEY_PREFIX):
            return None
        with self.lock:
            for record in self.records:
                if record.get("enabled") and hmac.compare_digest(record["key"], provided):
                    return record["name"]
        return None

    def list(self) -> list[dict]:
        with self.lock:
            rows = [dict(r) for r in self.records]
        rows.sort(key=lambda r: r.get("created") or 0)
        return rows
