"""Read-only bridge into the Facet facts table (same wheel, stdlib only).

Role split: the gateway *routes* on what its maintenance layer observes
(channel model lists); this module only answers *naming and selection*
questions from the bundled registry — resolve a well-known name to its
canonical key, list standing free-tier candidates, report coverage. Routing
must never depend on this table's freshness.

Any failure (facet data missing, import error) degrades to an empty
``available=False`` snapshot instead of breaking the server.
"""

from __future__ import annotations

import json

# Kept in sync with facet._find.FREE_TIERS (imported lazily; see below).
FREE_TIERS = ("free_forever", "free_daily")

_CACHED: "Facts | None" = None


class Facts:
    """Query façade over one registry table dict (empty = unavailable)."""

    def __init__(self, table: dict | None):
        self._table = table if isinstance(table, dict) else {}
        self._models = self._table.get("models") or {}

    @property
    def available(self) -> bool:
        return bool(self._models)

    def resolve(self, name: str) -> str | None:
        """Map a well-known name / alias to the canonical ``provider/model`` key."""
        name = (name or "").strip()
        if not name:
            return None
        if name in self._models:
            return name
        try:
            from facet._identity import resolve as _resolve
        except ImportError:
            return None
        return _resolve(self._table, name)

    def record(self, key: str) -> dict:
        rec = self._models.get(key)
        return dict(rec) if isinstance(rec, dict) else {}

    def free_candidates(self, limit: int = 20, tool_call: bool = False) -> list[dict]:
        """Standing free-tier models, tool-calling first then biggest context."""
        tiers = FREE_TIERS
        try:
            from facet._find import FREE_TIERS as _tiers
            tiers = _tiers
        except ImportError:
            pass
        rows = []
        for key, rec in self._models.items():
            if rec.get("quota_tier") not in tiers:
                continue
            if tool_call and rec.get("tool_call") is not True:
                continue
            rows.append({
                "id": key,
                "provider": rec.get("provider") or key.split("/", 1)[0],
                "quota_tier": rec.get("quota_tier"),
                "context_window": rec.get("context_window"),
                "max_output": rec.get("max_output"),
                "tool_call": rec.get("tool_call", False),
                "cost": rec.get("cost") or {},
            })
        rows.sort(key=lambda r: (-int(r["tool_call"]), -(r["context_window"] or 0), r["id"]))
        if limit is not None:
            rows = rows[: max(0, int(limit))]
        return rows

    def coverage(self) -> dict:
        providers = {k.split("/", 1)[0] for k in self._models}
        free = sum(1 for rec in self._models.values()
                   if rec.get("quota_tier") in FREE_TIERS)
        return {
            "models": len(self._models),
            "providers": len(providers),
            "free_models": free,
            "updated_at": self._table.get("updated_at"),
        }


def snapshot() -> Facts:
    """Load (once per process) the bundled registry snapshot as a Facts view."""
    global _CACHED
    if _CACHED is None:
        try:
            from facet._table import load as _load
            _CACHED = Facts(_load())
        except Exception:
            _CACHED = Facts({})
    return _CACHED
