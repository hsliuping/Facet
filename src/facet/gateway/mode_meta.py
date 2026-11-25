"""Unified classification of vendor free modes and access modes.

Single source of truth for two questions the whole system keeps asking:

* **free mode** — which of the four business free policies (one-time grant /
  daily reset / forever-free models / cash voucher, the taxonomy of
  ``docs/Facet免费额度全景.pptx``) does a registry mode carry?
* **access mode** — which credentials does it need (key vs AK/SK), does it
  require a daily check-in, and can its free quota be queried at all?

The vendor capability registries (usage fetchers, wallet fetchers, check-in
handlers) live here too, next to the enum that classifies them: adding a
vendor means adding one row here, not three scattered edits in server.py.
The console API decorates every mode with the normalized ``meta`` object,
so the frontend consumes classifications instead of re-deriving them.

Inference: explicit registry fields (``free_kind`` / ``cred_kind`` /
``requires_checkin`` / ``quota_capability``) win; while a mode lacks them
the helpers fall back to the legacy signals (``free_quota`` /
``free_models`` / ``free_limits`` / ``credential_envs`` / the fetcher
tables). The builtin registry declares ``free_kind`` on every mode
(step 2), so inference now only matters for user-defined vendor entries.
"""
from __future__ import annotations

from . import (ark_quota, bailian_quota, deepseek_quota, hunyuan_quota,
               minimax_quota, modelscope_quota, moonshot_quota,
               openrouter_quota, zhipu_quota)
from .probe import is_free_model

# ── Enums ────────────────────────────────────────────────────────────────
# free_kind: the PPT taxonomy. "none" means the mode carries no free policy.
FREE_KINDS = ("one_time", "daily", "forever", "voucher", "none")
# cred_kind: what the operator must paste for quota alignment / wallet reads.
CRED_KINDS = ("key", "aksk")
# quota_capability: how the gateway can observe the account's free budget.
QUOTA_CAPS = ("none", "key_api", "mgmt_api")

# ── Vendor capability registries (moved from server.py) ─────────────────
# Pay-as-you-go vendors report account usage to the same key the channel
# already sends upstream. provider -> fetcher(key) -> snapshot; the channel
# config page renders the result. Vendors without an endpoint stay out, so a
# missing entry simply means "no usage display".
KEY_API_FETCHERS = {
    "openrouter": openrouter_quota.fetch_key_usage,
    "deepseek": deepseek_quota.fetch_balance,
    "zhipu": zhipu_quota.fetch_account_report,
    "minimax": minimax_quota.fetch_usage,
    "moonshot": moonshot_quota.fetch_balance,
    "modelscope": modelscope_quota.fetch_balance,
}
# Management-plane wallets: these authenticate with the account's AK/SK pair
# from the credential chain (resolve_creds), not with the data-plane key the
# channel sends upstream — provider -> (fetcher(ak, sk), ak env, sk env).
MGMT_WALLET_FETCHERS = {
    "bailian": (bailian_quota.fetch_balance,
                bailian_quota.AK_ENV, bailian_quota.SK_ENV),
    "hunyuan": (hunyuan_quota.fetch_balance,
                hunyuan_quota.ID_ENV, hunyuan_quota.KEY_ENV),
}
# Daily check-in executors: provider -> checkin(api_key). The scheduled
# runner sweeps every channel whose mode declares requires_checkin and
# dispatches through this table, so a second check-in vendor is a new row,
# not a new Runner class.
CHECKIN_HANDLERS = {
    "modelscope": modelscope_quota.checkin,
}


def _quota_dict(mode: dict) -> dict:
    quota = mode.get("free_quota")
    return quota if isinstance(quota, dict) and quota else {}


def free_kind_of(provider_id: str, mode: dict) -> str:
    """Primary free policy of one registry mode (PPT taxonomy).

    Priority: explicit ``free_kind`` → spend-down ``free_quota`` (a day
    window is the daily kind; total/month grants are one_time) →
    ``free_models`` (forever) → ``free_limits`` (one_time, the conservative
    default for note-only vendors) → registry ``tier: free`` (forever: a
    wholly-free tier) → none. ``voucher`` is never inferred — mislabeling a
    wallet read as a cash voucher would be worse than admitting "unknown".
    """
    mode = mode or {}
    explicit = str(mode.get("free_kind") or "").strip().lower()
    if explicit in FREE_KINDS:
        return explicit
    quota = _quota_dict(mode)
    if quota:
        period = str(quota.get("period") or "total").strip().lower()
        return "daily" if period == "day" else "one_time"
    if mode.get("free_models"):
        return "forever"
    if mode.get("free_limits"):
        return "one_time"
    if str(mode.get("tier") or "") == "free":
        return "forever"
    return "none"


def free_kinds_of(provider_id: str, mode: dict) -> list[str]:
    """Every free policy the mode carries, primary first.

    A mode can stack policies (hunyuan payg: a one-time grant plus 44
    forever-free models; OpenRouter payg: forever-free ``:free`` models
    gated by a documented 50-requests/day account cap). Documented daily
    caps — ``free_limits.rpd`` / ``tpd`` — contribute the daily kind as a
    secondary policy. The list feeds the free-models page grouping;
    ``["none"]`` means no free policy at all.
    """
    mode = mode or {}
    primary = free_kind_of(provider_id, mode)
    kinds: list[str] = [] if primary == "none" else [primary]
    if mode.get("free_models") and "forever" not in kinds:
        kinds.append("forever")
    quota = _quota_dict(mode)
    if quota:
        period = str(quota.get("period") or "total").strip().lower()
        kind = "daily" if period == "day" else "one_time"
        if kind not in kinds:
            kinds.append(kind)
    limits = mode.get("free_limits")
    if isinstance(limits, dict) and (limits.get("rpd") or limits.get("tpd")) \
            and "daily" not in kinds:
        kinds.append("daily")
    return kinds or ["none"]


def cred_kind_of(mode: dict) -> str:
    """Credential shape the mode needs: ``key`` (data-plane only) or
    ``aksk`` (management-plane pair for quota alignment / wallet reads)."""
    mode = mode or {}
    explicit = str(mode.get("cred_kind") or "").strip().lower()
    if explicit in CRED_KINDS:
        return explicit
    envs = mode.get("credential_envs")
    return "aksk" if isinstance(envs, list) and envs else "key"


def requires_checkin_of(provider_id: str, mode: dict) -> bool:
    """Does claiming the daily free grant need a vendor check-in?

    Declared explicitly per mode (``requires_checkin``). ModelScope's
    magicube lands with the day's first website sign-in, so its mode sets
    true and the scheduled claim sweep dispatches through CHECKIN_HANDLERS;
    a mode without the field simply does not need one.
    """
    mode = mode or {}
    declared = mode.get("requires_checkin")
    if declared is not None:
        return bool(declared)
    return False


def quota_capability_of(provider_id: str, mode: dict) -> str:
    """How the account's free budget can be observed.

    ``mgmt_api`` — a management-plane wallet or free-quota CLI (AK/SK
    signed; volcengine reaches the same plane through its dedicated ark
    flow, so its ``free_quota_cmd`` counts here). ``key_api`` — the vendor
    exposes a usage endpoint on the data-plane key. ``none`` — no API, the
    console can only show the registry's documented numbers.
    """
    mode = mode or {}
    pid = str(provider_id or "")
    explicit = str(mode.get("quota_capability") or "").strip().lower()
    if explicit in QUOTA_CAPS:
        return explicit
    if pid in MGMT_WALLET_FETCHERS:
        return "mgmt_api"
    cmd = mode.get("free_quota_cmd")
    if isinstance(cmd, list) and cmd:
        return "mgmt_api"
    if pid in KEY_API_FETCHERS:
        return "key_api"
    return "none"


def mode_meta(provider_id: str, mode: dict) -> dict:
    """Normalized classification block for one registry mode.

    Injected into the ``/api/providers`` response; the wizard and the
    channel editor read these instead of re-deriving from raw fields.
    """
    return {
        "free_kind": free_kind_of(provider_id, mode),
        "free_kinds": free_kinds_of(provider_id, mode),
        "cred_kind": cred_kind_of(mode),
        "requires_checkin": requires_checkin_of(provider_id, mode),
        "quota_capability": quota_capability_of(provider_id, mode),
    }


# ── Catalog-level free pool (mixed vs wholly-free) ──────────────────────

def catalog_is_mixed(models) -> bool:
    """Does the channel catalog mix :free-suffixed ids with paid ones?

    OpenRouter-style catalogs mark their free subset with the ``:free``
    suffix; a catalog without any such id belongs to a wholly-free vendor
    (NIM, doubao), where every model is free. This is the single
    implementation point for that rule — the free-candidate pools and the
    mapping-value filters all ask it instead of re-deriving.
    """
    return any(is_free_model(m) for m in models or [])


def catalog_free_pool(models) -> list:
    """Free candidate ids from one channel catalog: the :free subset when
    the catalog is mixed, otherwise the whole (wholly-free) catalog."""
    models = [m for m in (models or [])]
    if catalog_is_mixed(models):
        return [m for m in models if is_free_model(m)]
    return models
