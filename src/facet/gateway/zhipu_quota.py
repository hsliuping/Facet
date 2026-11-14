"""facet-zhipu-quota: pay-as-you-go wallet for a Zhipu BigModel key.

``GET https://open.bigmodel.cn/api/biz/account/query-customer-account-report``
authenticates with the same key the channel sends upstream and reports the
account wallet:

    {"code": 200, "success": true,
     "data": {"balance": 9.978, "availableBalance": 9.978,
              "rechargeAmount": 10.0, "totalSpendAmount": 0.0219,
              "frozenBalance": 0}}

Zhipu keeps console routes under /api/biz/*; this one is open to API keys
(account settings and customer info are not). There is no usage window, so
the snapshot carries a summary line instead of windows.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

REPORT_URL = ("https://open.bigmodel.cn/api/biz/account/"
              "query-customer-account-report")


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_account_report(api_key: str, timeout: float = 30.0,
                         _urlopen=None) -> dict:
    """Account wallet; raises RuntimeError on any failure."""
    req = urllib.request.Request(REPORT_URL, headers={
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
    if payload.get("code") != 200 or not payload.get("success", True):
        raise RuntimeError(str(payload.get("msg") or "响应未成功")[:160])
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("响应缺少 data 字段")
    balance = _num(data.get("availableBalance"))
    if balance is None:
        balance = _num(data.get("balance"))
    if balance is None:
        raise RuntimeError("响应缺少余额字段")
    parts = [f"余额 ¥{balance:.2f}"]
    recharge = _num(data.get("rechargeAmount"))
    if recharge is not None:
        parts.append(f"充值 ¥{recharge:.2f}")
    spend = _num(data.get("totalSpendAmount"))
    if spend is not None:
        parts.append(f"累计消费 ¥{spend:.2f}")
    frozen = _num(data.get("frozenBalance"))
    if frozen:
        parts.append(f"冻结 ¥{frozen:.2f}")
    return {
        "plan": "智谱 BigModel",
        "provider": "zhipu",
        "windows": [],
        "summary": " · ".join(parts),
    }
