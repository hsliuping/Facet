"""facet-openrouter-quota: pay-as-you-go usage for an OpenRouter key.

``GET https://openrouter.ai/api/v1/key`` authenticates with the same key the
free-tier channel already sends upstream and reports the key's spend plus the
free-model request allowance:

    {"data": {"usage": 0, "limit": 50, "limit_remaining": 50,
              "free_model_daily_requests": {"used": 18, "limit": 1000,
                                            "remaining": 982}}}

This is a per-key budget rather than a monthly plan, so the windows carry a
pre-formatted ``text`` line (daily free requests, key spend limit) that the
channel card renders verbatim.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

KEY_URL = "https://openrouter.ai/api/v1/key"


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_key_usage(api_key: str, timeout: float = 30.0,
                    _urlopen=None) -> dict:
    """Key usage windows; raises RuntimeError on any failure."""
    req = urllib.request.Request(KEY_URL, headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
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
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("响应缺少 data 字段")
    windows = []
    daily = data.get("free_model_daily_requests")
    if isinstance(daily, dict):
        used, cap = _num(daily.get("used")) or 0.0, _num(daily.get("limit"))
        if cap:
            windows.append({
                "level": "daily",
                "text": f"免费模型今日 {int(used)} / {int(cap)} 次",
                "percent": round(used / cap * 100, 2),
                "reset_ts": None,
                "status": "ok" if used < cap else "exhausted",
            })
    spend, limit = _num(data.get("usage")), _num(data.get("limit"))
    if limit:
        remaining = _num(data.get("limit_remaining"))
        if remaining is None:
            remaining = max(0.0, limit - (spend or 0.0))
        windows.append({
            "level": "budget",
            "text": (f"Key 消费 ${spend or 0:.2f} / ${limit:.2f}"
                     f"（剩 ${remaining:.2f}）"),
            "percent": round((spend or 0.0) / limit * 100, 2),
            "reset_ts": None,
            "status": "ok" if remaining > 0 else "exhausted",
        })
    summary = ""
    if spend is not None and not limit:
        summary = f"已消费 ${spend:.2f}（无消费上限）"
    return {
        "plan": "OpenRouter",
        "provider": "openrouter",
        "windows": windows,
        "summary": summary,
    }
