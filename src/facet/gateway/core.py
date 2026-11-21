"""Channel configuration, quota/cooldown detection and channel health.

Pure standard library. A *channel* is one upstream endpoint (a coding plan, a
subscription bridge, or a free-tier API key). A route speaks the client's wire
protocol (picked by the endpoint path) and only dispatches channels speaking
that same protocol: a cross-protocol channel listed in a route is skipped at
pick time (Router.pick), never converted on the wire.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener, urlopen

from .quota import quota_view

OPENAI = "openai"
ANTHROPIC = "anthropic"
PROTOCOLS = (OPENAI, ANTHROPIC)
LOOPBACK = ("127.0.0.1", "localhost", "::1")

# Route channel-list macro: "$free" expands (at load time) to every enabled
# tier="free" channel speaking the route's protocol, in (priority, name)
# order. Channels already listed explicitly keep their position and are not
# duplicated. New free channels therefore join routes that carry the macro
# without any further configuration; paid channels are always hand-placed.
FREE_CHANNEL_MACRO = "$free"

# Safety margin kept in front of the free gate, in tokens, keyed by provider.
# Volcengine keeps serving after a model's free grant runs dry whenever the
# account holds any balance — no 429, no error code, no header — so the only
# defence is to stop that model while the vendor-observed remaining still
# looks healthy. The margin is applied per model (each wire has its own
# grant), never to the channel as a whole: one drained model must not take
# its siblings down with it. One routed request can spend ~90k tokens, so
# 100k left means an in-flight request can never cross zero and bill the
# balance. Providers absent from the mapping keep a smaller default margin:
# below ~10k tokens a call is likely to fail outright anyway, and the failed
# call would only trigger another retry — stopping the model first is cheaper
# for everyone.
FREE_RESERVE_TOKENS = {"volcengine": 100_000}
FREE_RESERVE_DEFAULT_TOKENS = 10_000

# Route order stays as the operator arranged it; the stability score only
# demotes a channel×model that keeps failing to the back of its route, so a
# known-bad vendor does not add a failed hop to every request. The score
# decays toward zero, which restores the pair to its chain position.
DEMOTE_SCORE = -3.0

# Per-call routing trace. FACET_ROUTE_DEBUG=1 adds one [route:debug] block per
# pick listing every channel of the route with its priority, wire model and
# verdict — the "why was channel X never chosen" question is answered from the
# log instead of from guesswork. Off by default (it is one line per channel
# per request).
ROUTE_DEBUG_ENV = "FACET_ROUTE_DEBUG"


def route_debug_enabled() -> bool:
    return bool(os.environ.get(ROUTE_DEBUG_ENV, "").strip())


def _cooldown_left(until: float, now: float) -> str:
    """Remaining cooldown as a short human string (90s / 4.9h)."""
    secs = int(max(0.0, until - now))
    return f"{secs}s" if secs < 3600 else f"{secs / 3600:.1f}h"

# Providers such as Volcengine Ark expose https://host/api/v3 (or
# /api/coding/v3) as the OpenAI-compatible base. Appending the usual
# /v1/chat/completions would produce /v3/v1/chat/completions and a 404.
VERSIONED_BASE = re.compile(r"/v\d+(?:beta)?$", re.IGNORECASE)


def upstream_path_for(base_url: str, upstream_path: str | None,
                      protocol: str, endpoint: str = "chat") -> str:
    """Pick the upstream path without duplicating a version segment.

    `endpoint` selects the API path: "chat" -> /v1/chat/completions (or /messages),
    "embeddings" -> /v1/embeddings (OpenAI only), "rerank" -> /v1/rerank (OpenAI only),
    "speech" -> /v1/audio/speech (OpenAI only),
    "transcriptions" -> /v1/audio/transcriptions (OpenAI only),
    "translations" -> /v1/audio/translations (OpenAI only).
    """
    if upstream_path:
        return upstream_path
    if endpoint == "embeddings":
        suffix = "/embeddings"
    elif endpoint == "rerank":
        suffix = "/rerank"
    elif endpoint == "speech":
        suffix = "/audio/speech"
    elif endpoint == "transcriptions":
        suffix = "/audio/transcriptions"
    elif endpoint == "translations":
        suffix = "/audio/translations"
    else:
        suffix = "/chat/completions" if protocol == OPENAI else "/messages"
    if VERSIONED_BASE.search(base_url.rstrip("/")):
        return suffix
    return "/v1" + suffix


def open_with_proxy(req, timeout, proxy: str = ""):
    """urlopen honoring an optional http(s):// proxy URL.

    Foreign upstreams (OpenAI, Anthropic, xAI, ...) are usually unreachable
    from CN networks directly, so a channel can point at a local relay
    (e.g. http://127.0.0.1:7890) via its `proxy` field. Empty proxy keeps
    the plain urlopen behavior (which still honors environment proxies).
    """
    if not proxy:
        return urlopen(req, timeout=timeout)
    opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
    return opener.open(req, timeout=timeout)


# Vendors gate on client identity headers. OpenCode Go's Cloudflare rules
# reject generic library user agents (Python-urllib/* -> HTTP 403 "error
# code: 1010"), and since 2026-09-05 it also requires a stable
# x-opencode-session header per conversation for routing and prompt caching.
# The gateway is the HTTP client of record, so it injects both on every
# forwarded request; clients like TRAE/dsh need no changes of their own.
GATEWAY_USER_AGENT = "facet-gateway/1.0"
OPENCODE_HOST = "opencode.ai"
SAFE_SESSION = re.compile(r"^[A-Za-z0-9._:/@+ -]{1,120}$")


def is_opencode_url(url: str) -> bool:
    """True when a request targets the OpenCode Go inference service."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return host == OPENCODE_HOST or host.endswith("." + OPENCODE_HOST)


def first_user_text(body) -> str:
    """Text of the first user message, across OpenAI/Anthropic request shapes.

    Chat APIs are stateless — every turn of one conversation repeats its
    first user message — so hashing this text yields the same session id
    for every request of that conversation.
    """
    if not isinstance(body, dict):
        return ""
    for message in body.get("messages") or []:
        if not isinstance(message, dict) or str(message.get("role")) != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [part.get("text") for part in content
                     if isinstance(part, dict) and isinstance(part.get("text"), str)]
            return "\n".join(texts)
        return ""
    return ""


def opencode_session_id(body, api_key: str = "",
                        client_session: str = "") -> str:
    """Stable x-opencode-session value for one conversation.

    A native session header from the client wins when it sends one; the
    fallback derives from the conversation's first user message (keeps
    prompt caches warm across turns), then from the channel key
    (embeddings-style bodies have no messages).
    """
    if client_session and SAFE_SESSION.fullmatch(client_session):
        return client_session
    seed = first_user_text(body)
    if not seed:
        seed = "key:" + str(api_key or "")
    if not seed.strip():
        return "facet-session"
    return "facet-" + hashlib.blake2b(
        seed.encode("utf-8", "replace"), digest_size=16).hexdigest()


def apply_upstream_headers(headers: dict, url: str, body=None,
                           api_key: str = "", client_session: str = "") -> dict:
    """Decorate upstream headers with the gateway identity, in place.

    Every forwarded request carries a proper User-Agent instead of the
    urllib default; OpenCode Go targets additionally get the
    per-conversation x-opencode-session header.
    """
    headers.setdefault("User-Agent", GATEWAY_USER_AGENT)
    if is_opencode_url(url) and "x-opencode-session" not in headers:
        headers["x-opencode-session"] = opencode_session_id(
            body, api_key, client_session)
    return headers

# Tools usually demand a concrete model id. These branded ids are ours: putting
# one into a tool's model field means "let the router schedule internally".
AUTO_MODEL = "auto"
AGENT_MODEL = "facet-agent"
EMBEDDING_MODEL = "facet-embedding"
DEFAULT_BRANDED_MODELS = {
    "facet-auto": "default",
    "facet-cheap": "cheap",
    "facet-free": "free",
    AGENT_MODEL: "default",
    EMBEDDING_MODEL: "embedding",
}
INTERNAL_MODEL_PREFIXES = ("auto:", "facet:")

# Substrings that mean "this channel is out of quota", not "the request is bad".
QUOTA_MARKERS = (
    "quota", "rate_limit", "rate limit", "usage limit", "usage_limit",
    "insufficient_quota", "too many requests", "credit balance", "billing",
    "overloaded", "capacity", "exceeded", "额度", "已用完", "限流", "冷却",
    "余额不足", "超出",
)

# Daily-quota exhaustion phrasings in 429 bodies that carry no Retry-After or
# *-reset header (OpenRouter "free-models-per-day", ModelScope daily caps, ...).
DAILY_QUOTA_MARKERS = (
    "per-day", "per day", "daily", "day limit", "requests per day",
    "每日", "当天",
)


def _seconds_until_utc_midnight(now: float) -> float:
    """Seconds until the next UTC 00:00 (when daily quotas usually reset)."""
    return 86400.0 - (now % 86400.0)


def resolve_secret(value: str | None) -> str | None:
    """Resolve `env:VAR` references; a missing variable resolves to None."""
    if not value:
        return None
    if value.startswith("env:"):
        return os.environ.get(value[4:]) or None
    return value


def _header(headers, *names: str) -> str | None:
    wanted = {n.lower() for n in names}
    try:
        for key, value in headers.items():
            if key.lower() in wanted:
                return str(value)
    except AttributeError:
        return None
    return None


def _duration(value: str | None, now: float) -> float | None:
    """Parse a Retry-After / *-reset value into seconds from `now`.

    Handles: seconds, epoch seconds, ISO-8601, HTTP-date, and "1h30m"/"6m0s".
    """
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", v):
        n = float(v)
        return max(0.0, n - now) if n > 1_000_000_000 else n
    if "T" in v and re.match(r"^\d{4}-\d{2}-\d{2}T", v):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
            return max(0.0, dt.timestamp() - now)
        except ValueError:
            return None
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", v, re.I)
    if m and any(m.groups()):
        h, minutes, s = (int(x or 0) for x in m.groups())
        return float(h * 3600 + minutes * 60 + s)
    try:
        dt = parsedate_to_datetime(v)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
            return max(0.0, dt.timestamp() - now)
    except (TypeError, ValueError):
        pass
    return None


def parse_cooldown(
    status: int,
    headers,
    body: bytes | str,
    default_minutes: int,
    max_hours: int,
    now: float | None = None,
    not_found_hours: int = 0,
    daily_hours: int = 0,
) -> tuple[bool, float, str]:
    """Return (is_quota_exhaustion, cooldown_seconds, reason).

    Quota exhaustion cools a channel for hours. Transient upstream errors only
    cool it briefly and allow a half-open probe on the next request.

    `not_found_hours` > 0 turns a 404 (removed model) into a long, probe-free
    cooldown. `daily_hours` handles daily-quota 429s without a reset header:
    0 cools until the next UTC midnight, >0 a fixed number of hours, -1
    disables the special case. Explicit Retry-After / *-reset headers win.
    """
    now = time.time() if now is None else now
    text = body.decode("utf-8", "replace")[:4000] if isinstance(body, bytes) else str(body)[:4000]
    low = text.lower()
    marker = next((m for m in QUOTA_MARKERS if m in low), "")

    if status < 400:
        return False, 0.0, ""
    if status == 404 and not_found_hours > 0:
        return True, min(not_found_hours * 3600.0, max_hours * 3600.0), "http_404_not_found"
    quota_like = status in (429, 529) or (
        bool(marker) and status in (400, 403, 409, 422, 429, 500, 502, 503, 504, 529)
    )
    if not quota_like:
        return False, 30.0, f"transient_http_{status}"

    resets: list[float] = []
    retry_after = _header(headers, "retry-after")
    if retry_after:
        seconds = _duration(retry_after, now)
        if seconds is not None:
            resets.append(seconds)
    try:
        for key, value in headers.items():
            if "reset" in key.lower() and key.lower() != "retry-after":
                seconds = _duration(str(value), now)
                if seconds is not None:
                    resets.append(seconds)
    except AttributeError:
        pass

    if status == 429 and not resets and daily_hours != -1 and any(
            m in low for m in DAILY_QUOTA_MARKERS):
        seconds = (daily_hours * 3600.0 if daily_hours > 0
                   else _seconds_until_utc_midnight(now))
        return True, min(max(seconds, 1.0), max_hours * 3600.0), "daily_quota_reset"

    seconds = max(resets) if resets else default_minutes * 60.0
    seconds = min(max(seconds, 1.0), max_hours * 3600.0)
    return True, seconds, marker or f"http_{status}"


@dataclass(frozen=True)
class Rate:
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None


@dataclass(frozen=True)
class FreeQuota:
    """Vendor free-tier allowance applied per upstream model.

    `period` decides the accounting window for the local usage counter:
    "total" (grant lifetime), "month" or "day". Both caps are optional; the
    router stops routing the model as soon as either one is used up, so a
    free channel can never silently spill into per-call billing.

    `expires` grants the model one-time usage that is valid through the given
    date (inclusive). After that date the model is treated as exhausted. A bare
    ``YYYY-MM-DD`` means the pack is valid through that day's last second
    (23:59:59).
    """
    tokens: int | None = None
    requests: int | None = None
    period: str = "total"
    expires: datetime | None = None


def parse_expires(value) -> datetime | None:
    """Parse a one-time pack expiry: ``YYYY-MM-DD`` or an ISO datetime.

    A bare date is inclusive: the pack stays usable through that day, so the
    stored value is that day's 23:59:59 rather than its midnight. A timezone-
    aware datetime is converted to local time and stored naive, matching the
    local-clock comparison the quota gate does.
    """
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(
            "free_quota.expires 必须是日期（YYYY-MM-DD）或 datetime")
    text = value.strip()
    if len(text) == 10:
        try:
            day = date.fromisoformat(text)
        except ValueError:
            raise ValueError(
                "free_quota.expires 必须是日期（YYYY-MM-DD）或 datetime"
            ) from None
        return datetime(day.year, day.month, day.day, 23, 59, 59)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            "free_quota.expires 必须是日期（YYYY-MM-DD）或 datetime"
        ) from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def parse_free_quota(raw) -> FreeQuota | None:
    if raw in (None, ""):
        return None
    if not isinstance(raw, dict):
        raise ValueError("free_quota 必须是对象")
    tokens = raw.get("tokens")
    requests = raw.get("requests")
    for label, value in (("tokens", tokens), ("requests", requests)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"free_quota.{label} 必须是非负整数")
    period = str(raw.get("period") or "total").strip().lower()
    if period not in ("total", "month", "day"):
        raise ValueError('free_quota.period 必须是 "total"、"month" 或 "day"')
    if tokens is None and requests is None:
        raise ValueError("free_quota 至少要填 tokens 或 requests 之一")
    expires = parse_expires(raw.get("expires"))
    return FreeQuota(tokens=tokens, requests=requests, period=period,
                     expires=expires)


def parse_model_quota(name: str, raw) -> dict[str, FreeQuota] | None:
    """Parse a channel's per-model one-time packs (中国电信 / SCNet style).

    Keys are model names — the exposed name or the upstream wire id, resolved
    the same way as `tiers`. Each value is a free_quota object, so every model
    carries its own amount and expiry; a ``null`` value removes a pack.
    """
    if raw in (None, ""):
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"channel {name}：model_quota 必须是对象")
    packs: dict[str, FreeQuota] = {}
    for model_name, spec in raw.items():
        key = str(model_name).strip()
        if not key:
            raise ValueError(
                f"channel {name}：model_quota 的键必须是模型名")
        try:
            quota = parse_free_quota(spec)
        except ValueError as exc:
            raise ValueError(
                f"channel {name}：model_quota.{key}：{exc}") from None
        if quota is not None:
            packs[key] = quota
    return packs or None


@dataclass(frozen=True)
class Channel:
    name: str
    protocol: str
    base_url: str
    api_key: str | None
    models: dict[str, str]
    priority: int
    cooldown_minutes: int
    rate: Rate
    timeout: int
    enabled: bool = True
    upstream_path: str | None = None
    tier: str = ""  # "", "free" or "paid"; empty infers from linked subscriptions
    # Per-model tier overrides ("free"/"paid" by exposed model name) — a plan
    # like Bailian mixes free-quota and billed models on one channel, and the
    # mix moves over time, so each model can be switched by hand.
    tiers: dict[str, str] = field(default_factory=dict)
    # Vendor registry bookkeeping: provider id + billing mode key from the
    # builtin providers.json registry; empty means a custom/unknown channel.
    provider: str = ""
    mode: str = ""
    # Management-plane credentials bound to THIS channel (the channel is the
    # account: two Volcengine accounts = two channels, each with its own
    # IAM AK/SK). Vendor-level config.credentials stays as the shared
    # fallback for channels that don't carry their own.
    creds: dict[str, str] = field(default_factory=dict)
    # Optional outbound http(s) proxy for foreign upstreams that are not
    # reachable directly (e.g. "http://127.0.0.1:7890"); empty = direct.
    proxy: str = ""
    # Free-tier allowance per upstream model. Explicit channel config wins;
    # otherwise the vendor registry mode supplies the baseline.
    free_quota: FreeQuota | None = None
    # Per-model one-time packs (中国电信 / SCNet style): each wire model carries
    # its own amount and expiry, so a pack drained on one model never blocks
    # its siblings. A matching entry wins over the channel-wide `free_quota`
    # above; models without one fall back to it.
    model_quota: dict[str, FreeQuota] | None = None
    # True for vendors that hand every model its own quota (百炼 / 火山引擎):
    # a model running out of allowance then cools only that model instead of
    # freezing the whole channel. Balance/billing errors still freeze the
    # channel — the account itself is out.
    per_model_quota: bool = False
    # Optional link to a config.accounts entry. Empty means "this channel is
    # its own account" — the legacy model every single-key vendor uses.
    account: str = ""
    # Business line this channel serves: "coding" / "agent" / "payg" / "".
    # Empty falls back to `mode` (the registry billing-mode key).
    business_line: str = ""
    # Stable identifier assigned once by the server and persisted in
    # router.json. Unlike `name` (which the operator may edit at will), the id
    # never changes, so subscriptions that link channels by id survive a
    # rename. Empty on legacy configs parsed without the server's backfill;
    # `channel_ref` then falls back to the name.
    id: str = ""
    # Multimodal routing: when a request carries image/document/audio parts
    # the router only sends it to channels that can take them. Per channel:
    #   "vision": false            → text-only, never receives image requests
    #   "vision": ["wire", ...]    → only these wire models accept images
    #   "vision_model": "wire-x"   → image requests are re-pointed to this model
    # Unset means capable: an unconfigured gateway forwards as before and the
    # upstream stays the final judge.
    vision: bool | tuple[str, ...] | None = None
    vision_model: str = ""


@dataclass(frozen=True)
class Route:
    name: str
    protocol: str
    channels: tuple[str, ...]
    # Route-level system prompt prepended before routing (P1-3 §2.3); empty
    # means the client's request is sent untouched.
    system_prompt: str = ""
    # Load-balancing strategy override: "" inherits defaults.lb_strategy,
    # "order" keeps chain order, "weighted" ranks by success/latency/errors.
    lb: str = ""


@dataclass(frozen=True)
class Account:
    id: str
    provider: str
    label: str
    credentials: dict[str, str] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class Listen:
    host: str
    port: int
    api_key: str | None
    admin_key: str | None = None
    # The master key can be paused from the console; member keys keep working.
    api_key_enabled: bool = True


@dataclass(frozen=True)
class Defaults:
    cooldown_minutes: int = 300
    max_failover: int = 3
    sticky_minutes: int = 30
    max_cooldown_hours: int = 168
    timeout: int = 120
    track_stream_usage: bool = True
    preempt_quota: bool = True
    catalog_refresh_hours: int = 24  # 0 disables the background catalog sweep
    maintenance_hours: int = 24  # 0 disables the daily self-maintenance sweep
    # Model name routed through (preferably free) channels for the LLM part of
    # self-maintenance; empty disables LLM analysis, deterministic rules still run.
    maintenance_model: str = ""
    # 0 disables the free-quota alignment sweep (freealign.py). Half an hour:
    # the sweep is also the reseed point of the self-metered free budget
    # (server.App._seed_free_budget), so its cadence bounds how long a dry
    # model can silently bill the account balance. Fractional hours allowed.
    free_align_hours: float = 0.5
    # 0 disables the scheduled free-pool verification sweep: every enabled
    # free channel's mappings are pinged, confirmed-dead ids get an expiring
    # label so routing stops calling them (manual button keeps working).
    free_verify_hours: int = 6
    # 0 disables the source-watch sweep (watch.py): vendor registry docs /
    # models URLs, channel catalogs and subscription plan docs are re-fetched
    # and their model-set changes recorded as console change events. The
    # watch only observes — it never edits config or registry data.
    watch_hours: int = 12
    # 0 disables the daily ModelScope magicube check-in (modelscope_quota):
    # a signed-in login/info request claims the day's grant. The value is an
    # enable switch and a status hint — the sweep itself runs once per Beijing
    # morning (random minute in 08:30-10:00), not on a plain interval.
    modelscope_checkin_hours: int = 24
    # Usage buckets (days/users) older than this are pruned on the day
    # switch, keeping usage.json and the model_usage scans bounded.
    # 0 keeps everything forever.
    usage_retention_days: int = 180
    # Alarm when the data directory's free space drops below this many MB:
    # with a full volume every persisted artefact fails silently otherwise.
    # 0 disables the check.
    disk_min_free_mb: int = 512
    # 404 (model removed upstream) cools this many hours with no half-open
    # probing; 0 keeps the old 30-second transient behaviour.
    not_found_cooldown_hours: int = 24
    # Daily-quota 429 without a reset header: 0 cools until the next UTC
    # midnight, >0 a fixed number of hours, -1 disables the special case.
    daily_429_cooldown_hours: int = 0
    # Same-channel retries for transient network errors before failing over.
    network_retries: int = 1
    # Stability-score half-life in hours: a (channel, model) score decays
    # toward 0 with exponential half-life so old failures (and old successes)
    # fade. 0 disables decay and keeps a pure running total.
    score_halflife_hours: float = 12.0
    # Load-balancing strategy for candidate ordering: "order" keeps the
    # chain order (only known-bad pairs sink), "weighted" ranks candidates
    # by success rate, latency EWMA, consecutive errors and stability score.
    lb_strategy: str = "order"
    # Background health probing of cooled channels: 0 disables the prober.
    # Only channel-level cooldowns that should end soon get probed.
    probe_interval_seconds: int = 0
    # Probe request type: "models" (GET /models, free) or "chat" (1-token
    # chat via the maintenance direct-call path).
    probe_mode: str = "models"
    # Cooldowns longer than this many seconds are skipped by the prober
    # (Retry-After / daily-quota windows end on their own schedule).
    probe_max_cooldown_seconds: int = 600


@dataclass(frozen=True)
class ModelPolicy:
    """How a client-supplied model name maps to routing behaviour."""

    branded: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_BRANDED_MODELS))
    fallback_unknown: bool = True
    fallback_route: str = "default"


def parse_model_policy(raw) -> ModelPolicy:
    if raw is None:
        return ModelPolicy()
    if not isinstance(raw, dict):
        raise ValueError("models 策略必须是 JSON 对象")
    branded = dict(DEFAULT_BRANDED_MODELS)
    extra = raw.get("branded") or {}
    if not isinstance(extra, dict):
        raise ValueError("models.branded 必须是对象")
    for name, hint in extra.items():
        if (not isinstance(name, str) or not name.strip()
                or not isinstance(hint, str) or not hint.strip()):
            raise ValueError("models.branded 的每项必须是非空的 模型名 -> 路由名 映射")
        key = name.strip()
        if key in (AUTO_MODEL, "facet") or key.startswith(INTERNAL_MODEL_PREFIXES):
            raise ValueError(f"models.branded：{key!r} 是保留的内部模型名")
        branded[key] = hint.strip()
    fallback_unknown = raw.get("fallback_unknown", True)
    if not isinstance(fallback_unknown, bool):
        raise ValueError("models.fallback_unknown 必须是 true 或 false")
    fallback_route = raw.get("fallback_route") or "default"
    if not isinstance(fallback_route, str) or not fallback_route.strip():
        raise ValueError("models.fallback_route 必须是路由名")
    return ModelPolicy(branded, fallback_unknown, fallback_route.strip())


@dataclass(frozen=True)
class Config:
    listen: Listen
    defaults: Defaults
    channels: tuple[Channel, ...]
    routes: dict[str, Route]
    models: ModelPolicy = field(default_factory=ModelPolicy)
    # Optional accounts shared across channels. When `channel.account` is
    # set on a channel, the channel's resolve_creds path prefers
    # Account.credentials over channel.creds.
    accounts: tuple[Account, ...] = field(default_factory=tuple)
    # Vendor management credentials for free-quota CLI tools, keyed by
    # provider id then env-var name, resolved from plaintext or `env:VAR`
    # at parse time (missing variables are dropped). The raw section stays
    # in App.raw for the editor; these resolved values are injected into
    # the vendor command's subprocess environment.
    credentials: dict[str, dict[str, str]] = field(default_factory=dict)

    def channel(self, name: str) -> Channel | None:
        return next((c for c in self.channels if c.name == name), None)

    def channel_by_ref(self, ref: str) -> Channel | None:
        """Resolve a channel reference: stable id first, then legacy name.

        Subscriptions store channel ids, but files written before ids existed
        still carry names, so both forms have to resolve.
        """
        key = str(ref or "").strip()
        if not key:
            return None
        by_id = next((c for c in self.channels if c.id and c.id == key), None)
        return by_id or self.channel(key)

    def route(self, protocol: str, hint: str) -> Route | None:
        return self.routes.get(f"{protocol}:{hint}") or self.routes.get(f"{protocol}:default")


def wire_model(channel: Channel, exposed: str) -> str | None:
    """Map an exposed model name to the name this channel expects on the wire."""
    if exposed.startswith("auto"):
        auto = channel.models.get("auto")
        if auto:
            return auto
        return next((v for k, v in channel.models.items() if k != "*" and v != "*"), None)
    if exposed in channel.models:
        return channel.models[exposed]
    if "*" in channel.models:
        wildcard = channel.models["*"]
        return exposed if wildcard == "*" else wildcard
    return None


def explicit_models(channel: Channel) -> set[str]:
    """Names a channel declares explicitly (internal and wildcard keys excluded)."""
    return {key for key in channel.models if key not in (AUTO_MODEL, "*")}


def dispatch_wire_model(channel: Channel, requested: str = AUTO_MODEL) -> str | None:
    """Wire model to use when the router schedules this channel itself."""
    auto = channel.models.get(AUTO_MODEL)
    if auto and auto != "*":
        return auto
    for key, value in channel.models.items():
        if key not in (AUTO_MODEL, "*") and value and value != "*":
            return value
    wildcard = channel.models.get("*")
    if wildcard == "*":
        return requested
    return wildcard or None


# Message-part types that make a request multimodal: the OpenAI chat shapes
# (image_url / input_image / file / audio) and the Anthropic block names
# (image / document). Text parts are absent on purpose.
VISION_PART_TYPES = frozenset({
    "image_url", "input_image", "image", "file", "input_file",
    "input_audio", "audio", "document",
})


def body_has_vision(payload: dict) -> bool:
    """True when any message carries image/document/audio content.

    Covers the OpenAI chat shapes (content as a list or object of typed
    parts) and the Anthropic shapes (typed content blocks), so both wire
    protocols get the same routing behaviour.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) or content is None:
            continue
        parts = content if isinstance(content, list) else [content]
        for part in parts:
            if isinstance(part, dict) and str(part.get("type") or "") in VISION_PART_TYPES:
                return True
    return False


def vision_wire(channel: Channel, wire: str) -> str:
    """Wire model for an image-bearing request on this channel.

    A channel with `vision_model` re-points image requests to that model
    (its own auto mapping may be text-only); every other mapping stands.
    """
    return channel.vision_model or wire


def channel_takes_images(channel: Channel, wire: str) -> bool:
    """Config-declared multimodal capability for this channel+wire pair."""
    if channel.vision_model:
        return True
    if channel.vision is None:
        return True
    if channel.vision is False:
        return False
    return wire in channel.vision


@dataclass(frozen=True)
class ModelDecision:
    mode: str  # auto | exact | fallback | rejected
    hint: str
    exposed: str
    requested: str
    reason: str
    exact_channels: tuple[str, ...] = ()


def resolve_model(
    config: Config,
    protocol: str,
    model: str,
    hint: str | None = None,
    blocked: set[str] | frozenset[str] | None = None,
) -> ModelDecision:
    """Decide how a client-supplied model name is served.

    Branded/reserved names always mean "schedule internally". Anything else is
    first matched against the explicit `models` keys of the channels in the
    selected route chain; when nothing matches, the same chain is scheduled
    internally (unless `fallback_unknown` is disabled).
    """
    policy = config.models
    requested = (model or "").strip() or AUTO_MODEL
    header_hint = (hint or "").strip() or None
    excluded = set(blocked or ())

    internal_hint: str | None = None
    if requested in policy.branded:
        internal_hint = policy.branded[requested]
    elif requested in (AUTO_MODEL, "facet"):
        internal_hint = "default"
    elif requested.startswith(INTERNAL_MODEL_PREFIXES):
        internal_hint = requested.split(":", 1)[1].strip() or "default"

    route_hint = header_hint or internal_hint or policy.fallback_route or "default"
    route = config.route(protocol, route_hint)
    ordered = route.channels if route else ()

    if internal_hint is not None:
        mode = "agent" if requested == AGENT_MODEL else "auto"
        return ModelDecision(mode, route_hint, AUTO_MODEL, requested,
                             "internal-model-name")

    exact = tuple(
        name for name in ordered
        if name not in excluded
        and (channel := config.channel(name)) is not None
        and channel.enabled and channel.protocol == protocol
        and requested in explicit_models(channel)
    )
    if exact:
        return ModelDecision("exact", route_hint, requested, requested,
                             "exact-model-match", exact)
    if policy.fallback_unknown:
        return ModelDecision("fallback", route_hint, AUTO_MODEL, requested,
                             "unknown-model-internal-schedule")
    return ModelDecision("rejected", route_hint, requested, requested,
                         "unknown-model-fallback-disabled")


def parse_config(raw: dict) -> Config:
    """Validate an in-memory config dict (used by the editor's hot reload)."""
    if not isinstance(raw, dict):
        raise ValueError("config 必须是 JSON 对象")

    listen_raw = raw.get("listen") or {}
    listen = Listen(
        host=listen_raw.get("host", "127.0.0.1"),
        port=int(listen_raw.get("port", 8787)),
        api_key=resolve_secret(listen_raw.get("api_key")),
        admin_key=resolve_secret(listen_raw.get("admin_key")),
        api_key_enabled=bool(listen_raw.get("api_key_enabled", True)),
    )
    if listen.host not in LOOPBACK and not listen.api_key:
        raise ValueError("非环回地址监听必须设置 listen.api_key（可用 env:VAR）")

    d = raw.get("defaults") or {}
    track_stream_usage = d.get("track_stream_usage", True)
    if not isinstance(track_stream_usage, bool):
        raise ValueError("defaults.track_stream_usage 必须是 true 或 false")
    preempt_quota = d.get("preempt_quota", True)
    if not isinstance(preempt_quota, bool):
        raise ValueError("defaults.preempt_quota 必须是 true 或 false")
    lb_strategy = str(d.get("lb_strategy") or "order").strip().lower()
    if lb_strategy not in ("order", "weighted"):
        raise ValueError("defaults.lb_strategy 必须是 'order' 或 'weighted'")
    probe_mode = str(d.get("probe_mode") or "models").strip().lower()
    if probe_mode not in ("models", "chat"):
        raise ValueError("defaults.probe_mode 必须是 'models' 或 'chat'")
    defaults = Defaults(
        cooldown_minutes=int(d.get("cooldown_minutes", 300)),
        max_failover=int(d.get("max_failover", 3)),
        sticky_minutes=int(d.get("sticky_minutes", 30)),
        max_cooldown_hours=int(d.get("max_cooldown_hours", 168)),
        timeout=int(d.get("timeout", 120)),
        track_stream_usage=track_stream_usage,
        preempt_quota=preempt_quota,
        catalog_refresh_hours=int(d.get("catalog_refresh_hours", 24)),
        maintenance_hours=int(d.get("maintenance_hours", 24)),
        maintenance_model=str(d.get("maintenance_model") or "").strip(),
        free_align_hours=float(d.get("free_align_hours", 0.5)),
        free_verify_hours=int(d.get("free_verify_hours", 6)),
        watch_hours=int(d.get("watch_hours", 12)),
        modelscope_checkin_hours=int(d.get("modelscope_checkin_hours", 24)),
        usage_retention_days=int(d.get("usage_retention_days", 180)),
        disk_min_free_mb=int(d.get("disk_min_free_mb", 512)),
        not_found_cooldown_hours=int(d.get("not_found_cooldown_hours", 24)),
        daily_429_cooldown_hours=int(d.get("daily_429_cooldown_hours", 0)),
        network_retries=int(d.get("network_retries", 1)),
        score_halflife_hours=float(d.get("score_halflife_hours", 12.0)),
        lb_strategy=lb_strategy,
        probe_interval_seconds=int(d.get("probe_interval_seconds", 0)),
        probe_mode=probe_mode,
        probe_max_cooldown_seconds=int(d.get("probe_max_cooldown_seconds", 600)),
    )

    # Vendor management credentials: {provider: {ENV_NAME: value}}. Values
    # may be plaintext or `env:VAR` references (resolved here so free-quota
    # commands receive the actual secret); malformed entries are dropped,
    # never fatal — a broken credentials section must not brick the config.
    credentials: dict[str, dict[str, str]] = {}
    creds_raw = raw.get("credentials")
    if isinstance(creds_raw, dict):
        for vendor_id, entries in creds_raw.items():
            if not isinstance(entries, dict):
                continue
            resolved: dict[str, str] = {}
            for env_name, value in entries.items():
                if not isinstance(value, str):
                    continue
                secret = resolve_secret(value.strip())
                if secret:
                    resolved[str(env_name)] = secret
            if resolved:
                credentials[str(vendor_id)] = resolved

    # Optional accounts shared across channels.
    accounts: tuple[Account, ...] = ()
    accounts_raw = raw.get("accounts") or []
    if isinstance(accounts_raw, list):
        # Imported here to avoid an import cycle (providers imports core).
        from .accounts import ValidationError, validate_account
        from .providers import load_providers
        prov = load_providers()
        for a_raw in accounts_raw:
            if not isinstance(a_raw, dict):
                continue
            try:
                a = validate_account(a_raw, prov)
            except ValidationError:
                continue  # silently skip bad entries
            # Same env: handling as the vendor slot: a reference resolves at
            # load time so callers only ever see plain secrets.
            acc_creds: dict[str, str] = {}
            for env_name, value in (a.get("credentials") or {}).items():
                secret = resolve_secret(str(value).strip())
                if secret:
                    acc_creds[str(env_name)] = secret
            accounts = accounts + (Account(
                id=a["id"], provider=a["provider"], label=a["label"],
                credentials=acc_creds,
                note=a.get("note") or "",
            ),)
    channels: list[Channel] = []
    seen: set[str] = set()
    for item in raw.get("channels") or []:
        name = item.get("name")
        if not name or name in seen:
            raise ValueError(f"渠道名重复或缺失：{name!r}")
        seen.add(name)
        protocol = item.get("protocol", OPENAI)
        if protocol not in PROTOCOLS:
            raise ValueError(f"channel {name}：protocol 必须是 {PROTOCOLS} 之一")
        base_url = (item.get("base_url") or "").rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"channel {name}：base_url 必须是 http(s) URL")
        upstream_path = item.get("upstream_path")
        if upstream_path is not None:
            if (not isinstance(upstream_path, str) or not upstream_path.startswith("/")
                    or "://" in upstream_path):
                raise ValueError(
                    f"channel {name}：upstream_path 必须是以 / 开头的路径"
                )
            upstream_path = upstream_path.rstrip("/") or "/"
        proxy = resolve_secret(str(item.get("proxy") or "").strip()) or ""
        if proxy and not proxy.lower().startswith(("http://", "https://")):
            raise ValueError(f"channel {name}：proxy 必须是 http(s):// URL")
        creds_raw = item.get("creds") or {}
        if not isinstance(creds_raw, dict):
            raise ValueError(f"channel {name}：creds 必须是对象")
        creds = {str(k): str(v).strip() for k, v in creds_raw.items()
                 if isinstance(v, (str, int, float)) and str(v).strip()}
        tier = str(item.get("tier") or "").strip().lower()
        if tier not in ("", "free", "paid"):
            raise ValueError(f'channel {name}：tier 必须是 ""、"free" 或 "paid"')
        tiers_raw = item.get("tiers") or {}
        if not isinstance(tiers_raw, dict):
            raise ValueError(f"channel {name}：tiers 必须是对象")
        tiers = {}
        for model_name, mark in tiers_raw.items():
            key = str(model_name).strip()
            mark = str(mark or "").strip().lower()
            if not key:
                raise ValueError(f"channel {name}：tiers 的键必须是模型名")
            if mark not in ("", "free", "paid"):
                raise ValueError(
                    f'channel {name}：tiers 的值必须是 ""、"free" 或 "paid"'
                )
            if mark:
                tiers[key] = mark
        models = item.get("models") or {}
        if not models:
            raise ValueError(f"channel {name}：必须提供 models 映射")
        rate_raw = item.get("rate") or {}
        rate = Rate(
            rpm=rate_raw.get("rpm"), rpd=rate_raw.get("rpd"),
            tpm=rate_raw.get("tpm"), tpd=rate_raw.get("tpd"),
        )
        try:
            free_quota = parse_free_quota(item.get("free_quota"))
        except ValueError as exc:
            raise ValueError(f"channel {name}：{exc}") from None
        model_quota = parse_model_quota(name, item.get("model_quota"))
        vision_raw = item.get("vision")
        vision: bool | tuple[str, ...] | None = None
        if vision_raw is not None:
            if isinstance(vision_raw, bool):
                vision = vision_raw
            elif isinstance(vision_raw, list):
                vision = tuple(dict.fromkeys(
                    str(entry).strip() for entry in vision_raw if str(entry).strip()))
                if not vision:
                    raise ValueError(f"channel {name}：vision 列表不能为空")
            else:
                raise ValueError(
                    f'channel {name}：vision 必须是 true、false 或模型名列表')
        vision_model = str(item.get("vision_model") or "").strip()
        if vision_model and vision is False:
            raise ValueError(f"channel {name}：vision_model 与 vision: false 冲突")
        channels.append(Channel(
            name=name,
            protocol=protocol,
            base_url=base_url,
            api_key=resolve_secret(item.get("api_key")),
            models=dict(models),
            priority=int(item.get("priority", 100)),
            cooldown_minutes=int(item.get("cooldown_minutes", defaults.cooldown_minutes)),
            rate=rate,
            timeout=int(item.get("timeout", defaults.timeout)),
            enabled=bool(item.get("enabled", True)),
            upstream_path=upstream_path,
            tier=tier,
            tiers=tiers,
            provider=str(item.get("provider") or "").strip(),
            mode=str(item.get("mode") or "").strip(),
            creds=creds,
            proxy=proxy,
            free_quota=free_quota,
            model_quota=model_quota,
            per_model_quota=bool(item.get("per_model_quota", False)),
            account=str(item.get("account") or "").strip(),
            business_line=str(item.get("business_line") or "").strip(),
            id=str(item.get("id") or "").strip(),
            vision=vision,
            vision_model=vision_model,
        ))
    if not channels:
        raise ValueError("至少需要一个渠道")
    channels.sort(key=lambda c: (c.priority, c.name))

    routes: dict[str, Route] = {}
    for item in raw.get("routes") or []:
        name = item.get("name", "default")
        protocol = item.get("protocol", OPENAI)
        names = tuple(item.get("channels") or [])
        system_prompt = str(item.get("system_prompt") or "")
        lb = str(item.get("lb") or "").strip().lower()
        if lb and lb not in ("order", "weighted"):
            raise ValueError(
                f"route {name}：lb 必须是 ''、'order' 或 'weighted'")
        if protocol not in PROTOCOLS:
            raise ValueError(f"route {name}：protocol 必须是 {PROTOCOLS} 之一")
        if not names:
            raise ValueError(f"route {name}：channels 列表为空")
        # "$free" is resolved against the already-sorted channel table, so
        # the expansion order doubles as the attempt order inside the macro.
        # A channel listed explicitly keeps its exact position, wherever it
        # sits; the macro only fills in free channels that were NOT listed.
        named = {n for n in names if n != FREE_CHANNEL_MACRO}
        expanded: list[str] = []
        for channel_name in names:
            if channel_name == FREE_CHANNEL_MACRO:
                for channel in channels:
                    if (channel.protocol == protocol and channel.tier == "free"
                            and channel.enabled and channel.name not in named):
                        expanded.append(channel.name)
            else:
                expanded.append(channel_name)
        # Defensive dedup: an explicit name repeated by hand stays unique.
        names = tuple(dict.fromkeys(expanded))
        if not expanded:
            raise ValueError(
                f"route {name}：'{FREE_CHANNEL_MACRO}' 没有匹配到"
                f"支持 {protocol} 且已启用的免费渠道")
        for channel_name in names:
            channel = next((c for c in channels if c.name == channel_name), None)
            if channel is None:
                raise ValueError(f"route {name}：未知渠道 {channel_name!r}")
        # An explicit cross-protocol name is accepted here, but the scheduler
        # skips such channels at pick time (Router.pick): a route only ever
        # dispatches channels speaking its own protocol.
        key = f"{protocol}:{name}"
        if key in routes:
            raise ValueError(f"路由重复：{key!r}")
        routes[key] = Route(name=name, protocol=protocol, channels=names,
                            system_prompt=system_prompt, lb=lb)
    for protocol in PROTOCOLS:
        if f"{protocol}:default" not in routes:
            raise ValueError(f"缺少 {protocol}:default 路由")
    model_policy = parse_model_policy(raw.get("models"))
    declared = set().union(*(explicit_models(c) for c in channels))
    for name in model_policy.branded:
        if name in declared:
            raise ValueError(
                f"models.branded：{name!r} 与渠道显式模型名冲突"
            )
    return Config(
        listen=listen, defaults=defaults, channels=tuple(channels),
        routes=routes, models=model_policy, credentials=credentials,
        accounts=accounts,
    )


def load_config(path: str | Path) -> Config:
    return parse_config(json.loads(Path(path).read_text(encoding="utf-8")))


def channel_ref(channel: Channel) -> str:
    """Reference that survives a rename: the id when present, else the name."""
    return channel.id or channel.name


def ensure_channel_ids(raw: dict) -> bool:
    """Assign a stable id to every channel that lacks one.

    Ids are generated once and persisted in the raw config, so a name edit
    never changes them. Returns True when the raw dict was modified.
    """
    if not isinstance(raw, dict):
        return False
    changed = False
    seen: set[str] = set()
    for channel in (raw.get("channels") or []):
        if not isinstance(channel, dict):
            continue
        current = str(channel.get("id") or "").strip()
        if current and current not in seen:
            seen.add(current)
            continue
        while True:
            generated = "ch-" + uuid.uuid4().hex[:12]
            if generated not in seen:
                break
        channel["id"] = generated
        seen.add(generated)
        changed = True
    return changed


@dataclass
class ChannelState:
    name: str
    cooldown_until: float = 0.0
    model_cooldown_until: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    last_error: str = ""
    consecutive_errors: int = 0
    half_open: bool = False
    req_times: deque = field(default_factory=deque)
    tok_events: deque = field(default_factory=deque)
    total_requests: int = 0
    served_requests: int = 0
    failed_requests: int = 0
    total_tokens: int = 0
    handoffs: int = 0
    last_used: float = 0.0
    quota: dict = field(default_factory=dict)
    # Exponential moving average of upstream round-trip seconds (weighted
    # strategy input); 0.0 means "unknown" and never penalizes a channel.
    latency_ewma: float = 0.0

    def model_available(self, now: float, wire: str) -> bool:
        """该模型的冷却是否已结束（不含渠道整体冷却）。"""
        return self.model_cooldown_until.get(wire, 0.0) <= now

    def _prune(self, now: float) -> None:
        while self.req_times and now - self.req_times[0] > 86400:
            self.req_times.popleft()
        while self.tok_events and now - self.tok_events[0][0] > 86400:
            self.tok_events.popleft()
        if self.model_cooldown_until:
            expired = [w for w, until in self.model_cooldown_until.items()
                       if until <= now]
            for wire in expired:
                del self.model_cooldown_until[wire]

    def available(self, now: float, rate: Rate) -> tuple[bool, str]:
        self._prune(now)
        if self.cooldown_until > now:
            if self.half_open:
                return True, "half_open_probe"
            return False, f"cooling:{self.reason}"
        if rate.rpm is not None:
            recent = sum(1 for t in self.req_times if now - t < 60)
            if recent >= rate.rpm:
                return False, "rpm_cap"
        if rate.rpd is not None and len(self.req_times) >= rate.rpd:
            return False, "rpd_cap"
        if rate.tpm is not None:
            recent = sum(n for t, n in self.tok_events if now - t < 60)
            if recent >= rate.tpm:
                return False, "tpm_cap"
        if rate.tpd is not None and sum(n for _, n in self.tok_events) >= rate.tpd:
            return False, "tpd_cap"
        return True, "ok"

    def note_attempt(self, now: float) -> None:
        self.total_requests += 1
        self.last_used = now
        self.req_times.append(now)
        if self.half_open:
            self.half_open = False

    def mark_cooldown(self, seconds: float, reason: str, now: float,
                      per_model: bool = False, *, wire: str | None = None,
                      half_open: bool = False) -> None:
        """Freeze the channel, or with `per_model` only this wire model.

        A quota window belongs to one upstream model: 百炼 and 火山引擎 each
        expose many models whose allowances run out independently, so cooling
        the whole channel would skip the sibling models (and the next channel
        in the route) for hours. Account-level failures keep using the
        channel-wide branch.
        """
        if per_model and wire:
            self.model_cooldown_until[wire] = now + seconds
        else:
            self.cooldown_until = now + seconds
            self.reason = reason
        self.consecutive_errors += 1
        self.failed_requests += 1
        self.half_open = half_open

    def mark_success(self, tokens: int, now: float,
                     wire: str | None = None) -> None:
        self.cooldown_until = 0.0
        self.reason = ""
        self.consecutive_errors = 0
        self.half_open = False
        if wire:
            self.model_cooldown_until.pop(wire, None)
        self.served_requests += 1
        if tokens:
            self.tok_events.append((now, tokens))
            self.total_tokens += tokens

    def record_latency(self, seconds: float) -> None:
        """Fold one upstream round-trip into the EWMA (α=0.5)."""
        if seconds <= 0:
            return
        self.latency_ewma = (seconds if self.latency_ewma <= 0
                             else 0.5 * self.latency_ewma + 0.5 * seconds)

    def clear_cooldown(self) -> None:
        """Lift a channel-level cooldown without touching counters.

        Used by the background health prober: a successful probe proves the
        channel is back, but it is not client traffic, so served_requests and
        the 24h windows must stay untouched (mark_success would count it).
        """
        self.cooldown_until = 0.0
        self.reason = ""
        self.half_open = False

    def snapshot(self, now: float) -> dict:
        cooling = max(0.0, self.cooldown_until - now)
        return {
            "cooling": cooling > 0,
            "cooling_seconds": round(cooling, 1),
            "reason": self.reason if cooling > 0 else "",
            "model_cooldowns": {
                wire: round(until - now, 1)
                for wire, until in self.model_cooldown_until.items()
                if until > now
            },
            "consecutive_errors": self.consecutive_errors,
            "last_error": self.last_error[:300],
            "total_requests": self.total_requests,
            "served_requests": self.served_requests,
            "failed_requests": self.failed_requests,
            "total_tokens": self.total_tokens,
            "handoffs": self.handoffs,
            "last_used": self.last_used,
            "quota": quota_view(self.quota, now),
            "latency_ms": round(self.latency_ewma * 1000) if self.latency_ewma > 0 else 0,
        }


class StateStore:
    """Thread-safe channel health, sticky sessions and candidate selection."""

    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.RLock()
        self.states = {c.name: ChannelState(c.name) for c in config.channels}
        self.sessions: dict[str, tuple[str, float]] = {}
        # name -> {"fetched_at", "endpoint", "models": [...], "changes": {...}}
        self.catalogs: dict[str, dict] = {}
        # f"{protocol}:{route}" -> next round-robin offset for new sessions
        self.rr_cursor: dict[str, int] = {}
        # Per-(channel, wire-model) stability score: +1 per served request,
        # -5 per non-quota failure; quota exhaustion only cools, never scores.
        # Keyed "channel|wire_model"; each value is [score, last_ts] with
        # exponential decay applied lazily on read/write. Missing keys = 0.0.
        self.scores: dict[str, list[float, float]] = {}
        # Last rotation performed by _rotate_tier, formatted "offset/tier_size"
        # for the [route] selection log; empty when rotation was not possible.
        self.last_rotate = ""
        # channel name -> (billing group, expiry epoch, burn tiebreak) fed by
        # the App from the subscription file: 0 = monthly plan (soonest expiry
        # first; third slot is -remaining%/day for same-day tiebreaks), 1 =
        # monthly without a renewal date, 2 = free tier, 3 = pay-as-you-go.
        # Only explicit model requests consult this map; auto/free routing
        # keeps the console chain order. Replaced wholesale on refresh, so a
        # pick always sees one consistent snapshot.
        self.billing_ranks: dict[str, tuple[int, float, float]] = {}

    def _halflife_seconds(self) -> float:
        return self.config.defaults.score_halflife_hours * 3600.0

    def _decayed(self, key: str, now: float) -> float:
        """Current decayed score for a key, without mutating storage.

        Pure read: the timestamp stays put so repeated reads at the same
        moment are idempotent. Writing decays up front and refreshes the
        timestamp instead.
        """
        entry = self.scores.get(key)
        if entry is None:
            return 0.0
        value, last_ts = entry
        half = self._halflife_seconds()
        if half <= 0.0 or now <= last_ts:
            return float(value)
        return float(value) * (0.5 ** ((now - last_ts) / half))

    def score(self, channel: str, wire_model: str,
              now: float | None = None) -> float:
        now = time.time() if now is None else now
        with self.lock:
            return self._decayed(f"{channel}|{wire_model}", now)

    def record_score(self, channel: str, wire_model: str, delta: int,
                     now: float | None = None) -> None:
        now = time.time() if now is None else now
        key = f"{channel}|{wire_model}"
        with self.lock:
            value = self._decayed(key, now) + delta
            if abs(value) < 1e-9:
                self.scores.pop(key, None)
            else:
                self.scores[key] = [value, now]

    def scores_for(self, channel: str, now: float | None = None) -> dict[str, int]:
        """One channel's decayed score map keyed by wire model (for the console)."""
        now = time.time() if now is None else now
        prefix = f"{channel}|"
        with self.lock:
            return {k[len(prefix):]: int(round(self._decayed(k, now)))
                    for k in self.scores if k.startswith(prefix)}

    def reset_scores(self, channel: str | None = None,
                     wire_model: str | None = None) -> int:
        """Clear all scores, one channel's, or one (channel, wire) pair."""
        with self.lock:
            if channel is None:
                removed = len(self.scores)
                self.scores.clear()
                return removed
            prefix = f"{channel}|"
            if wire_model is None:
                keys = [k for k in self.scores if k.startswith(prefix)]
                for k in keys:
                    del self.scores[k]
                return len(keys)
            key = f"{channel}|{wire_model}"
            return 1 if self.scores.pop(key, None) is not None else 0

    def record_catalog(self, name: str, models, endpoint: str,
                       now: float | None = None) -> dict:
        """Store an upstream model catalog and diff it against the last one."""
        now = time.time() if now is None else now
        fresh = sorted({str(m) for m in (models or []) if m})
        with self.lock:
            previous = self.catalogs.get(name) or {}
            old = set(previous.get("models") or ())
            changes = {
                "added": sorted(set(fresh) - old),
                "removed": sorted(old - set(fresh)),
            }
            if old:
                changes["at"] = now
            self.catalogs[name] = {
                "fetched_at": now,
                "endpoint": endpoint,
                "models": fresh,
                "changes": changes,
            }
            return changes

    def catalog_view(self, name: str) -> dict:
        """Summary of one recorded catalog (full model list included)."""
        with self.lock:
            catalog = self.catalogs.get(name) or {}
            return {
                "fetched_at": catalog.get("fetched_at", 0),
                "endpoint": catalog.get("endpoint", ""),
                "models": list(catalog.get("models") or []),
                "count": len(catalog.get("models") or []),
                "changes": dict(catalog.get("changes") or {}),
            }

    def catalog_knows(self, model: str) -> bool:
        """True when any channel's catalog lists this model name."""
        if not model:
            return False
        with self.lock:
            for catalog in self.catalogs.values():
                if model in (catalog.get("models") or []):
                    return True
        return False

    def session_channel(self, session_id: str | None, now: float) -> str | None:
        if not session_id:
            return None
        bound = self.sessions.get(session_id)
        if not bound:
            return None
        name, expires = bound
        if expires < now:
            self.sessions.pop(session_id, None)
            return None
        return name

    def bind_session(self, session_id: str | None, name: str, now: float) -> None:
        if session_id:
            self.sessions[session_id] = (name, now + self.config.defaults.sticky_minutes * 60)

    def _rotate_tier(self, protocol: str, route: Route, ordered: list[str]) -> list[str]:
        """Round-robin the starting point among a route's top-priority tier.

        Sticky sessions are untouched (the caller keeps them in front). A brand
        new session starts at the next channel of the tier so free quota is
        spread instead of everyone hammering the first channel until it cools.
        Lower-priority channels keep their tail positions as fallback.
        """
        if len(ordered) < 2:
            self.last_rotate = ""
            return ordered
        head = self.config.channel(ordered[0])
        if head is None:
            self.last_rotate = ""
            return ordered
        positions = [i for i, name in enumerate(ordered)
                     if (c := self.config.channel(name)) is not None
                     and c.priority == head.priority]
        if len(positions) < 2:
            self.last_rotate = ""
            return ordered
        key = f"{protocol}:{route.name}"
        offset = self.rr_cursor.get(key, 0) % len(positions)
        self.rr_cursor[key] = offset + 1
        self.last_rotate = f"{offset}/{len(positions)}"
        if not offset:
            return ordered
        tier = [ordered[i] for i in positions]
        rotated = tier[offset:] + tier[:offset]
        result = list(ordered)
        for pos, name in zip(positions, rotated):
            result[pos] = name
        return result

    @staticmethod
    def _fmt_pairs(pairs: list[tuple[Channel, str]], limit: int = 5) -> str:
        shown = [f"{channel.name}->{wire}" for channel, wire in pairs[:limit]]
        extra = len(pairs) - len(shown)
        text = " ".join(shown) if shown else "none"
        return f"{text} +{extra}" if extra > 0 else text

    def _log_pick(self, protocol: str, route: Route, model: str,
                  decision: ModelDecision, sticky: str | None,
                  skipped: list[str], strategy: str,
                  result: list[tuple[Channel, str]]) -> None:
        """One [route] line per pick: how the candidate chain was built.

        Mirrors the [inbound] shape-log style: a single flushed stdout line,
        no configuration, so a running gateway can be read top to bottom.
        """
        picked = (f"{result[0][0].name}->{result[0][1]}"
                  if result else "none")
        parts = [
            f"[route] {protocol}:{route.name}",
            f"model={model!r}",
            f"mode={decision.mode}",
            f"reason={decision.reason}",
            f"sticky={sticky or 'no'}",
            f"rotate={self.last_rotate or '-'}",
        ]
        if strategy:
            parts.append(f"lb={strategy}")
        parts.append("skipped=" + (" ".join(skipped[:5]) or "-")
                     + (f" +{len(skipped) - 5}" if len(skipped) > 5 else ""))
        parts.append(f"picked={picked}")
        parts.append(f"chain={self._fmt_pairs(result)}")
        print(" ".join(parts), flush=True)

    def _debug_detail(self, protocol: str, route: Route, ordered: list[str],
                      detail: dict[str, str],
                      result: list[tuple[Channel, str]]) -> None:
        """[route:debug] block: every route channel, its priority and verdict.

        Printed only when FACET_ROUTE_DEBUG is set. The two questions this
        answers that the compact [route] line cannot: which channels share the
        head priority tier (only that tier rotates), and which position each
        channel actually holds after the rotation.
        """
        if not route_debug_enabled():
            return
        head = self.config.channel(ordered[0]) if ordered else None
        tier = [name for name in ordered
                if (c := self.config.channel(name)) is not None
                and head is not None and c.priority == head.priority]
        print(f"[route:debug] {protocol}:{route.name} "
              f"head_prio={head.priority if head else '-'} "
              f"rotate_tier={len(tier)}[{','.join(tier)}] "
              f"rotate={self.last_rotate or '-'}", flush=True)
        for i, name in enumerate(ordered):
            channel = self.config.channel(name)
            prio = channel.priority if channel else "-"
            print(f"[route:debug]   #{i} {name} prio={prio} "
                  f"{detail.get(name, 'wire=- queued')}", flush=True)
        final = " ".join(f"#{i}:{ch.name}->{wire}"
                         for i, (ch, wire) in enumerate(result[:12]))
        print(f"[route:debug]   attempt-order {final or 'none'}", flush=True)

    def pick(
        self,
        protocol: str,
        hint: str,
        model: str,
        session_id: str | None,
        now: float | None = None,
        decision: ModelDecision | None = None,
        blocked: set[str] | frozenset[str] | None = None,
        agent_candidates: list[tuple[str, str]] | None = None,
    ) -> tuple[list[tuple[Channel, str]], Route | None]:
        """Return ([(channel, wire_model), ...], route) in attempt order.

        `decision` comes from resolve_model. Exact matches get their own
        channels first; if every exact channel is cooling, the router's own
        schedule takes over so a quota window never breaks the calling tool.

        `agent` mode (decision.mode == "agent") uses `agent_candidates`, a
        pre-ranked [(channel_name, wire_model), ...] list of models that can
        actually drive a tool (see App._agent_candidates). The order is kept
        verbatim — no stability re-sort — so the attempts match the console's
        recommendation table. Only channels this route can reach are used,
        and a session's sticky channel does not pin the choice: the candidate
        set itself changes as scores and catalogs move.
        """
        now = time.time() if now is None else now
        excluded = set(blocked or ())
        with self.lock:
            if decision is None:
                decision = resolve_model(self.config, protocol, model, hint, excluded)
            if decision.mode == "rejected":
                print(f"[route] {protocol} model={model!r} "
                      f"rejected reason={decision.reason}", flush=True)
                return [], None
            route = self.config.route(protocol, decision.hint)
            if route is None:
                print(f"[route] {protocol} model={model!r} mode={decision.mode} "
                      f"no-route hint={decision.hint!r}", flush=True)
                return [], None
            ordered = list(route.channels)
            sticky = self.session_channel(session_id, now)
            if sticky in ordered:
                ordered.remove(sticky)
                ordered.insert(0, sticky)
                self.last_rotate = ""
            else:
                ordered = self._rotate_tier(protocol, route, ordered)
            exact: list[tuple[Channel, str]] = []
            scheduled: list[tuple[Channel, str]] = []
            reachable: dict[str, Channel] = {}
            skipped: list[str] = []
            detail: dict[str, str] = {}
            for name in ordered:
                channel = self.config.channel(name)
                if channel is None:
                    skipped.append(f"{name}(missing)")
                    detail[name] = "skip:missing"
                    continue
                if not channel.enabled:
                    skipped.append(f"{name}(disabled)")
                    detail[name] = "skip:disabled"
                    continue
                if name in excluded:
                    skipped.append(f"{name}(blocked)")
                    detail[name] = "skip:blocked"
                    continue
                if channel.protocol != protocol:
                    skipped.append(f"{name}(protocol={channel.protocol})")
                    detail[name] = f"skip:protocol={channel.protocol}"
                    continue
                ok, why = self.states[name].available(now, channel.rate)
                if not ok:
                    skipped.append(f"{name}({why or 'unavailable'})")
                    left = _cooldown_left(self.states[name].cooldown_until, now)
                    detail[name] = f"skip:{why or 'unavailable'}({left})"
                    continue
                reachable[name] = channel
                if decision.mode == "exact" and name in decision.exact_channels:
                    wire = wire_model(channel, decision.exposed)
                    if wire and self.states[name].model_available(now, wire):
                        exact.append((channel, wire))
                        detail[name] = f"wire={wire} exact"
                    elif wire:
                        skipped.append(f"{name}({wire}:cooling)")
                        detail[name] = (f"wire={wire} skip:model-cooling"
                                        f"({_cooldown_left(self.states[name].model_cooldown_until.get(wire, 0.0), now)})")
                    else:
                        skipped.append(f"{name}(no-wire)")
                        detail[name] = "skip:no-wire"
                    continue
                if decision.mode == "agent":
                    detail[name] = "reachable (agent candidates)"
                    continue  # filled from the caller's ranked list below
                wire = dispatch_wire_model(channel, decision.requested)
                if wire and self.states[name].model_available(now, wire):
                    scheduled.append((channel, wire))
                    detail[name] = f"wire={wire} queued"
                elif wire:
                    skipped.append(f"{name}({wire}:cooling)")
                    detail[name] = (f"wire={wire} skip:model-cooling"
                                    f"({_cooldown_left(self.states[name].model_cooldown_until.get(wire, 0.0), now)})")
                else:
                    skipped.append(f"{name}(no-wire)")
                    detail[name] = "skip:no-wire"
            if decision.mode == "agent":
                for name, wire in (agent_candidates or ()):
                    channel = reachable.get(name)
                    if channel is not None and self.states[name].model_available(now, wire):
                        scheduled.append((channel, wire))
                    else:
                        why = "unreachable" if channel is None else f"{wire}:cooling"
                        skipped.append(f"{name}({why})")
                if not scheduled:
                    # No usable catalog entry anywhere (fresh install, or
                    # every catalog model is tool-less / labelled dead): give
                    # each reachable channel one shot through its own auto
                    # mapping so the alias still works.
                    for channel in reachable.values():
                        wire = dispatch_wire_model(channel, decision.requested)
                        if wire and self.states[channel.name].model_available(now, wire):
                            scheduled.append((channel, wire))
                self._log_pick(protocol, route, model, decision, sticky,
                               skipped, "", scheduled)
                self._debug_detail(protocol, route, ordered, detail,
                                   scheduled)
                return scheduled, route
            # Stability reordering: within each group, higher score first;
            # equal scores keep the chain order. The sticky session channel
            # (position 0, if any) stays pinned to the front. The "weighted"
            # strategy additionally ranks by success rate, latency and
            # consecutive errors; "order" (default) is unchanged.
            strategy = route.lb or self.config.defaults.lb_strategy
            if decision.mode == "exact" and exact:
                # A request that names a model exactly prefers quota that
                # would otherwise expire: monthly plans (soonest expiry
                # first), then free tiers, then pay-as-you-go. Internal
                # aliases (auto/free) keep the chain-order strategies below.
                result = self._billing_order(exact, sticky)
            else:
                ordered_pairs = exact or scheduled
                if decision.mode != "exact":
                    ordered_pairs = scheduled
                result = (self._weighted_order(ordered_pairs, sticky, now)
                          if strategy == "weighted"
                          else self._by_score(ordered_pairs, sticky))
            self._log_pick(protocol, route, model, decision, sticky,
                           skipped, strategy, result)
            self._debug_detail(protocol, route, ordered, detail, result)
            return result, route

    def _by_score(self, pairs: list[tuple[Channel, str]],
                  sticky: str | None) -> list[tuple[Channel, str]]:
        """Chain order decides; only known-bad pairs sink to the back.

        The console's route order is authoritative: untested and healthy
        pairs keep their chain position, so a channel or model that was never
        called still gets its turn. A pair whose decayed score sits at or
        below DEMOTE_SCORE is known-bad and moves behind its siblings; the
        score decays toward zero, which naturally restores its position.
        """
        if len(pairs) < 2:
            return pairs
        head = 1 if (sticky and pairs and pairs[0][0].name == sticky) else 0
        body = pairs[head:]
        healthy, bad = [], []
        for pair in body:
            if self.score(pair[0].name, pair[1]) <= DEMOTE_SCORE:
                bad.append(pair)
            else:
                healthy.append(pair)
        bad.sort(key=lambda pair: -self.score(pair[0].name, pair[1]))
        return pairs[:head] + healthy + bad

    def _billing_order(self, pairs: list[tuple[Channel, str]],
                       sticky: str | None) -> list[tuple[Channel, str]]:
        """Explicit model requests drain expiring quota before paying cash.

        Ranks come from App._refresh_billing_ranks: monthly plans first
        (soonest linked subscription expiry first, undated plans next), then
        free-tier channels, then pay-as-you-go. Within one expiry day, plans
        holding more remaining quota per remaining day sort first (the
        subscription most at risk of going unused drains before it lapses).
        Known-bad pairs still sink behind healthy ones, and the sticky
        session channel stays pinned. The sort is stable, so equal ranks
        keep the console chain order.
        """
        if len(pairs) < 2:
            return pairs
        head = 1 if (sticky and pairs and pairs[0][0].name == sticky) else 0
        body = pairs[head:]
        rank = lambda pair: self.billing_ranks.get(pair[0].name, (3, 0.0, 0.0))
        healthy, bad = [], []
        for pair in body:
            if self.score(pair[0].name, pair[1]) <= DEMOTE_SCORE:
                bad.append(pair)
            else:
                healthy.append(pair)
        healthy.sort(key=rank)
        bad.sort(key=lambda pair: (-self.score(pair[0].name, pair[1]), rank(pair)))
        return pairs[:head] + healthy + bad

    # Weighted-strategy rank weights: stability score dominates, the 24h
    # success rate pulls toward its 0.9 prior, consecutive errors and the
    # latency EWMA subtract. Kept as module constants so tests can reason
    # about magnitudes without re-deriving them.
    RANK_SUCCESS_WEIGHT = 6.0
    RANK_ERROR_WEIGHT = 2.0
    RANK_ERROR_CAP = 5
    RANK_LATENCY_WEIGHT = 1.5
    RANK_LATENCY_CAP = 10.0
    RANK_SUCCESS_PRIOR = 0.9
    RANK_WARMUP_ATTEMPTS = 10

    def _success_rate(self, state: ChannelState) -> float:
        """24h window success rate in [0, 1], blended toward a 0.9 prior.

        The window counts attempts (req_times) vs successes with tokens
        (tok_events). Fewer than RANK_WARMUP_ATTEMPTS samples blend linearly
        toward the prior so a fresh channel is never punished into silence;
        an empty window falls back to lifetime counters, then to the prior.
        """
        attempts = len(state.req_times)
        successes = len(state.tok_events)
        if attempts <= 0:
            lifetime = state.served_requests + state.failed_requests
            if lifetime <= 0:
                return self.RANK_SUCCESS_PRIOR
            raw = state.served_requests / lifetime
            warm = self.RANK_WARMUP_ATTEMPTS
            return raw * (lifetime / warm) + self.RANK_SUCCESS_PRIOR * (1 - lifetime / warm)
        raw = min(successes, attempts) / attempts
        blend = min(attempts, self.RANK_WARMUP_ATTEMPTS) / self.RANK_WARMUP_ATTEMPTS
        return raw * blend + self.RANK_SUCCESS_PRIOR * (1 - blend)

    def _rank(self, channel: Channel, wire: str, now: float) -> float:
        state = self.states[channel.name]
        lat = min(max(state.latency_ewma, 0.0), self.RANK_LATENCY_CAP) / self.RANK_LATENCY_CAP
        return (self._decayed(f"{channel.name}|{wire}", now)
                + self.RANK_SUCCESS_WEIGHT * (self._success_rate(state) - self.RANK_SUCCESS_PRIOR)
                - self.RANK_ERROR_WEIGHT * min(state.consecutive_errors, self.RANK_ERROR_CAP)
                - self.RANK_LATENCY_WEIGHT * lat)

    def _weighted_order(self, pairs: list[tuple[Channel, str]], sticky: str | None,
                        now: float) -> list[tuple[Channel, str]]:
        """Rank pairs by _rank, highest first; equal ranks keep chain order.

        Sticky sessions stay pinned to the front exactly like _by_score.
        Python's sort is stable, so untouched channels keep their route
        order — a brand-new channel with no data lands where the console
        put it, it just no longer guarantees the front spot once a
        statistically better sibling exists.
        """
        if len(pairs) < 2:
            return pairs
        head = 1 if (sticky and pairs and pairs[0][0].name == sticky) else 0
        body = pairs[head:]
        body.sort(key=lambda pair: -self._rank(pair[0], pair[1], now))
        return pairs[:head] + body

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            return {
                "channels": {name: state.snapshot(now) for name, state in self.states.items()},
                "sticky_sessions": len(self.sessions),
            }
