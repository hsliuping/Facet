"""facet-hunyuan-quota: pay-as-you-go wallet + Token Plan usage for Hunyuan.

Two credential planes, both signed with the shared TC3-HMAC-SHA256
plumbing from tokenhub_quota (same CAM SecretId/Key pair):

1) Token Plan (agent mode) — the TokenHub management plane
   ``DescribeTokenPlanPersonalPackage`` with ``ProductType
   "personal-hunyuan"`` (混元专用 Hy Token Plan; the common product type
   is ``personal`` for the 轻量版 plan). It reports the plan window:

    {"Response": {"PackageStatus": "ACTIVE", "AutoRenewFlag": 1,
                  "StartTime": "2026-04-05 21:06:09",
                  "ExpireTime": "2027-04-05 21:06:09",
                  "PrepayInquiryKey": "sp_xxx_standard",
                  "TotalUsed": 120, "TotalCredits": 9360,
                  "CycleCredits": 1560, "CurrentCycle": 1,
                  "TotalCycles": 6, "RemainCycles": 6}}

   ``TotalUsed`` is cumulative across cycles and ``TotalCredits`` the
   plan's purchased credits (thcli pairs them as used/total), so one
   monthly window carries the pair and the summary carries the cycle
   counters. Requires CAM read on tokenhub (e.g. QcloudHunyuanReadOnlyAccess).

2) Wallet — the billing plane ``DescribeAccountBalance`` on
   ``billing.tencentcloudapi.com`` (Version 2018-07-09, global, no
   region): the account wallet behind the pay-as-you-go channel:

    {"Response": {"Balance": 12345, "PresentAccountBalance": 500,
                  "OweAmount": 0, "FreezeAmount": 0}}

   All amounts are in cents (分); requires the CAM read on billing
   (e.g. QcloudBillingReadOnlyAccess).
"""
from __future__ import annotations

import json

from . import tokenhub_quota as tq

ID_ENV = "TENCENT_SECRET_ID"
KEY_ENV = "TENCENT_SECRET_KEY"
# Hy Token Plan rides the TokenHub plane (same service/version/region).
PLAN_ACTION = "DescribeTokenPlanPersonalPackage"
PLAN_HOST = "tokenhub.tencentcloudapi.com"
PLAN_SERVICE = "tokenhub"
PLAN_VERSION = "2026-03-22"
PLAN_REGION = "ap-guangzhou"
PRODUCT_TYPE = "personal-hunyuan"
# Payg wallet: account-wide balance on the billing plane (global service).
BILLING_ACTION = "DescribeAccountBalance"
BILLING_HOST = "billing.tencentcloudapi.com"
BILLING_SERVICE = "billing"
BILLING_VERSION = "2018-07-09"
# PrepayInquiryKey ends with the tier id (sp_xxx_lite / _standard / ...).
TIERS = ("lite", "standard", "pro", "max")
TIER_LABELS = {"lite": "Lite", "standard": "Standard",
               "pro": "Pro", "max": "Max"}
STATUS_LABELS = {"EXPIRED": "已过期", "UNPURCHASED": "未购买"}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_token_plan(secret_id: str, secret_key: str,
                     timeout: float = 30.0, _urlopen=None) -> dict:
    """混元 Token Plan (personal) usage; raises RuntimeError on failure."""
    response = tq._post(secret_id, secret_key, PLAN_ACTION,
                        {"ProductType": PRODUCT_TYPE}, PLAN_REGION, timeout,
                        _urlopen, host=PLAN_HOST, service=PLAN_SERVICE,
                        version=PLAN_VERSION)
    status = str(response.get("PackageStatus") or "").strip().upper()
    tier = str(response.get("PrepayInquiryKey") or "").rsplit("_", 1)[-1].lower()
    plan = "混元 Token Plan"
    if tier in TIER_LABELS:
        plan += f" · {TIER_LABELS[tier]}"
    windows = []
    total_used = _num(response.get("TotalUsed"))
    total_credits = _num(response.get("TotalCredits"))
    if total_credits and total_credits > 0 and total_used is not None:
        quota = total_credits
        windows.append({
            "level": "monthly",
            "quota": quota,
            "used": total_used,
            "percent": round(min(100.0, max(0.0, total_used * 100.0 / quota)), 2),
        })
    parts = []
    cycle_credits = _num(response.get("CycleCredits"))
    if cycle_credits and cycle_credits > 0:
        parts.append(f"本周期额度 {cycle_credits:g} 积分")
    current, total_cycles = (_num(response.get("CurrentCycle")),
                             _num(response.get("TotalCycles")))
    if current and total_cycles:
        parts.append(f"第 {int(current)}/{int(total_cycles)} 周期")
    remain = _num(response.get("RemainCycles"))
    if remain is not None:
        parts.append(f"剩 {int(remain)} 周期")
    if status in STATUS_LABELS:
        parts.insert(0, STATUS_LABELS[status])
    snapshot = {"plan": plan, "provider": "hunyuan", "windows": windows}
    if parts:
        snapshot["summary"] = " · ".join(parts)
    end_time = str(response.get("ExpireTime") or "").strip()[:10]
    if end_time:
        snapshot["end_time"] = end_time
    auto_renew = response.get("AutoRenewFlag")
    if auto_renew is not None:
        snapshot["auto_renew"] = auto_renew in (1, "1", True, "true", "True")
    return snapshot


def fetch_balance(secret_id: str, secret_key: str,
                  timeout: float = 30.0, _urlopen=None) -> dict:
    """腾讯云账户余额（分）; raises RuntimeError on any failure."""
    response = tq._post(secret_id, secret_key, BILLING_ACTION, {}, "",
                        timeout, _urlopen, host=BILLING_HOST,
                        service=BILLING_SERVICE, version=BILLING_VERSION)
    balance = _num(response.get("Balance"))
    if balance is None:
        balance = _num(response.get("RealBalance"))
    if balance is None:
        raise RuntimeError("响应缺少余额字段")
    parts = [f"余额 ¥{balance / 100.0:.2f}"]
    present = _num(response.get("PresentAccountBalance"))
    if present and present > 0:
        parts.append(f"代金券 ¥{present / 100.0:.2f}")
    owe = _num(response.get("OweAmount"))
    if owe and owe > 0:
        parts.append(f"欠费 ¥{owe / 100.0:.2f}")
    freeze = _num(response.get("FreezeAmount"))
    if freeze and freeze > 0:
        parts.append(f"冻结 ¥{freeze / 100.0:.2f}")
    return {
        "plan": "腾讯云账户余额",
        "provider": "hunyuan",
        "windows": [],
        "summary": " · ".join(parts),
    }
