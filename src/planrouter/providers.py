"""Built-in registry of mainstream LLM vendors and their billing modes.

The package ships ``providers.json`` with well-known vendors (volcengine,
zhipu, deepseek, ...). Each vendor lists only the modes it actually offers
(coding / agent / payg) with label, protocol, base_url, docs_url and tier.
A user overlay file ``<config_dir>/providers.json`` may override whole
vendor entries by id; the daily maintenance task updates the ``models``
field of modes that expose a ``docs_url`` and persists them there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Package copy of the registry; the config dir may hold a user overlay.
BUILTIN_PATH = Path(__file__).parent / "providers.json"

# Name of the user overlay file, resolved inside the config directory.
OVERLAY_NAME = "providers.json"

# Every vendor entry must only use these mode keys.
MODE_KEYS = ("coding", "agent", "payg")


def _read_entries(path: Path) -> list:
    """Read a providers JSON file and return the providers list."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
        raise ValueError(f"{path}: 需要包含 providers 数组的 JSON 对象")
    return data["providers"]


def _merge(builtin: list, overlay: list) -> list:
    """Overlay whole vendor entries by id, keeping builtin order first."""
    merged: dict[str, dict] = {}
    for entry in builtin:
        if isinstance(entry, dict) and entry.get("id"):
            merged[str(entry["id"])] = entry
    for entry in overlay:
        if isinstance(entry, dict) and entry.get("id"):
            merged[str(entry["id"])] = entry
    return list(merged.values())


def load_providers(config_dir=None) -> list[dict]:
    """Load the builtin registry merged with the user overlay file.

    The overlay file lives next to config.json and is optional; a missing
    file simply yields the builtin list while a broken one raises
    ValueError (JSONDecodeError is a ValueError subclass).
    """
    builtin = _read_entries(BUILTIN_PATH)
    if not config_dir:
        return [dict(entry) if isinstance(entry, dict) else entry for entry in builtin]
    overlay_path = Path(config_dir) / OVERLAY_NAME
    if not overlay_path.is_file():
        return [dict(entry) if isinstance(entry, dict) else entry for entry in builtin]
    return _merge(builtin, _read_entries(overlay_path))


def save_overlay(config_dir, providers: list) -> Path:
    """Persist agent-updated vendor entries into the user overlay file.

    Existing overlay entries are merged (whole entries replaced by id) so
    unrelated vendors keep their overlay state. Before writing, the current
    file is backed up as ``<file>.bak`` following the config backup style.
    Writes go through a temp file plus os.replace for atomicity.
    """
    target = Path(config_dir) / OVERLAY_NAME
    current: dict[str, dict] = {}
    if target.is_file():
        try:
            for entry in _read_entries(target):
                if isinstance(entry, dict) and entry.get("id"):
                    current[str(entry["id"])] = entry
        except ValueError:
            # A broken overlay is replaced, not trusted.
            current = {}
    for entry in providers or []:
        if isinstance(entry, dict) and entry.get("id"):
            current[str(entry["id"])] = entry
    payload = {"providers": list(current.values())}
    if target.is_file():
        target.with_name(target.name + ".bak").write_bytes(target.read_bytes())
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return target
