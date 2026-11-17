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


# Coding Plan usage query: Zhipu BigModel Coding Plan is a subscription that
# grants monthly credits (积分). The usage endpoint is under /api/biz/coding-plan/
# and returns consumption windows similar to other vendors' coding plans.
# NOTE: This endpoint structure is based on the observed Zhipu API patterns;
# verify against the actual response when testing with a real key.
CODING_PLAN_USAGE_URL = ("https://open.bigmodel.cn/api/biz/coding-plan/"
                         "query-usage")


def fetch_coding_plan(api_key: str, timeout: float = 30.0,
                      _urlopen=None) -> dict:
    """Zhipu Coding Plan usage; raises RuntimeError on any failure.
    
    Expected response shape (needs verification against actual API):
    {
        "code": 200,
        "success": true,
        "data": {
            "planType": "Lite",
            "status": "Running",
            "endTime": "2026-12-09T00:00:00+08:00",
            "autoRenew": false,
            "credits": {
                "total": 10000,
                "used": 2500,
                "remaining": 7500
            },
            "windows": [
                {
                    "level": "daily",
                    "quota": 500,
                    "used": 120,
                    "resetTime": 1794435200000
                },
                {
                    "level": "weekly",
                    "quota": 2500,
                    "used": 800,
                    "resetTime": 1794758400000
                }
            ]
        }
    }
    """
    req = urllib.request.Request(CODING_PLAN_USAGE_URL, headers={
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
    
    windows = []
    for w in data.get("windows") or []:
        if not isinstance(w, dict):
            continue
        level = str(w.get("level") or "").strip()
        if not level:
            continue
        entry = {"level": level}
        quota = _num(w.get("quota"))
        used = _num(w.get("used"))
        if quota is not None and used is not None:
            entry["quota"] = quota
            entry["used"] = used
            entry["percent"] = round(min(100.0, used * 100.0 / quota), 2) if quota else 0.0
        reset_ms = _num(w.get("resetTime"))
        if reset_ms is not None:
            entry["reset_ts"] = int(reset_ms / 1000)
        windows.append(entry)
    
    # Credits summary
    summary_parts = []
    credits = data.get("credits")
    if isinstance(credits, dict):
        total = _num(credits.get("total"))
        used = _num(credits.get("used"))
        remaining = _num(credits.get("remaining"))
        if remaining is not None:
            summary_parts.append(f"剩余积分 {int(remaining)}")
        elif total is not None and used is not None:
            summary_parts.append(f"已用积分 {int(used)} / {int(total)}")
    
    plan_type = str(data.get("planType") or "").strip()
    status = str(data.get("status") or "").strip()
    end_time = str(data.get("endTime") or "").strip()
    auto_renew = bool(data.get("autoRenew"))
    
    result = {
        "plan": f"智谱 Coding Plan{(' · ' + plan_type) if plan_type else ''}",
        "provider": "zhipu",
        "windows": windows,
    }
    if status:
        result["status"] = status
    if end_time:
        result["end_time"] = end_time
    if summary_parts:
        result["summary"] = " · ".join(summary_parts)
    if auto_renew:
        result["auto_renew"] = auto_renew
    return result
