"""Name identity: normalization, lookup build, resolve.

Same logic as examples/pick_model.py (copied, not imported — tools/ scripts
stay independently runnable).
"""

from __future__ import annotations

import re

# Same normalization as tools/sync_models_dev.py: case, dots and hyphens are
# presentation, they never distinguish capabilities ('glm-5.3' == 'glm5.3').
_PREFIX_RE = re.compile(r"^[a-z0-9_~-]+/")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def identity(name: str) -> str:
    """'OpenRouter/Anthropic/Claude-Sonnet-4.5' -> 'claudesonnet45'."""
    return _NON_ALNUM_RE.sub("", _PREFIX_RE.sub("", name.strip().lower()))


def build_lookup(table: dict) -> dict[str, str]:
    """normalized name -> table key. Canonical keys first, then aliases."""
    lookup: dict[str, str] = {}
    for key, rec in table["models"].items():
        bare = key.split("/", 1)[1]
        lookup.setdefault(identity(bare), key)
        for alias in rec.get("aliases", []):
            lookup.setdefault(identity(alias), key)
    return lookup


def resolve(table: dict, name: str) -> str | None:
    """Resolve a user-supplied name or alias to its table key (None if unknown)."""
    return build_lookup(table).get(identity(name))
