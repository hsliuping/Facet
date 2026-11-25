"""facet-moonshot-quota: Kimi Open Platform account balance.

``GET https://api.moonshot.cn/v1/users/me/balance`` authenticates with the
same API key the channel already sends upstream and returns the three
balances the console shows (unit: CNY):

    {"code": 0, "status": true,
     "data": {"available_balance": 49.59, "voucher_balance": 46.59,
              "cash_balance": 3.00}}

``available_balance`` already sums cash + voucher; ``cash_balance`` may be
negative (arrears). The endpoint exposes no usage windows, reset times or
spend history — one summary line is the whole story. Inference fails with
``exceeded_current_quota_error`` once ``available_balance <= 0``.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BALANCE_URL = "https://api.moonshot.cn/v1/users/me/balance"


def _money(value) -> float | None:
    """Balance number -> 2-decimal float; malformed values are dropped."""
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def fetch_balance(api_key: str, timeout: float = 30.0,
                  _urlopen=None) -> dict:
    """Kimi account balance summary; raises RuntimeError on any failure."""
    req = urllib.request.Request(BALANCE_URL, headers={
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
    if not isinstance(payload, dict) or payload.get("code") != 0:
        message = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(message, dict):
            message = message.get("message")
        raise RuntimeError(f"余额查询失败：{str(message or payload)[:160]}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("响应缺少 data 字段")
    available = _money(data.get("available_balance"))
    if available is None:
        raise RuntimeError("响应缺少 available_balance")
    voucher = _money(data.get("voucher_balance"))
    cash = _money(data.get("cash_balance"))
    parts = [f"可用 ¥{available:.2f}"]
    if cash is not None:
        parts.append(f"现金 ¥{cash:.2f}" + ("（欠费）" if cash < 0 else ""))
    if voucher is not None:
        parts.append(f"代金券 ¥{voucher:.2f}")
    if available <= 0:
        parts.append("余额不足，调用将失败")
    return {
        "plan": "Kimi 开放平台",
        "provider": "moonshot",
        "windows": [],
        "summary": " · ".join(parts),
        # Structured money ledger (voucher round, design §8): the three
        # balances the console shows, kept machine-readable so the gateway
        # can persist an account-level snapshot and split 现金/代金券
        # columns instead of re-parsing the summary text.
        "balance": {
            "currency": "CNY",
            "available": available,
            "cash": cash,
            "voucher": voucher,
        },
    }
