"""facet-bailian-quota: pay-as-you-go wallet + Token Plan usage for Bailian.

Two separate credential planes:

1) Wallet — the BSS open API ``QueryAccountBalance`` on
   ``business.aliyuncs.com`` with the RAM AccessKey pair (Aliyun RPC
   HMAC-SHA1 signature, Version 2017-12-14). The account-wide wallet:

    {"Code": "Success", "Data": "{\\"AvailableAmount\\": \\"123.45\\",
     \\"AvailableCashAmount\\": \\"100.00\\", \\"CreditAmount\\": 50,
     \\"Currency\\": \\"CNY\\"}"}

   ``Data`` arrives as a JSON string (older responses) or an object;
   both are accepted. Requires the RAM user to hold
   ``bss:DescribeAcccount`` (AliyunBSSReadOnlyAccess).

2) Token Plan (agent mode) — the official ``bl`` CLI
   (``npm i -g bailian-cli``): ``bl usage token-plan --output json``
   prints the flat percentage windows as decimals:

    {"per5HourPercentage": 0.25, "per5HourResetTime": 1790000000000,
     "per1MonthPercentage": 0.5, "per1MonthResetTime": 1790000000000,
     "hasQuotaData": true}

   That command authenticates with the console session
   (``bl auth login --console``), NOT the RAM key, so the fetcher takes
   no credentials — it only reads this host's login. Without the CLI
   installed or logged in the error text tells the user what to run.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from .freealign import _resolve_cmd

BSS_ENDPOINT = "https://business.aliyuncs.com"
BSS_ACTION = "QueryAccountBalance"
BSS_VERSION = "2017-12-14"
AK_ENV = "BAILIAN_ACCESS_KEY_ID"
SK_ENV = "BAILIAN_ACCESS_KEY_SECRET"
TOKEN_PLAN_CMD = ("bl", "usage", "token-plan", "--output", "json")
TOKEN_PLAN_TIMEOUT = 60.0
# bl CLI window names -> facet console levels (vendorUsageHtml level map).
WINDOW_LEVELS = {
    (5, "Hour"): "five_hour",
    (1, "Day"): "daily",
    (7, "Day"): "weekly",
    (1, "Month"): "monthly",
}
WINDOW_RE = re.compile(r"^per(\d+)(Hour|Day|Week|Month)Percentage$")
# Rendering order matches the console's window ordering.
LEVEL_ORDER = ("five_hour", "daily", "weekly", "monthly", "session", "budget")


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pe(value) -> str:
    """Aliyun RPC percent-encoding (UTF-8, space -> %20, no '+')."""
    return urllib.parse.quote(str(value), safe="~")


def _sign_rpc(params: dict, secret: str) -> str:
    """Aliyun RPC signature (HMAC-SHA1 over the sorted query string)."""
    canonical = "&".join(f"{_pe(k)}={_pe(v)}" for k, v in sorted(params.items()))
    string_to_sign = "GET&" + _pe("/") + "&" + _pe(canonical)
    digest = hmac.new((secret + "&").encode("utf-8"),
                      string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def fetch_balance(access_key_id: str, access_key_secret: str,
                  timeout: float = 30.0, _urlopen=None) -> dict:
    """Aliyun account wallet; raises RuntimeError on any failure."""
    params = {
        "AccessKeyId": access_key_id,
        "Action": BSS_ACTION,
        "Format": "JSON",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": BSS_VERSION,
    }
    params["Signature"] = _sign_rpc(params, access_key_secret)
    url = BSS_ENDPOINT + "?" + "&".join(
        f"{_pe(k)}={_pe(v)}" for k, v in sorted(params.items()))
    req = urllib.request.Request(url, headers={
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
    code = str(payload.get("Code") or "")
    if code and code != "Success":
        raise RuntimeError(str(payload.get("Message") or f"BSS 返回 {code}")[:160])
    data = payload.get("Data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if not isinstance(data, dict):
        raise RuntimeError("响应缺少余额数据")
    balance = _num(data.get("AvailableAmount"))
    if balance is None:
        balance = _num(data.get("AvailableCashAmount"))
    if balance is None:
        raise RuntimeError("响应缺少余额字段")
    currency = str(data.get("Currency") or "CNY").strip().upper()
    symbol = "¥" if currency in ("CNY", "") else f"{currency} "
    parts = [f"可用余额 {symbol}{balance:.2f}"]
    cash = _num(data.get("AvailableCashAmount"))
    if cash is not None and abs(cash - balance) > 0.005:
        parts.append(f"现金 {symbol}{cash:.2f}")
    credit = _num(data.get("CreditAmount"))
    if credit and credit > 0:
        parts.append(f"信用额度 {symbol}{credit:.2f}")
    return {
        "plan": "阿里云百炼",
        "provider": "bailian",
        "windows": [],
        "summary": " · ".join(parts),
    }


def _cli_error(payload: dict) -> str | None:
    """The bl CLI's ``{"error": {message, hint}}`` shape, or None."""
    err = payload.get("error")
    if not isinstance(err, dict):
        return None
    msg = str(err.get("message") or "").strip()
    hint = str(err.get("hint") or "").strip()
    if hint:
        return f"{msg}（{hint}）"[:240]
    if msg:
        return msg[:240]
    return f"bl CLI 错误码 {err.get('code')}"


def fetch_token_plan(timeout: float = TOKEN_PLAN_TIMEOUT,
                     _run=None) -> dict:
    """百炼 Token Plan (agent) usage via the bl CLI; raises RuntimeError.

    The CLI is a per-host console session, not a per-channel key: the
    snapshot is shared by every bailian agent channel of this deployment.
    """
    runner = _run or subprocess.run
    try:
        proc = runner(_resolve_cmd(list(TOKEN_PLAN_CMD)),
                      capture_output=True, timeout=timeout,
                      encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise RuntimeError(
            "未找到 bl CLI（bailian-cli）：请先 npm i -g bailian-cli，"
            "再执行 bl auth login --console") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"bl CLI 超时（>{int(timeout)}s）") from None
    out = (proc.stdout or "").strip()
    payload = None
    for text in (out, (proc.stderr or "").strip()):
        if not text:
            continue
        try:
            candidate = json.loads(text)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None:
        raise RuntimeError(
            f"bl usage token-plan 失败（退出码 {proc.returncode}）："
            f"{(proc.stderr or out or '').strip()[:200]}")
    error = _cli_error(payload)
    if error:
        raise RuntimeError(error)
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    windows = []
    for key, value in data.items():
        match = WINDOW_RE.match(key)
        if not match or _num(value) is None:
            continue
        count, unit = int(match.group(1)), match.group(2)
        level = WINDOW_LEVELS.get((count, unit), f"{count}{unit.lower()}")
        pct = min(100.0, max(0.0, float(value) * 100.0))
        entry = {"level": level, "percent": round(pct, 2)}
        reset_ms = _num(data.get(f"per{count}{unit}ResetTime"))
        if reset_ms is not None:
            entry["reset_ts"] = int(reset_ms / 1000)
        windows.append(entry)
    windows.sort(key=lambda w: (LEVEL_ORDER.index(w["level"])
                                if w["level"] in LEVEL_ORDER else len(LEVEL_ORDER)))
    snapshot = {"plan": "百炼 Token Plan", "provider": "bailian",
                "windows": windows}
    if not windows:
        if data.get("hasQuotaData") is False:
            snapshot["summary"] = "该账号未开通 Token Plan 套餐"
        else:
            snapshot["summary"] = "套餐用量数据为空"
    return snapshot
