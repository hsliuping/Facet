"""facet-minimax-quota: MiniMax (China) channel usage.

Two credential families share one key shape, so the fetch is dispatched on
the prefix MiniMax's own CLI uses:

* ``sk-api-…`` (pay-as-you-go) → account balance,
  ``GET https://api.minimax.cn/account/query_balance`` with decimal
  **strings**: ``available_amount`` / ``cash_balance`` / ``voucher_balance``
  / ``credit_balance`` / ``owed_amount`` (CNY).
* ``sk-cp-…`` (Token Plan subscription) or any other key → quota windows,
  ``GET https://api.minimax.cn/v1/token_plan/remains``.

Token Plan rows (``model_remains``) report *remaining* percent per model
bucket; the first row carries the canonical account state. Each row mixes
two windows:

    5h:  current_interval_remaining_percent, start_time/end_time (ms)
    7d:  current_weekly_remaining_percent × weekly_boost_permille/1000,
         weekly_start_time/weekly_end_time (ms)

``*_usage_count`` has been observed on both sides across upstream versions,
so the percent arbitrates: the count is read as *used* only when
``count/total`` lands closer to the used percent than to the remaining one.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE_URL = "https://api.minimax.cn"
TOKEN_PLAN_URL = BASE_URL + "/v1/token_plan/remains"
BALANCE_URL = BASE_URL + "/account/query_balance"
# MiniMax CLI convention: pay-as-you-go keys are prefixed, plan keys are not.
PAYG_PREFIX = "sk-api-"


def _request(url: str, api_key: str, timeout: float, _urlopen=None) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "facet-gateway/1.0",
    })
    try:
        opener = _urlopen or urllib.request.urlopen
        with opener(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"HTTP {exc.code}：{detail}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"请求失败：{str(exc)[:200]}") from None


def _check(payload) -> dict:
    """Validate the shared ``base_resp`` envelope; return the payload."""
    if not isinstance(payload, dict):
        raise RuntimeError("响应不是 JSON 对象")
    base = payload.get("base_resp")
    code = base.get("status_code") if isinstance(base, dict) else None
    if code != 0:
        message = base.get("status_msg") if isinstance(base, dict) else None
        raise RuntimeError(f"MiniMax 返回错误：{str(message or payload)[:160]}")
    return payload


def _number(value) -> float | None:
    if value is None:
        return None
    try:
        return round(float(str(value).strip()), 2)
    except (TypeError, ValueError):
        return None


def _count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _epoch_seconds(value) -> int | None:
    """Millisecond stamp -> epoch seconds (the console's window shape)."""
    if isinstance(value, bool):
        return None
    try:
        stamp = int(value) // 1000
    except (TypeError, ValueError):
        return None
    return stamp if stamp > 0 else None


def _resolve_counts(total, count, remaining_percent):
    """(used, quota) when the count semantics can be pinned down, else None."""
    total = _count(total)
    count = _count(count)
    if not total or count is None or count > total:
        return None
    if remaining_percent is None:
        return count, total
    ratio = count * 100.0 / total
    as_used = abs(ratio - (100.0 - remaining_percent))
    as_remaining = abs(ratio - remaining_percent)
    if min(as_used, as_remaining) > 1.0:
        return None
    if as_remaining < as_used:
        return total - count, total
    return count, total


def _window(level: str, total, count, remaining_percent, status, reset_ts,
            boost: float = 1000.0) -> dict | None:
    """One percent/absolute window; status 1=normal 2=exhausted 3=unlimited."""
    if status == 3:
        return {"level": level, "text": "不限额", "status": "unlimited"}
    window = {"level": level, "percent": None, "reset_ts": reset_ts}
    if remaining_percent is not None:
        remaining = remaining_percent * (boost / 1000.0)
        window["percent"] = round(min(100.0, max(0.0, 100.0 - remaining)), 2)
    if status == 2:
        window["percent"] = 100.0
        window["status"] = "exhausted"
    counts = _resolve_counts(total, count, remaining_percent)
    if counts:
        window["used"], window["quota"] = counts
    if window["percent"] is None and "used" not in window:
        return None
    return window


def fetch_usage(api_key: str, timeout: float = 30.0,
                _urlopen=None) -> dict:
    """MiniMax channel usage; dispatches on the key family."""
    key = str(api_key or "")
    if key.startswith(PAYG_PREFIX):
        return _fetch_balance(key, timeout=timeout, _urlopen=_urlopen)
    return _fetch_token_plan(key, timeout=timeout, _urlopen=_urlopen)


def _fetch_token_plan(api_key: str, timeout: float = 30.0,
                      _urlopen=None) -> dict:
    payload = _check(_request(TOKEN_PLAN_URL, api_key, timeout, _urlopen))
    rows = payload.get("model_remains")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Token Plan 响应缺少 model_remains")
    row = rows[0] if isinstance(rows[0], dict) else {}
    boost = _number(row.get("weekly_boost_permille")) or 1000.0
    if boost <= 0:
        boost = 1000.0
    windows = [
        _window(
            "five_hour",
            row.get("current_interval_total_count"),
            row.get("current_interval_usage_count"),
            _number(row.get("current_interval_remaining_percent")),
            row.get("current_interval_status"),
            _epoch_seconds(row.get("end_time")),
        ),
        _window(
            "weekly",
            row.get("current_weekly_total_count"),
            row.get("current_weekly_usage_count"),
            _number(row.get("current_weekly_remaining_percent")),
            row.get("current_weekly_status"),
            _epoch_seconds(row.get("weekly_end_time")),
            boost,
        ),
    ]
    return {
        "plan": "MiniMax Token Plan",
        "provider": "minimax",
        "windows": [w for w in windows if w is not None],
    }


def _fetch_balance(api_key: str, timeout: float = 30.0,
                   _urlopen=None) -> dict:
    payload = _check(_request(BALANCE_URL, api_key, timeout, _urlopen))
    available = _number(payload.get("available_amount"))
    if available is None:
        raise RuntimeError("余额响应缺少 available_amount")
    parts = [f"可用 ¥{available:.2f}"]
    for field, label in (("cash_balance", "现金"),
                         ("voucher_balance", "代金券"),
                         ("credit_balance", "信用"),
                         ("owed_amount", "欠费")):
        value = _number(payload.get(field))
        if value:
            parts.append(f"{label} ¥{value:.2f}")
    if available <= 0:
        parts.append("余额不足，调用将失败")
    return {
        "plan": "MiniMax 按量付费",
        "provider": "minimax",
        "windows": [],
        "summary": " · ".join(parts),
    }
