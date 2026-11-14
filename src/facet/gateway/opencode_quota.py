"""facet-opencode-quota: Go plan usage from the OpenCode inference gateway.

``GET https://opencode.ai/inference/go/v1/usage`` authenticates with the
same ``oc_sk_`` Go key the channel already uses for chat calls and reports
percent-only windows (rolling ≈ 5 hours, weekly, monthly) with ISO-8601
reset stamps:

    {"usage": {"rolling": {"status": "ok", "percent": 1,
                           "resetsAt": "2026-11-14T06:32:08.000Z"}, ...}}

The console management plane (``/console/api/...``) answers 403 to Bearer
keys — it is session-cookie only — so this inference route is the only
readable usage source. Unlike the Volcengine plan calls no extra IAM
credentials are needed: a subscription bound to an OpenCode Go channel can
show vendor usage with the key it already carries.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

USAGE_URL = "https://opencode.ai/inference/go/v1/usage"
# (response key -> console window level); "rolling" is the ~5h bucket.
WINDOWS = (("rolling", "five_hour"), ("weekly", "weekly"),
           ("monthly", "monthly"))


def _epoch(stamp) -> int | None:
    """ISO-8601 UTC stamp -> epoch seconds, matching the Ark window shape."""
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def fetch_go_plan(api_key: str, timeout: float = 30.0,
                  _urlopen=None) -> dict:
    """Go subscription usage windows; raises RuntimeError on any failure.

    Shape mirrors ark_quota.fetch_*_plan (percent-only like the Coding
    Plan) plus a per-window ``status`` (ok / throttled / ...) that the
    console surfaces when the bucket is degraded.
    """
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        # Cloudflare fronts this host and rejects the default urllib UA.
        "User-Agent": "facet-gateway/1.0",
    })
    try:
        opener = _urlopen or urllib.request.urlopen
        with opener(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"HTTP {exc.code}：{detail}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"请求失败：{str(exc)[:200]}") from None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        raise RuntimeError("响应缺少 usage 字段")
    windows = []
    for key, level in WINDOWS:
        row = usage.get(key)
        if not isinstance(row, dict):
            continue
        try:
            percent = round(float(row.get("percent") or 0), 2)
        except (TypeError, ValueError):
            percent = None
        windows.append({
            "level": level,
            "percent": percent,
            "status": str(row.get("status") or ""),
            "reset_ts": _epoch(row.get("resetsAt")),
        })
    return {
        "plan": "OpenCode Go",
        "provider": "opencode",
        "windows": windows,
    }
