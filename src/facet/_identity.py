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
    """normalized name -> table key. Canonical keys first, then aliases.

    Shallow keys ("provider/model") are indexed before nested ones
    ("aggregator/vendor/model"), so 'hy3' resolves to 'tencent/hy3' rather
    than the edenai copy 'edenai/tencent/hy3'.
    """
    lookup: dict[str, str] = {}
    models = table["models"]

    def index(key: str, rec: dict) -> None:
        # Some keys (e.g. embedding models) carry no "provider/" prefix;
        # [-1] keeps those resolving instead of raising IndexError.
        bare = key.split("/", 1)[-1]
        lookup.setdefault(identity(bare), key)
        for alias in rec.get("aliases", []):
            lookup.setdefault(identity(alias), key)

    # Index shallow keys ("provider/model") first so they win identity groups
    for key, rec in models.items():
        if key.count("/") <= 1:
            index(key, rec)
    # Index nested keys ("vendor/provider/model" etc.) afterward
    for key, rec in models.items():
        if key.count("/") > 1:
            index(key, rec)
    return lookup


def resolve(table: dict, name: str) -> str | None:
    """Resolve a user-supplied name or alias to its table key (None if unknown)."""
    return build_lookup(table).get(identity(name))
