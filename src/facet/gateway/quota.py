"""Read upstream quota headers so the router switches *before* a 429 arrives."""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

REQUEST_REMAINING = (
    "x-ratelimit-remaining-requests",
    "anthropic-ratelimit-unified-requests-remaining",
    "x-ratelimit-remaining",
)
TOKEN_REMAINING = (
    "x-ratelimit-remaining-tokens",
    "anthropic-ratelimit-unified-tokens-remaining",
)
REQUEST_LIMIT = (
    "x-ratelimit-limit-requests",
    "anthropic-ratelimit-unified-requests-limit",
    "x-ratelimit-limit",
)
TOKEN_LIMIT = (
    "x-ratelimit-limit-tokens",
    "anthropic-ratelimit-unified-tokens-limit",
)
RESET_HEADERS = (
    "x-ratelimit-reset-requests",
    "anthropic-ratelimit-unified-requests-reset",
    "x-ratelimit-reset-tokens",
    "anthropic-ratelimit-unified-tokens-reset",
    "x-ratelimit-reset",
)
DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?")


def _first(headers, names):
    for name in names:
        try:
            value = headers.get(name)
        except (AttributeError, TypeError):
            return None
        if value not in (None, ""):
            return value
    return None


def _number(value):
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _reset_at(value, now: float):
    if value is None:
        return None
    text = str(value).strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    except ValueError:
        pass
    seconds = _number(text)
    if seconds is not None and seconds >= 0:
        return now + seconds
    match = DURATION.fullmatch(text.lower())
    if match and any(match.groups()):
        hours, minutes, secs = (float(part or 0) for part in match.groups())
        return now + hours * 3600 + minutes * 60 + secs
    return None


def parse_quota(headers, now: float | None = None) -> dict | None:
    """Return the last observed quota window, or None when the upstream says nothing."""
    now = time.time() if now is None else now
    requests_remaining = _number(_first(headers, REQUEST_REMAINING))
    tokens_remaining = _number(_first(headers, TOKEN_REMAINING))
    if requests_remaining is None and tokens_remaining is None:
        return None
    return {
        "requests_remaining": requests_remaining,
        "tokens_remaining": tokens_remaining,
        "requests_limit": _number(_first(headers, REQUEST_LIMIT)),
        "tokens_limit": _number(_first(headers, TOKEN_LIMIT)),
        "reset_at": _reset_at(_first(headers, RESET_HEADERS), now),
        "observed_at": now,
    }


def quota_ratio(quota: dict | None) -> float | None:
    if not quota:
        return None
    ratios = []
    for remaining_key, limit_key in (("requests_remaining", "requests_limit"),
                                     ("tokens_remaining", "tokens_limit")):
        remaining, limit = quota.get(remaining_key), quota.get(limit_key)
        if remaining is not None and limit and limit > 0:
            ratios.append(max(0.0, min(1.0, remaining / limit)))
    return min(ratios) if ratios else None


def quota_view(quota: dict | None, now: float | None = None) -> dict:
    """Shape the observation for /health, /stats and the console."""
    if not quota:
        return {"known": False, "ratio": None, "exhausted": False}
    now = time.time() if now is None else now
    reset_at = quota.get("reset_at")
    if reset_at and reset_at <= now:
        return {"known": False, "ratio": None, "exhausted": False, "stale": True}
    remaining = [quota.get(key) for key in ("requests_remaining", "tokens_remaining")]
    return {
        "known": True,
        "requests_remaining": quota.get("requests_remaining"),
        "requests_limit": quota.get("requests_limit"),
        "tokens_remaining": quota.get("tokens_remaining"),
        "tokens_limit": quota.get("tokens_limit"),
        "ratio": quota_ratio(quota),
        "exhausted": any(value is not None and value <= 0 for value in remaining),
        "reset_in_seconds": (round(max(0.0, reset_at - now), 1)
                             if reset_at else None),
        "observed_at": quota.get("observed_at"),
    }


def quota_exhaustion_wait(quota: dict | None, now: float, default_seconds: float,
                          max_seconds: float) -> float | None:
    """How long to cool a channel whose upstream headers report an empty window.

    Without a reset header we only back off briefly: some proxies report the
    count *before* the request, so a missing reset must not lock a good plan
    for hours.
    """
    if not quota:
        return None
    remaining = [quota.get(key) for key in ("requests_remaining", "tokens_remaining")]
    observed = [value for value in remaining if value is not None]
    if not observed or min(observed) > 0:
        return None
    reset_at = quota.get("reset_at")
    seconds = (reset_at - now) if reset_at else min(default_seconds, 300.0)
    if seconds <= 0:
        return None
    return min(seconds, max_seconds)
