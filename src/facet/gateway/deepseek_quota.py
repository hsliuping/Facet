"""facet-deepseek-quota: pay-as-you-go balance for a DeepSeek key.

``GET https://api.deepseek.com/user/balance`` answers with the account
balance for the same key the channel sends upstream:

    {"is_available": false,
     "balance_infos": [{"currency": "CNY", "total_balance": "-0.23",
                        "granted_balance": "0.00", "topped_up_balance": "0.00"}]}

There is no usage window, so the snapshot carries a summary line instead of
windows (the console renders it in the same quota slot).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BALANCE_URL = "https://api.deepseek.com/user/balance"


def fetch_balance(api_key: str, timeout: float = 30.0, _urlopen=None) -> dict:
    """Account balance; raises RuntimeError on any failure."""
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
    if not isinstance(payload, dict):
        raise RuntimeError("响应不是 JSON 对象")
    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        raise RuntimeError("响应缺少 balance_infos 字段")
    parts = [f"{row.get('total_balance')} {row.get('currency') or ''}".strip()
             for row in infos if isinstance(row, dict) and row.get("total_balance")
             is not None]
    if not parts:
        raise RuntimeError("余额信息为空")
    available = bool(payload.get("is_available"))
    return {
        "plan": "DeepSeek",
        "provider": "deepseek",
        "windows": [],
        "summary": f"余额 {' · '.join(parts)}（{'可用' if available else '不可用'}）",
    }
