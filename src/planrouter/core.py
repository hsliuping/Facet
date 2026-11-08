"""Channel configuration, quota/cooldown detection and channel health.

Pure standard library. A *channel* is one upstream endpoint (a coding plan, a
subscription bridge, or a free-tier API key). The router never converts between
wire protocols: a request on /v1/chat/completions only goes to openai channels,
a request on /v1/messages only goes to anthropic channels. Use a local bridge
(one-api / new-api / CLIProxyAPI) upstream if a plan speaks another protocol.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import ProxyHandler, build_opener, urlopen

from .quota import quota_view

OPENAI = "openai"
ANTHROPIC = "anthropic"
PROTOCOLS = (OPENAI, ANTHROPIC)
LOOPBACK = ("127.0.0.1", "localhost", "::1")

# Providers such as Volcengine Ark expose https://host/api/v3 (or
# /api/coding/v3) as the OpenAI-compatible base. Appending the usual
# /v1/chat/completions would produce /v3/v1/chat/completions and a 404.
VERSIONED_BASE = re.compile(r"/v\d+(?:beta)?$", re.IGNORECASE)


def upstream_path_for(base_url: str, upstream_path: str | None,
                      protocol: str) -> str:
    """Pick the upstream path without duplicating a version segment."""
    if upstream_path:
        return upstream_path
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

# Tools usually demand a concrete model id. These branded ids are ours: putting
# one into a tool's model field means "let the router schedule internally".
AUTO_MODEL = "auto"
DEFAULT_BRANDED_MODELS = {
    "facet-auto": "default",
    "facet-cheap": "cheap",
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
    # Optional outbound http(s) proxy for foreign upstreams that are not
    # reachable directly (e.g. "http://127.0.0.1:7890"); empty = direct.
    proxy: str = ""


@dataclass(frozen=True)
class Route:
    name: str
    protocol: str
    channels: tuple[str, ...]


@dataclass(frozen=True)
class Listen:
    host: str
    port: int
    api_key: str | None
    admin_key: str | None = None


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
    # 0 disables the free-quota alignment sweep (freealign.py)
    free_align_hours: int = 24
    # 404 (model removed upstream) cools this many hours with no half-open
    # probing; 0 keeps the old 30-second transient behaviour.
    not_found_cooldown_hours: int = 24
    # Daily-quota 429 without a reset header: 0 cools until the next UTC
    # midnight, >0 a fixed number of hours, -1 disables the special case.
    daily_429_cooldown_hours: int = 0
    # Same-channel retries for transient network errors before failing over.
    network_retries: int = 1


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
        raise ValueError("models policy must be a JSON object")
    branded = dict(DEFAULT_BRANDED_MODELS)
    extra = raw.get("branded") or {}
    if not isinstance(extra, dict):
        raise ValueError("models.branded must be an object")
    for name, hint in extra.items():
        if (not isinstance(name, str) or not name.strip()
                or not isinstance(hint, str) or not hint.strip()):
            raise ValueError("models.branded entries must be non-empty name -> route pairs")
        key = name.strip()
        if key in (AUTO_MODEL, "facet") or key.startswith(INTERNAL_MODEL_PREFIXES):
            raise ValueError(f"models.branded: {key!r} is a reserved internal model name")
        branded[key] = hint.strip()
    fallback_unknown = raw.get("fallback_unknown", True)
    if not isinstance(fallback_unknown, bool):
        raise ValueError("models.fallback_unknown must be true or false")
    fallback_route = raw.get("fallback_route") or "default"
    if not isinstance(fallback_route, str) or not fallback_route.strip():
        raise ValueError("models.fallback_route must be a route name")
    return ModelPolicy(branded, fallback_unknown, fallback_route.strip())


@dataclass(frozen=True)
class Config:
    listen: Listen
    defaults: Defaults
    channels: tuple[Channel, ...]
    routes: dict[str, Route]
    models: ModelPolicy = field(default_factory=ModelPolicy)

    def channel(self, name: str) -> Channel | None:
        return next((c for c in self.channels if c.name == name), None)

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
        return ModelDecision("auto", route_hint, AUTO_MODEL, requested,
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
        raise ValueError("config must be a JSON object")

    listen_raw = raw.get("listen") or {}
    listen = Listen(
        host=listen_raw.get("host", "127.0.0.1"),
        port=int(listen_raw.get("port", 8787)),
        api_key=resolve_secret(listen_raw.get("api_key")),
        admin_key=resolve_secret(listen_raw.get("admin_key")),
    )
    if listen.host not in LOOPBACK and not listen.api_key:
        raise ValueError("non-loopback listen requires listen.api_key (use env:VAR)")

    d = raw.get("defaults") or {}
    track_stream_usage = d.get("track_stream_usage", True)
    if not isinstance(track_stream_usage, bool):
        raise ValueError("defaults.track_stream_usage must be true or false")
    preempt_quota = d.get("preempt_quota", True)
    if not isinstance(preempt_quota, bool):
        raise ValueError("defaults.preempt_quota must be true or false")
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
        free_align_hours=int(d.get("free_align_hours", 24)),
        not_found_cooldown_hours=int(d.get("not_found_cooldown_hours", 24)),
        daily_429_cooldown_hours=int(d.get("daily_429_cooldown_hours", 0)),
        network_retries=int(d.get("network_retries", 1)),
    )

    channels: list[Channel] = []
    seen: set[str] = set()
    for item in raw.get("channels") or []:
        name = item.get("name")
        if not name or name in seen:
            raise ValueError(f"duplicate or missing channel name: {name!r}")
        seen.add(name)
        protocol = item.get("protocol", OPENAI)
        if protocol not in PROTOCOLS:
            raise ValueError(f"channel {name}: protocol must be one of {PROTOCOLS}")
        base_url = (item.get("base_url") or "").rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"channel {name}: base_url must be an http(s) URL")
        upstream_path = item.get("upstream_path")
        if upstream_path is not None:
            if (not isinstance(upstream_path, str) or not upstream_path.startswith("/")
                    or "://" in upstream_path):
                raise ValueError(
                    f"channel {name}: upstream_path must be a path starting with /"
                )
            upstream_path = upstream_path.rstrip("/") or "/"
        proxy = resolve_secret(str(item.get("proxy") or "").strip()) or ""
        if proxy and not proxy.lower().startswith(("http://", "https://")):
            raise ValueError(f"channel {name}: proxy must be an http(s):// URL")
        tier = str(item.get("tier") or "").strip().lower()
        if tier not in ("", "free", "paid"):
            raise ValueError(f'channel {name}: tier must be "", "free" or "paid"')
        tiers_raw = item.get("tiers") or {}
        if not isinstance(tiers_raw, dict):
            raise ValueError(f"channel {name}: tiers must be an object")
        tiers = {}
        for model_name, mark in tiers_raw.items():
            key = str(model_name).strip()
            mark = str(mark or "").strip().lower()
            if not key:
                raise ValueError(f"channel {name}: tiers keys must be model names")
            if mark not in ("", "free", "paid"):
                raise ValueError(
                    f'channel {name}: tiers values must be "", "free" or "paid"'
                )
            if mark:
                tiers[key] = mark
        models = item.get("models") or {}
        if not models:
            raise ValueError(f"channel {name}: models mapping is required")
        rate_raw = item.get("rate") or {}
        rate = Rate(
            rpm=rate_raw.get("rpm"), rpd=rate_raw.get("rpd"),
            tpm=rate_raw.get("tpm"), tpd=rate_raw.get("tpd"),
        )
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
            proxy=proxy,
        ))
    if not channels:
        raise ValueError("at least one channel is required")
    channels.sort(key=lambda c: (c.priority, c.name))

    routes: dict[str, Route] = {}
    for item in raw.get("routes") or []:
        name = item.get("name", "default")
        protocol = item.get("protocol", OPENAI)
        names = tuple(item.get("channels") or [])
        if protocol not in PROTOCOLS:
            raise ValueError(f"route {name}: protocol must be one of {PROTOCOLS}")
        if not names:
            raise ValueError(f"route {name}: channels list is empty")
        for channel_name in names:
            channel = next((c for c in channels if c.name == channel_name), None)
            if channel is None:
                raise ValueError(f"route {name}: unknown channel {channel_name!r}")
            if channel.protocol != protocol:
                raise ValueError(
                    f"route {name}: channel {channel_name!r} speaks {channel.protocol}, "
                    f"route speaks {protocol}. Protocols are never converted; "
                    f"put a local bridge upstream instead."
                )
        key = f"{protocol}:{name}"
        if key in routes:
            raise ValueError(f"duplicate route {key!r}")
        routes[key] = Route(name=name, protocol=protocol, channels=names)
    for protocol in PROTOCOLS:
        if f"{protocol}:default" not in routes:
            raise ValueError(f"missing {protocol}:default route")
    model_policy = parse_model_policy(raw.get("models"))
    declared = set().union(*(explicit_models(c) for c in channels))
    for name in model_policy.branded:
        if name in declared:
            raise ValueError(
                f"models.branded: {name!r} clashes with a channel's explicit model name"
            )
    return Config(
        listen=listen, defaults=defaults, channels=tuple(channels),
        routes=routes, models=model_policy,
    )


def load_config(path: str | Path) -> Config:
    return parse_config(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass
class ChannelState:
    name: str
    cooldown_until: float = 0.0
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

    def _prune(self, now: float) -> None:
        while self.req_times and now - self.req_times[0] > 86400:
            self.req_times.popleft()
        while self.tok_events and now - self.tok_events[0][0] > 86400:
            self.tok_events.popleft()

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

    def mark_cooldown(self, seconds: float, reason: str, now: float, half_open: bool = False) -> None:
        self.cooldown_until = now + seconds
        self.reason = reason
        self.consecutive_errors += 1
        self.failed_requests += 1
        self.half_open = half_open

    def mark_success(self, tokens: int, now: float) -> None:
        self.cooldown_until = 0.0
        self.reason = ""
        self.consecutive_errors = 0
        self.half_open = False
        self.served_requests += 1
        if tokens:
            self.tok_events.append((now, tokens))
            self.total_tokens += tokens

    def snapshot(self, now: float) -> dict:
        cooling = max(0.0, self.cooldown_until - now)
        return {
            "cooling": cooling > 0,
            "cooling_seconds": round(cooling, 1),
            "reason": self.reason if cooling > 0 else "",
            "consecutive_errors": self.consecutive_errors,
            "last_error": self.last_error[:300],
            "total_requests": self.total_requests,
            "served_requests": self.served_requests,
            "failed_requests": self.failed_requests,
            "total_tokens": self.total_tokens,
            "handoffs": self.handoffs,
            "last_used": self.last_used,
            "quota": quota_view(self.quota, now),
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
            return ordered
        head = self.config.channel(ordered[0])
        if head is None:
            return ordered
        positions = [i for i, name in enumerate(ordered)
                     if (c := self.config.channel(name)) is not None
                     and c.priority == head.priority]
        if len(positions) < 2:
            return ordered
        key = f"{protocol}:{route.name}"
        offset = self.rr_cursor.get(key, 0) % len(positions)
        self.rr_cursor[key] = offset + 1
        if not offset:
            return ordered
        tier = [ordered[i] for i in positions]
        rotated = tier[offset:] + tier[:offset]
        result = list(ordered)
        for pos, name in zip(positions, rotated):
            result[pos] = name
        return result

    def pick(
        self,
        protocol: str,
        hint: str,
        model: str,
        session_id: str | None,
        now: float | None = None,
        decision: ModelDecision | None = None,
        blocked: set[str] | frozenset[str] | None = None,
    ) -> tuple[list[tuple[Channel, str]], Route | None]:
        """Return ([(channel, wire_model), ...], route) in attempt order.

        `decision` comes from resolve_model. Exact matches get their own
        channels first; if every exact channel is cooling, the router's own
        schedule takes over so a quota window never breaks the calling tool.
        """
        now = time.time() if now is None else now
        excluded = set(blocked or ())
        with self.lock:
            if decision is None:
                decision = resolve_model(self.config, protocol, model, hint, excluded)
            if decision.mode == "rejected":
                return [], None
            route = self.config.route(protocol, decision.hint)
            if route is None:
                return [], None
            ordered = list(route.channels)
            sticky = self.session_channel(session_id, now)
            if sticky in ordered:
                ordered.remove(sticky)
                ordered.insert(0, sticky)
            else:
                ordered = self._rotate_tier(protocol, route, ordered)
            exact: list[tuple[Channel, str]] = []
            scheduled: list[tuple[Channel, str]] = []
            for name in ordered:
                channel = self.config.channel(name)
                if (channel is None or not channel.enabled or name in excluded
                        or channel.protocol != protocol):
                    continue
                ok, _ = self.states[name].available(now, channel.rate)
                if not ok:
                    continue
                if decision.mode == "exact" and name in decision.exact_channels:
                    wire = wire_model(channel, decision.exposed)
                    if wire:
                        exact.append((channel, wire))
                    continue
                wire = dispatch_wire_model(channel, decision.requested)
                if wire:
                    scheduled.append((channel, wire))
            if decision.mode == "exact":
                return (exact or scheduled), route
            return scheduled, route

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            return {
                "channels": {name: state.snapshot(now) for name, state in self.states.items()},
                "sticky_sessions": len(self.sessions),
            }
