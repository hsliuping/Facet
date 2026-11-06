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

OPENAI = "openai"
ANTHROPIC = "anthropic"
PROTOCOLS = (OPENAI, ANTHROPIC)
LOOPBACK = ("127.0.0.1", "localhost", "::1")

# Substrings that mean "this channel is out of quota", not "the request is bad".
QUOTA_MARKERS = (
    "quota", "rate_limit", "rate limit", "usage limit", "usage_limit",
    "insufficient_quota", "too many requests", "credit balance", "billing",
    "overloaded", "capacity", "exceeded", "额度", "已用完", "限流", "冷却",
    "余额不足", "超出",
)


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
) -> tuple[bool, float, str]:
    """Return (is_quota_exhaustion, cooldown_seconds, reason).

    Quota exhaustion cools a channel for hours. Transient upstream errors only
    cool it briefly and allow a half-open probe on the next request.
    """
    now = time.time() if now is None else now
    text = body.decode("utf-8", "replace")[:4000] if isinstance(body, bytes) else str(body)[:4000]
    low = text.lower()
    marker = next((m for m in QUOTA_MARKERS if m in low), "")

    if status < 400:
        return False, 0.0, ""
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


@dataclass(frozen=True)
class Defaults:
    cooldown_minutes: int = 300
    max_failover: int = 3
    sticky_minutes: int = 30
    max_cooldown_hours: int = 168
    timeout: int = 120


@dataclass(frozen=True)
class Config:
    listen: Listen
    defaults: Defaults
    channels: tuple[Channel, ...]
    routes: dict[str, Route]

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


def parse_config(raw: dict) -> Config:
    """Validate an in-memory config dict (used by the editor's hot reload)."""
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")

    listen_raw = raw.get("listen") or {}
    listen = Listen(
        host=listen_raw.get("host", "127.0.0.1"),
        port=int(listen_raw.get("port", 8787)),
        api_key=resolve_secret(listen_raw.get("api_key")),
    )
    if listen.host not in LOOPBACK and not listen.api_key:
        raise ValueError("non-loopback listen requires listen.api_key (use env:VAR)")

    d = raw.get("defaults") or {}
    defaults = Defaults(
        cooldown_minutes=int(d.get("cooldown_minutes", 300)),
        max_failover=int(d.get("max_failover", 3)),
        sticky_minutes=int(d.get("sticky_minutes", 30)),
        max_cooldown_hours=int(d.get("max_cooldown_hours", 168)),
        timeout=int(d.get("timeout", 120)),
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
    return Config(listen=listen, defaults=defaults, channels=tuple(channels), routes=routes)


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
        }


class StateStore:
    """Thread-safe channel health, sticky sessions and candidate selection."""

    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.RLock()
        self.states = {c.name: ChannelState(c.name) for c in config.channels}
        self.sessions: dict[str, tuple[str, float]] = {}

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

    def pick(
        self,
        protocol: str,
        hint: str,
        model: str,
        session_id: str | None,
        now: float | None = None,
    ) -> tuple[list[tuple[Channel, str]], Route | None]:
        """Return ([(channel, wire_model), ...], route) in attempt order."""
        now = time.time() if now is None else now
        with self.lock:
            route = self.config.route(protocol, hint)
            if route is None:
                return [], None
            ordered = list(route.channels)
            sticky = self.session_channel(session_id, now)
            if sticky in ordered:
                ordered.remove(sticky)
                ordered.insert(0, sticky)
            result: list[tuple[Channel, str]] = []
            for name in ordered:
                channel = self.config.channel(name)
                if channel is None or not channel.enabled:
                    continue
                wire = wire_model(channel, model)
                if not wire:
                    continue
                ok, _ = self.states[name].available(now, channel.rate)
                if not ok:
                    continue
                result.append((channel, wire))
            return result, route

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            return {
                "channels": {name: state.snapshot(now) for name, state in self.states.items()},
                "sticky_sessions": len(self.sessions),
            }
