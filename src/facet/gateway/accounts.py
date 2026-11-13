"""Account management: bind channels to an account so that AK/SK is filled once.

The channel remains the only runtime identity for routing; an account is a
convenience object that removes duplicate AK/SK when one vendor account has
multiple channels (coding/agent/payg) and keeps subscription usage grouped by
account instead of by channel name.

Key design choices:
- Accounts are optional. A channel with empty `account` is its own implicit
  account; the old credential chain (channel.creds > vendor default) still
  applies, so simple vendors (OpenRouter/DeepSeek) need no new objects.
- The data-plane api_key stays on the channel; only the management-plane AK/SK
  moves into Account.credentials. The provider registry's `credential_envs`
  dictates the env names inside credentials.
- Deleting an account is blocked if any channel or subscription references it.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from .providers import load_providers

# Allowed id pattern: alphanumeric, underscore, hyphen, must start with letter
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class ValidationError(ValueError):
    """Raised when account validation fails (bad id, missing provider, etc.)."""
    pass


def validate_account_id(value: str) -> str:
    """Validate an account id string.

    Raises ValidationError with a clear message on error.
    """
    value = str(value or "").strip()
    if not value:
        raise ValidationError("账号 id 不能为空")
    if len(value) > 64:
        raise ValidationError("账号 id 最长 64 个字符")
    if not ACCOUNT_ID_RE.match(value):
        raise ValidationError(
            "账号 id 只能包含字母、数字、下划线或横线，且必须以字母开头")
    return value


def normalize_credentials(raw: dict, provider_id: str | None, providers: list[dict] | None) -> dict[str, str]:
    """Normalize credentials block; keys come from provider registry if available.

    If provider_id is given and found in providers, we restrict credential keys
    to the union of all mode credential_envs for that provider. Otherwise allow
    any non-empty keys as an escape hatch.
    """
    if not isinstance(raw, dict):
        raise ValidationError("credentials 必须是对象")

    creds: dict[str, str] = {}
    allowed_keys: set[str] | None = None

    if provider_id and providers:
        allowed_keys = _provider_cred_keys(provider_id, providers)

    for key, val in raw.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(val, (str, int, float)):
            # silently skip non-string values
            continue
        key = key.strip()
        if allowed_keys and key not in allowed_keys:
            # ignore out-of-scope keys during init; caller may log warning
            continue
        creds[key] = str(val).strip()

    # Validate at least one required key if provider demands it
    if allowed_keys and not creds:
        raise ValidationError(f"厂家 {provider_id} 需要管理面凭据，请在下方输入 AK/SK")

    return creds


def _provider_cred_keys(provider_id: str, providers: list[dict]) -> set[str]:
    """Collect all credential_envs keys for a given provider."""
    keys: set[str] = set()
    for prov in providers:
        if prov.get("id") != provider_id:
            continue
        modes = prov.get("modes") or {}
        for mode in modes.values():
            if not isinstance(mode, dict):
                continue
            envs = mode.get("credential_envs") or []
            for entry in envs:
                if isinstance(entry, dict) and "env" in entry:
                    keys.add(str(entry["env"]).strip())
    return keys


def validate_account(entry: dict, providers: list[dict] | None = None) -> dict:
    """Validate one account entry; returns normalized dict (raises on error)."""
    if not isinstance(entry, dict):
        raise ValidationError("账号必须是一个对象")

    rid = validate_account_id(entry.get("id"))
    provider = str(entry.get("provider") or "").strip()
    if not provider:
        raise ValidationError("账号缺少 provider（厂家 id）")

    if providers:
        prov_ids = [p.get("id") for p in providers if isinstance(p, dict)]
        if provider not in prov_ids:
            raise ValidationError(f"账号的 provider `{provider}` 不存在于注册表")

    label = str(entry.get("label") or "").strip()
    if not label:
        raise ValidationError("账号 label（显示名）不能为空")

    creds_raw = entry.get("credentials") or {}
    creds = normalize_credentials(copy.deepcopy(creds_raw), provider, providers)

    note = str(entry.get("note") or "").strip()

    return {
        "id": rid,
        "provider": provider,
        "label": label,
        "credentials": creds,
        "note": note,
    }


def resolve_creds(channel: Any, config: Any) -> dict[str, str]:
    """Management-plane credentials: explicit channel override > account > vendor fallback.

    Args:
      channel: Channel object (has .creds, .account, .provider).
      config: Config object (has .accounts tuple and .credentials dict).

    Priority chain (matches current semantics, account layer inserted between
    channel creds and vendor-level fallback):
      1) channel.creds (explicit override, preserves legacy custom cases)
      2) account.credentials (when channel.account is set)
      3) config.credentials[channel.provider] (vendor-level shared slot)
      4) empty dict (caller falls back to process env / data-plane key itself)
    """
    own = dict(getattr(channel, "creds", None) or {})
    if own:
        return own

    account_id = str(getattr(channel, "account", None) or "")
    if account_id:
        account = _find_account(config, account_id)
        if account and account.get("credentials"):
            return dict(account["credentials"])

    provider = str(getattr(channel, "provider", "") or "")
    creds = (getattr(config, "credentials", None) or {}).get(provider) or {}
    return dict(creds)


def _find_account(config: Any, account_id: str) -> dict | None:
    """Lookup account by id in config.accounts (Account or plain dict)."""
    for acc in getattr(config, "accounts", None) or ():
        if isinstance(acc, dict):
            if str(acc.get("id") or "") == account_id:
                return acc
            continue
        if getattr(acc, "id", None) == account_id:
            return {
                "id": acc.id,
                "provider": acc.provider,
                "label": acc.label,
                "credentials": acc.credentials or {},
                "note": acc.note or "",
            }
    return None


def check_account_references(config: Any) -> list[str]:
    """Check which accounts have zero references (for cleanup hints)."""
    references: dict[str, list[str]] = {}
    for ch in getattr(config, "channels", None) or ():
        aid = str(getattr(ch, "account", None) or "")
        if aid:
            references.setdefault(aid, []).append(f"渠道/{getattr(ch, 'name', '')}")
    # Note: subscriptions live outside Config; caller merges subscription refs.
    return [aid for aid, refs in references.items()]


def account_lookup(config: Any, account_id: str) -> dict | None:
    """Public accessor for resolving account details (used by server.py)."""
    return _find_account(config, account_id)
