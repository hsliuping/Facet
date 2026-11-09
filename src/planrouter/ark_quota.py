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


def fetch_page(ak: str, sk: str, page: int, timeout: float = 30.0,
               _urlopen=None) -> dict:
    """One ListModelActivations call with free-usage fields attached."""
    body_obj = {"WithFreeUsage": True, "PageNumber": page,
                "PageSize": PAGE_SIZE}
    body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
    query = canonical_query({"Action": ACTION, "Version": VERSION})
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


def extract_items(payload: dict) -> list:
    """The Items array of a ListModelActivations response."""
    items = payload.get("Items")
    if items is None and isinstance(payload.get("Result"), dict):
        items = payload["Result"].get("Items")
    if not isinstance(items, list):
        raise RuntimeError("响应缺少 Items 数组")
    return items


def rows_from_items(items: list, today: str | None = None) -> list[dict]:
    """Map API items to freealign rows; only usable free chat quota survives.

    Available models only; remaining is the better of the initial free
    grant (Total − Consumed) and the sum of FreeInference resource packs
    (Total − Consumed − Reclaimed, never below zero). DataPermission packs
    grant no tokens.
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
        initial = item.get("InitialInferenceFreeUsage")
        if isinstance(initial, dict):
            remaining = max(0, _int(initial.get("Total"))
                            - _int(initial.get("Consumed")))
        for pack in item.get("FreeResourcePackItems") or []:
            if not isinstance(pack, dict) or pack.get("Type") != "FreeInference":
                continue
            pack_left = (_int(pack.get("Total")) - _int(pack.get("Consumed"))
                         - _int(pack.get("Reclaimed")))
            remaining = max(remaining, max(0, pack_left))
        if remaining <= 0:
            continue
        out.append({"model": model, "type": "Text",
                    "remaining": remaining, "expires": horizon})
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
