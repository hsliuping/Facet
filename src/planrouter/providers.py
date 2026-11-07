"""Built-in registry of mainstream LLM vendors and their billing modes.

The package ships ``providers.json`` with well-known vendors (volcengine,
zhipu, deepseek, ...). Each vendor lists only the modes it actually offers
(coding / agent / payg) with label, protocol, base_url, docs_url and tier.
A user overlay file ``<config_dir>/providers.json`` may override whole
vendor entries by id; the daily maintenance task updates the ``models``
field of modes that expose a ``docs_url`` / ``models_url`` and persists
them there.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .core import PROTOCOLS

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


def normalize_vendor(entry) -> dict:
    """Validate and normalize one vendor entry.

    Raises ValueError with a Chinese message when a required field is
    missing or malformed; otherwise returns a fresh normalized dict with
    the canonical key set (id / name / modes). Mode keys outside
    MODE_KEYS are rejected and every mode is reduced to label, protocol,
    base_url, tier, docs_url, models_url and a cleaned models list.
    """
    if not isinstance(entry, dict):
        raise ValueError("厂家条目必须是 JSON 对象")
    raw_id = entry.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ValueError("厂家 id 必填")
    vendor_id = raw_id.strip()
    if not re.fullmatch(r"[a-z0-9-]+", vendor_id):
        raise ValueError("厂家 id 只能包含小写字母、数字和连字符")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("厂家名称必填")
    modes_raw = entry.get("modes")
    if not isinstance(modes_raw, dict) or not modes_raw:
        raise ValueError("厂家至少需要配置一个模式")
    modes: dict[str, dict] = {}
    for key, mode in modes_raw.items():
        if key not in MODE_KEYS:
            raise ValueError(f"不支持的模式 {key}，只能使用 {'/'.join(MODE_KEYS)}")
        if not isinstance(mode, dict):
            raise ValueError(f"模式 {key} 必须是 JSON 对象")
        label = mode.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"模式 {key} 的显示名必填")
        protocol = mode.get("protocol")
        if protocol not in PROTOCOLS:
            raise ValueError(f"模式 {key} 的协议必须是 {'/'.join(PROTOCOLS)}")
        base_url = mode.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError(f"模式 {key} 的 base_url 必填")
        base_url = base_url.strip()
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"模式 {key} 的 base_url 必须以 http(s):// 开头")
        tier = mode.get("tier") or ""
        if tier not in ("", "free", "paid"):
            raise ValueError(f"模式 {key} 的档位只能是 空、free 或 paid")
        docs_url = _optional_url(mode, "docs_url", key)
        models_url = _optional_url(mode, "models_url", key)
        models_raw = mode.get("models")
        if models_raw is None:
            models_raw = []
        if not isinstance(models_raw, list):
            raise ValueError(f"模式 {key} 的模型清单必须是字符串数组")
        models = [str(item).strip() for item in models_raw if str(item).strip()]
        modes[key] = {
            "label": label.strip(),
            "protocol": protocol,
            "base_url": base_url,
            "tier": tier,
            "docs_url": docs_url,
            "models_url": models_url,
            "models": models,
        }
    return {"id": vendor_id, "name": name.strip(), "modes": modes}


def _optional_url(mode: dict, key: str, mode_key: str) -> str:
    """Read an optional http(s) URL field, raising ValueError otherwise."""
    value = mode.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"模式 {mode_key} 的 {key} 必须是字符串")
    value = value.strip()
    if value and not value.startswith(("http://", "https://")):
        raise ValueError(f"模式 {mode_key} 的 {key} 必须以 http(s):// 开头")
    return value


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


def overlay_ids(config_dir) -> set[str]:
    """Ids of vendor entries currently present in the user overlay file."""
    if not config_dir:
        return set()
    target = Path(config_dir) / OVERLAY_NAME
    if not target.is_file():
        return set()
    try:
        entries = _read_entries(target)
    except ValueError:
        return set()
    return {str(e["id"]) for e in entries
            if isinstance(e, dict) and e.get("id")}


def delete_overlay_entry(config_dir, provider_id: str) -> bool:
    """Remove one vendor entry from the overlay file by id.

    Returns True when the entry existed and was removed; False when the
    overlay file is missing or does not contain the id. The write back
    follows the save_overlay style: .bak backup plus atomic replace.
    """
    target = Path(config_dir) / OVERLAY_NAME
    if not target.is_file():
        return False
    try:
        entries = _read_entries(target)
    except ValueError as exc:
        raise ValueError(f"覆盖层文件损坏：{exc}") from exc
    kept = []
    found = False
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("id") or "") == provider_id:
            found = True
            continue
        kept.append(entry)
    if not found:
        return False
    target.with_name(target.name + ".bak").write_bytes(target.read_bytes())
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(
        json.dumps({"providers": kept}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return True
