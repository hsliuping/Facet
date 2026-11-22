"""Per-member API keys behind the shared gateway (attribution + light quotas).

A small team shares one gateway; each member gets a key so usage can be
counted per person and capped per person. Keys never gate routes or models —
they only carry optional rpm/daily/monthly caps. Revocation flips `enabled`
so historical stats keep the name; the record itself is kept.
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
# Optional per-key caps. 0 / absent means unlimited, so a record without a
# `limits` block keeps the pure-attribution behaviour.
LIMIT_FIELDS = ("rpm", "daily_requests", "daily_tokens",
                "monthly_requests", "monthly_tokens")


def normalize_limits(raw) -> dict[str, int]:
    """Validate a quota block; returns only the positive fields.

    Raises ValueError with a user-facing (Chinese) message naming the bad
    field, so the console can show it verbatim. Zero or absent = unlimited.
    """
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise ValueError('配额必须是一个对象，例如 {"rpm": 60}')
    limits: dict[str, int] = {}
    for field in LIMIT_FIELDS:
        value = raw.get(field)
        if value in (None, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"配额 {field} 必须是非负整数")
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError(f"配额 {field} 必须是非负整数")
            value = int(value)
        if value < 0:
            raise ValueError(f"配额 {field} 不能为负数")
        if value == 0:
            continue
        limits[field] = value
    return limits


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

    def create(self, name: str, limits=None) -> dict:
        name = str(name or "").strip()[:60]
        if not name:
            raise ValueError("名称不能为空")
        record = {
            "key": KEY_PREFIX + secrets.token_hex(16),
            "name": name,
            "created": time.time(),
            "enabled": True,
        }
        normalized = normalize_limits(limits)
        if normalized:
            record["limits"] = normalized
        with self.lock:
            self.records.append(record)
            self._write()
        return dict(record)

    def set_limits(self, key: str, limits) -> dict | None:
        """Replace one key's quota block (empty block clears it); None = no key."""
        normalized = normalize_limits(limits)
        with self.lock:
            for record in self.records:
                if hmac.compare_digest(record["key"], str(key or "")):
                    if normalized:
                        record["limits"] = normalized
                    else:
                        record.pop("limits", None)
                    self._write()
                    return dict(record)
        return None

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

    def enable(self, key: str) -> bool:
        """Re-enable a paused key; True only when it flipped this call."""
        with self.lock:
            for record in self.records:
                if hmac.compare_digest(record["key"], str(key or "")):
                    if record.get("enabled"):
                        return False
                    record["enabled"] = True
                    self._write()
                    return True
        return False

    def delete(self, key: str) -> bool:
        """Remove a key record permanently; True when one was removed."""
        with self.lock:
            for index, record in enumerate(self.records):
                if hmac.compare_digest(record["key"], str(key or "")):
                    del self.records[index]
                    self._write()
                    return True
        return False

    def rotate(self, key: str) -> dict | None:
        """Re-issue one member's secret in place, keeping name/limits/state.

        Returns the updated record (carrying the new key value) or None when
        the key is unknown. The old value stops working immediately — the
        record it matched against now holds the fresh secret.
        """
        with self.lock:
            for record in self.records:
                if hmac.compare_digest(record["key"], str(key or "")):
                    record["key"] = KEY_PREFIX + secrets.token_hex(16)
                    self._write()
                    return dict(record)
        return None

    def pause(self, key: str) -> bool:
        """Pause a key (set enabled=False); True only when it flipped this call."""
        with self.lock:
            for record in self.records:
                if hmac.compare_digest(record["key"], str(key or "")):
                    if not record.get("enabled"):
                        return False
                    record["enabled"] = False
                    self._write()
                    return True
        return False

    def lookup(self, provided: str) -> dict | None:
        """Full active record when `provided` is an enabled key, else None.

        The copy carries the name (for attribution) plus any quota block the
        gateway needs to enforce caps at request time.
        """
        if not provided or not provided.startswith(KEY_PREFIX):
            return None
        with self.lock:
            for record in self.records:
                if record.get("enabled") and hmac.compare_digest(record["key"], provided):
                    return dict(record)
        return None

    def match(self, provided: str) -> str | None:
        """Member name when `provided` is an active key, else None."""
        record = self.lookup(provided)
        return record["name"] if record else None

    def list(self) -> list[dict]:
        with self.lock:
            rows = [dict(r) for r in self.records]
        rows.sort(key=lambda r: r.get("created") or 0)
        return rows
