"""facet-ark-quota: report Volcengine Ark free quota as freealign JSON rows.

The Ark management plane (``ark.cn-beijing.volcengineapi.com``) exposes
per-model free usage through the ``ListModelActivations`` action — the same
quota state the console shows as 免费推理额度. It authenticates with IAM
AK/SK under the Volcengine V4 signature scheme (the ``ark-...`` data-plane
keys used for chat calls cannot read it), so this CLI wraps the signing,
paging and row-mapping into a stdlib-only command:

    facet-ark-quota --output json

stdout is a bare JSON array of ``{"model", "type": "Text", "remaining",
"expires"}`` rows — the contract ``planrouter.freealign`` consumes to keep
free-tier channel mappings in sync with the collaborative-reward quota
(协作奖励计划：调用授权模型，次日按量返还 30 天有效的免费资源包).

Credentials are read from ``VOLC_ACCESSKEY`` / ``VOLC_SECRETKEY`` in the
environment. The API reports no expiry date for reward packs, so rows carry
a synthesized ``expires`` of today + 30 days (the reward cycle length); the
real gate is ``remaining > 0`` re-checked on every free-align sweep.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta, timezone

# Management-plane endpoint; Action/Version travel in the query string.
ENDPOINT = "https://ark.cn-beijing.volcengineapi.com"
HOST = "ark.cn-beijing.volcengineapi.com"
REGION = "cn-beijing"
SERVICE = "ark"
ACTION = "ListModelActivations"
VERSION = "2024-01-01"
PAGE_SIZE = 100
# Guard against a server that keeps returning full pages forever.
MAX_PAGES = 50
# Reward packs are valid for 30 days but the API exposes no dates; the sweep
# refreshes daily, so a synthesized horizon is only cosmetic.
EXPIRE_DAYS = 30
SIGNED_HEADERS = "host;x-content-sha256;x-date"

AK_ENV = "VOLC_ACCESSKEY"
SK_ENV = "VOLC_SECRETKEY"


def canonical_query(params: dict) -> str:
    """URI-encoded, key-sorted query string per the V4 canonical form."""
    pairs = sorted((str(k), str(v)) for k, v in params.items())
    return "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in pairs)


def sign_request(ak: str, sk: str, body: str, x_date: str,
                 query: str) -> str:
    """Authorization header for one request, per Volcengine V4 signing.

    ``x_date`` is the UTC ``YYYYMMDDTHHMMSSZ`` stamp sent as ``X-Date``; the
    credential scope and signing key derive its first 8 digits. Only the
    headers we actually send (host, x-content-sha256, x-date) are signed.
    """
    hashed_body = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical_headers = (
        f"host:{HOST}\n"
        f"x-content-sha256:{hashed_body}\n"
        f"x-date:{x_date}\n")
    canonical_request = "\n".join([
        "POST", "/", query, canonical_headers, SIGNED_HEADERS, hashed_body])
    scope = f"{x_date[:8]}/{REGION}/{SERVICE}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256", x_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    key = hmac.new(sk.encode("utf-8"), x_date[:8].encode("utf-8"),
                   hashlib.sha256).digest()
    for part in (REGION, SERVICE, "request"):
        key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return (f"HMAC-SHA256 Credential={ak}/{scope}, "
            f"SignedHeaders={SIGNED_HEADERS}, Signature={signature}")


def utc_stamp() -> str:
    """UTC ``YYYYMMDDTHHMMSSZ`` stamp used as the signed X-Date header."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _post(ak: str, sk: str, action: str, body_obj: dict,
          timeout: float = 30.0, _urlopen=None) -> dict:
    """One signed management-plane call; raises RuntimeError on any failure."""
    body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
    query = canonical_query({"Action": action, "Version": VERSION})
    x_date = utc_stamp()
    headers = {
        "Host": HOST,
        "X-Date": x_date,
        "X-Content-Sha256": hashlib.sha256(
            body.encode("utf-8")).hexdigest(),
        "Content-Type": "application/json",
        "Authorization": sign_request(ak, sk, body, x_date, query),
    }
    req = urllib.request.Request(
        f"{ENDPOINT}/?{query}", data=body.encode("utf-8"),
        headers=headers, method="POST")
    try:
        opener = _urlopen or urllib.request.urlopen
        with opener(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"HTTP {exc.code}：{detail}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"请求失败：{str(exc)[:200]}") from None
    error = (payload.get("ResponseMetadata") or {}).get("Error")
    if error:
        raise RuntimeError(
            f"API 错误 {error.get('Code')}: {error.get('Message')}")
    return payload


def fetch_page(ak: str, sk: str, page: int, timeout: float = 30.0,
               _urlopen=None) -> dict:
    """One ListModelActivations call with free-usage fields attached."""
    return _post(ak, sk, ACTION,
                 {"WithFreeUsage": True, "PageNumber": page,
                  "PageSize": PAGE_SIZE}, timeout, _urlopen)


def fetch_coding_plan(ak: str, sk: str, timeout: float = 30.0,
                      _urlopen=None) -> dict:
    """Coding Plan subscription info + per-window usage for the subscriptions page.

    GetPersonalPlan(Plan=CodingPlan) carries the tier/status/dates; the
    windows come from GetCodingPlanUsage, which reports Percent-only
    (session/weekly/monthly, cap 100) plus an epoch-seconds ResetTimestamp
    (-1 = no reset scheduled). Raises RuntimeError when either call fails —
    the caller shows the message instead of inventing numbers.
    """
    info = (_post(ak, sk, "GetPersonalPlan", {"Plan": "CodingPlan"},
                  timeout, _urlopen).get("Result") or {})
    usage = (_post(ak, sk, "GetCodingPlanUsage", {}, timeout,
                   _urlopen).get("Result") or {})
    windows = []
    for row in usage.get("QuotaUsage") or []:
        if not isinstance(row, dict):
            continue
        try:
            percent = round(float(row.get("Percent") or 0), 2)
        except (TypeError, ValueError):
            percent = None
        windows.append({
            "level": str(row.get("Level") or row.get("level") or ""),
            "percent": percent,
            "reset_ts": row.get("ResetTimestamp") if isinstance(
                row.get("ResetTimestamp"), int) else None,
        })
    return {
        "plan": "CodingPlan",
        "plan_type": str(info.get("PlanType") or ""),
        "status": str(info.get("Status") or ""),
        "start_time": str(info.get("StartTime") or ""),
        "end_time": str(info.get("EndTime") or ""),
        "auto_renew": bool(info.get("AutoRenew")),
        "windows": windows,
    }


def extract_items(payload: dict) -> list:
    """The Items array of a ListModelActivations response."""
    items = payload.get("Items")
    if items is None and isinstance(payload.get("Result"), dict):
        items = payload["Result"].get("Items")
    if not isinstance(items, list):
        raise RuntimeError("响应缺少 Items 数组")
    return items


def rows_from_items(items: list, today: str | None = None) -> list[dict]:
    """Map API items to freealign rows for every activated chat model.

    Available models only; remaining is the better of the initial free
    grant (Total − Consumed) and the sum of FreeInference resource packs
    (Total − Consumed − Reclaimed, never below zero). DataPermission packs
    grant no tokens. Rows keep the grant they were measured against
    (``total`` / ``consumed``) and are emitted even at zero remaining, so the
    console can show "quota used up" instead of a model that silently
    disappears; the sweep still filters zero rows out of the mapping.
    """
    horizon = (date.today() + timedelta(days=EXPIRE_DAYS)).isoformat() \
        if today is None else today
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model = str(item.get("FoundationModelName") or "").strip()
        if not model or item.get("State") != "Available":
            continue
        remaining = 0
        total = 0
        initial = item.get("InitialInferenceFreeUsage")
        if isinstance(initial, dict):
            total = _int(initial.get("Total"))
            remaining = max(0, total - _int(initial.get("Consumed")))
        for pack in item.get("FreeResourcePackItems") or []:
            if not isinstance(pack, dict) or pack.get("Type") != "FreeInference":
                continue
            pack_total = _int(pack.get("Total"))
            pack_consumed = _int(pack.get("Consumed")) + _int(pack.get("Reclaimed"))
            pack_left = max(0, pack_total - pack_consumed)
            if pack_left > remaining:
                remaining, total = pack_left, pack_total
        if total <= 0:
            # No grant to report against: stay out of the sweep (no false
            # "exhausted" verdict for a model whose quota is simply unknown).
            continue
        out.append({"model": model, "type": "Text",
                    "remaining": remaining, "total": total,
                    "consumed": total - remaining, "expires": horizon})
    return out


def resolve_callable_names(families: list[str],
                           callables: list[str]) -> list[str]:
    """Map management-plane family names to data-plane callable ids.

    ``ListModelActivations`` reports a per-family activation name
    (``FoundationModelName``, e.g. ``kimi-k3-1-pro``) while the data-plane
    ``/models`` route lists the concrete id a chat call must use — for
    third-party models that is the family name plus a version-date suffix
    (``kimi-k3-1-pro-261024``). A family present verbatim in the callable
    list (the doubao family) is kept as-is; otherwise the latest id carrying
    a pure-numeric ``-suffix`` is chosen. This numeric rule keeps families
    that share a prefix distinct (``deepseek-v4-1-flash`` never matches
    ``deepseek-v4-1-flash-economy-261017``). Families resolve to at most one
    name each, in family order; unmatched families are dropped.
    """
    callable_set = {str(m).strip() for m in callables if str(m).strip()}
    out: list[str] = []
    seen: set[str] = set()
    for family in families:
        family = str(family).strip()
        if not family:
            continue
        if family in callable_set:
            pick = family
        else:
            candidates = sorted(
                m for m in callable_set
                if m.startswith(family + "-")
                and m[len(family) + 1:].isdigit())
            if not candidates:
                continue
            pick = candidates[-1]  # latest version-date suffix
        if pick not in seen:
            seen.add(pick)
            out.append(pick)
    return out


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def list_items(ak: str, sk: str, timeout: float = 30.0,
               _urlopen=None) -> list:
    """All model activations across pages, up to MAX_PAGES."""
    items: list = []
    for page in range(1, MAX_PAGES + 1):
        payload = fetch_page(ak, sk, page, timeout, _urlopen)
        batch = extract_items(payload)
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            return items
    return items


def main(argv=None) -> int:
    """CLI entry: exit 0 with rows on stdout, 2 on config, 1 on API errors."""
    parser = argparse.ArgumentParser(
        prog="facet-ark-quota",
        description="输出火山方舟各模型免费额度（协作奖励）的 freealign JSON 行")
    parser.add_argument("--output", choices=("json",), default="json",
                        help="输出格式（当前仅 json）")
    parser.parse_args(argv)
    ak = os.environ.get(AK_ENV, "").strip()
    sk = os.environ.get(SK_ENV, "").strip()
    if not ak or not sk:
        print(f"缺少火山引擎 IAM 凭据：请设置 {AK_ENV} / {SK_ENV} 环境变量"
              "（访问控制 → API 访问密钥；ark- 开头的数据面 Key 无效）",
              file=sys.stderr)
        return 2
    try:
        items = list_items(ak, sk)
    except Exception as exc:
        print(f"方舟免费额度查询失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(rows_from_items(items), ensure_ascii=False))
    return 0
