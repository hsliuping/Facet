"""facet-tokenhub-quota: report Tencent TokenHub free-trial models as freealign JSON rows.

TokenHub's management plane (``tokenhub.tencentcloudapi.com``) exposes the
machine-readable source of the 新人免费体验包 policy through ``DescribeModelList``:
every model covered by the free trial carries a ``FreeTrialInfo`` object
(``CapacitySize`` + ``Unit``), so the free-model catalog no longer needs the
manually curated registry subset — it follows Tencent's policy automatically.
Auth is the standard TC3-HMAC-SHA256 scheme with CAM SecretId/SecretKey (the
``ak-...`` Bearer keys TokenHub issues for chat calls cannot sign the
management plane), wrapped here into a stdlib-only command:

    facet-tokenhub-quota --output json

stdout is a bare JSON array of ``{"model", "type": "Text", "remaining",
"total", "consumed", "expires"}`` rows — the contract ``planrouter.freealign``
consumes to keep free-tier channel mappings in sync. Rows cover the
token-billed chat-capable subset only: Speech/Embedding types never serve
/chat/completions, and image/video/3D generation models (credits-billed,
``Unit`` is not ``token``; tags carry 生成/3D marks) are filtered out.

Credentials are read from ``TENCENT_SECRET_ID`` / ``TENCENT_SECRET_KEY`` in
the environment. The API reports policy capacity only — no per-account
grant state — so rows carry ``remaining`` = the trial pack size and a
synthesized ``expires`` of today + 90 days (the trial validity per the
新人免费体验包 policy; the real gate is ``remaining > 0`` re-checked on
every free-align sweep). Rows are emitted for every covered model even if
the account never claimed the pack: the platform auto-claims it on first
call, so policy coverage is the practical gate.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

# Management-plane endpoint; Action/Version/Region travel in headers.
ENDPOINT = "https://tokenhub.tencentcloudapi.com"
HOST = "tokenhub.tencentcloudapi.com"
SERVICE = "tokenhub"
VERSION = "2026-03-22"
# Official quickstart calls the console API from 华南（广州）; control-plane
# catalog APIs work from any listed region.
REGION = "ap-guangzhou"
REGION_ENV = "TENCENT_TOKENHUB_REGION"
ACTION = "DescribeModelList"
# CAM credentials shared by every management call in this module; the
# channel-bound creds and the vendor-level slot both key by these names.
ID_ENV = "TENCENT_SECRET_ID"
KEY_ENV = "TENCENT_SECRET_KEY"
PAGE_SIZE = 100
# Guard against a server that keeps returning full pages forever.
MAX_PAGES = 50
# Trial packs stay valid for 90 days but the API exposes no claim/expiry
# dates; the sweep refreshes daily, so a synthesized horizon is only
# cosmetic (worst case it overstates the tail by less than one day).
EXPIRE_DAYS = 90
SIGNED_HEADERS = "content-type;host;x-tc-action"
CONTENT_TYPE = "application/json; charset=utf-8"

# Free trial covers 语言模型 + 多模态理解模型; image/video/3D generation is
# credits-billed (Unit ≠ token) and its tags carry generation marks. Both
# signals guard the chat-capable subset — tags catch a generation id even
# if its unit string drifts, and the unit check catches one with sparse
# tags. Speech/Embedding types are excluded outright by ModelType.
GEN_TAG_MARKS = ("图像生成", "图片生成", "视频生成", "生图", "生视频",
                 "文生图", "文生视频", "3D", "语音合成", "语音识别", "数字人")


def sign_request(secret_id: str, secret_key: str, action: str, body: str,
                 timestamp: int) -> str:
    """Authorization header for one request, per Tencent TC3-HMAC-SHA256.

    ``timestamp`` is the unix-seconds stamp sent as ``X-TC-Timestamp``; the
    credential scope derives its UTC date. Only the headers we actually send
    (content-type, host, x-tc-action) are signed, and the action value is
    lowercased in the canonical form per the signature spec.
    """
    utc_date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
    canonical_headers = (
        f"content-type:{CONTENT_TYPE}\n"
        f"host:{HOST}\n"
        f"x-tc-action:{action.lower()}\n")
    hashed_body = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical_request = "\n".join([
        "POST", "/", "", canonical_headers, SIGNED_HEADERS, hashed_body])
    scope = f"{utc_date}/{SERVICE}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256", str(timestamp), scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    key = hmac.new(("TC3" + secret_key).encode("utf-8"),
                   utc_date.encode("utf-8"), hashlib.sha256).digest()
    for part in (SERVICE, "tc3_request"):
        key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return (f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
            f"SignedHeaders={SIGNED_HEADERS}, Signature={signature}")


def _post(secret_id: str, secret_key: str, action: str, body_obj: dict,
          region: str, timeout: float = 30.0, _urlopen=None) -> dict:
    """One signed management-plane call; raises RuntimeError on any failure."""
    body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
    timestamp = int(time.time())
    headers = {
        "Host": HOST,
        "Content-Type": CONTENT_TYPE,
        "X-TC-Action": action,
        "X-TC-Version": VERSION,
        "X-TC-Region": region,
        "X-TC-Timestamp": str(timestamp),
        "Authorization": sign_request(secret_id, secret_key, action, body,
                                      timestamp),
    }
    req = urllib.request.Request(
        ENDPOINT, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        opener = _urlopen or urllib.request.urlopen
        with opener(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"HTTP {exc.code}：{detail}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"请求失败：{str(exc)[:200]}") from None
    response = payload.get("Response") if isinstance(payload, dict) else None
    error = (response or {}).get("Error")
    if isinstance(error, dict):
        raise RuntimeError(
            f"API 错误 {error.get('Code')}: {error.get('Message')}")
    if not isinstance(response, dict):
        raise RuntimeError("响应缺少 Response 对象")
    return response


def fetch_page(secret_id: str, secret_key: str, offset: int, region: str,
               timeout: float = 30.0, _urlopen=None) -> dict:
    """One DescribeModelList call for a catalog window."""
    return _post(secret_id, secret_key, ACTION,
                 {"Limit": PAGE_SIZE, "Offset": offset}, region, timeout,
                 _urlopen)


def list_models(secret_id: str, secret_key: str, region: str,
                timeout: float = 30.0, _urlopen=None) -> list:
    """All catalog models across pages, up to MAX_PAGES."""
    models: list = []
    offset = 0
    for _ in range(MAX_PAGES):
        response = fetch_page(secret_id, secret_key, offset, region,
                              timeout, _urlopen)
        batch = [m for m in response.get("ModelSet") or []
                 if isinstance(m, dict)]
        models.extend(batch)
        offset += len(batch)
        total = response.get("TotalCount")
        if not batch or offset >= (total if isinstance(total, int) else offset):
            return models
    return models


def _free_trial_tokens(model: dict) -> int:
    """Trial pack size in tokens, 0 when the model has none/credits-billed."""
    info = model.get("FreeTrialInfo")
    if not isinstance(info, dict):
        return 0
    if str(info.get("Unit") or "").strip().lower() != "token":
        return 0
    try:
        return int(info.get("CapacitySize") or 0)
    except (TypeError, ValueError):
        return 0


def is_chat_candidate(model: dict) -> bool:
    """Whether a catalog entry can serve /chat/completions.

    Speech/Embedding types never serve chat; generation models carry
    generation marks in their tags. Vision/Multimodal understanding models
    (hy-vision-*, glm-5v-*) are chat-capable and stay in.
    """
    if str(model.get("ModelType") or "").strip() in ("Speech", "Embedding"):
        return False
    tags = " ".join(str(t) for t in model.get("Tags") or [])
    return not any(mark in tags for mark in GEN_TAG_MARKS)


def rows_from_models(models: list, today: str | None = None) -> list[dict]:
    """Map catalog entries to freealign rows for every free chat model.

    Only entries carrying a token-billed free trial survive (``FreeTrialInfo``
    with ``Unit``=token and positive capacity). ``remaining`` mirrors the
    trial capacity because the API exposes no per-account usage; rows are
    emitted even for an unclaimed pack (auto-claimed on first call). Order
    follows the catalog so console mapping rows stay stable across pulls.
    ``today`` (ISO date) is the horizon base for tests; real runs default
    to the current date.
    """
    base = date.fromisoformat(today) if today else date.today()
    horizon = (base + timedelta(days=EXPIRE_DAYS)).isoformat()
    out: list[dict] = []
    seen: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            continue
        name = str(model.get("ModelId") or "").strip()
        if not name or name in seen:
            continue
        capacity = _free_trial_tokens(model)
        if capacity <= 0 or not is_chat_candidate(model):
            continue
        seen.add(name)
        out.append({"model": name, "type": "Text",
                    "remaining": capacity, "total": capacity,
                    "consumed": 0, "expires": horizon})
    return out


def main(argv=None) -> int:
    """CLI entry: exit 0 with rows on stdout, 2 on config, 1 on API errors."""
    parser = argparse.ArgumentParser(
        prog="facet-tokenhub-quota",
        description="输出腾讯云 TokenHub 各模型免费体验包的 freealign JSON 行")
    parser.add_argument("--output", choices=("json",), default="json",
                        help="输出格式（当前仅 json）")
    parser.parse_args(argv)
    secret_id = os.environ.get(ID_ENV, "").strip()
    secret_key = os.environ.get(KEY_ENV, "").strip()
    if not secret_id or not secret_key:
        print(f"缺少腾讯云管理面凭据：请设置 {ID_ENV} / {KEY_ENV} 环境变量"
              "（访问管理 → API 密钥管理；ak- 开头的数据面 API Key 无效）",
              file=sys.stderr)
        return 2
    region = os.environ.get(REGION_ENV, "").strip() or REGION
    try:
        models = list_models(secret_id, secret_key, region)
    except Exception as exc:
        print(f"TokenHub 免费模型查询失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(rows_from_models(models), ensure_ascii=False))
    return 0
