"""Local HTTP gateway: one compatible endpoint, automatic channel failover."""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import http.client
import io
import json
import os
import random
import re
import secrets
import shutil
import sys
import threading
import time
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from datetime import date, datetime, timedelta, timezone

from . import subscriptions as subs
from . import ui
from . import maintenance as maint
from . import dsh as dsh_mod
from . import freealign as freealign_mod
from . import providers as providers_mod
from . import facts as facts_mod
from . import watch as watch_mod
from . import (ark_quota, bailian_quota, hunyuan_quota,
               opencode_quota, zhipu_quota)
from . import mode_meta
from . import benchmarks as bench_mod
from .audit import RequestLog
from .completions import CompletionsAdapter
from .completions import to_chat_request as legacy_to_chat_request
from .convert import convert_request, convert_response, inject_system, stream_converter
from .core import (
    ANTHROPIC, AUTO_MODEL, FREE_RESERVE_DEFAULT_TOKENS,
    FREE_RESERVE_TOKENS, OPENAI,
    QUOTA_MARKERS, Config,
    StateStore, channel_ref,
    apply_upstream_headers,
    body_has_vision, channel_takes_images, dispatch_wire_model,
    ensure_channel_ids, explicit_models,
    open_with_proxy, parse_cooldown, parse_config, parse_free_quota,
    resolve_model, resolve_secret, upstream_path_for, vision_wire,
)
from .keys import KeyStore
from .notifier import ReminderService
from .center import CenterClient
from .. import __version__
from .persistence import StatePersistence
from .probe import (MAX_CHANNELS, TEST_TIMEOUT, is_free_model, pick_model,
                    pick_probe_model, probe_channel, probe_models)
from .quota import parse_quota, quota_exhaustion_wait
from .report import build_member_costs, build_report
from .responses import ResponsesAdapter, to_chat_request
from .usage import UsageRecorder

MAX_BODY_BYTES = 8 * 1024 * 1024
UNSAFE_HEADER = re.compile(r"[^A-Za-z0-9._:/@+ -]")
# The management console authenticates with listen.admin_key only. A first
# start seeds no password at all (seed.py): the console stays open on the
# loopback address and the browser forces the operator to set a management
# password before anything can be used. A gateway api_key without an admin_key
# keeps the management API locked instead of falling back to open access.
# Sliding window (seconds) behind a member key's `rpm` cap; timestamps of
# accepted inference calls are kept in memory only, so a restart resets it.
MEMBER_RPM_WINDOW = 60.0
# How long a vendor plan snapshot is served without re-hitting the Ark
# management plane; the subscriptions page reloads often enough that a live
# fetch per view would be wasteful and slow.
PLAN_TTL = 300.0
# A failed fetch is retried sooner than a successful one: the vendor call
# is cheap and the user is staring at the error text waiting to fix auth.
PLAN_ERROR_TTL = 60.0
# A vendor-reported window reset is authoritative (GetAFPUsage / coding-plan
# percent windows), but never trust one for months: a plan-driven cooldown is
# capped at this horizon, after which normal 429 handling takes over again.
PLAN_RESET_CAP_HOURS = 45 * 24
# A 429/404 or an allowance marker belongs to the upstream model that was
# called; the sibling models of the same channel keep their own quotas. An
# account out of credit or with an invalid key is different: every model
# behind it fails, so those failures freeze the whole channel.
ACCOUNT_BALANCE_MARKERS = (
    "billing", "credit", "balance", "arrearage",
    "payment", "余额不足", "欠费", "账户", "账号",
)
ACCOUNT_SCOPED_MARKERS = ("insufficient_quota",) + ACCOUNT_BALANCE_MARKERS
# A free-tier vendor rejecting one model ("Free quota exhausted, please use
# free tier only") is metering that model's grant, not the account — even
# though the body also carries insufficient_quota. Treating it as
# account-scoped froze the whole channel for hours while its siblings still
# had headroom.
MODEL_QUOTA_MARKERS = (
    "free quota", "free-tier", "free tier", "免费额度", "免费模型",
)


def _account_scoped_failure(quota: bool, low: str, channel) -> bool:
    """True when the upstream blames the account, not a single model.

    Vendors that hand every model its own free grant (百炼 / 火山引擎) report
    exhaustion per model; a channel flagged ``per_model_quota`` therefore
    cools only the model that failed, so its siblings keep serving. Balance
    and billing text still means the whole account and freezes the channel.
    """
    if any(marker in low for marker in MODEL_QUOTA_MARKERS):
        return False
    if (quota and getattr(channel, "per_model_quota", False)
            and not any(marker in low for marker in ACCOUNT_BALANCE_MARKERS)):
        return False
    return any(marker in low for marker in ACCOUNT_SCOPED_MARKERS)


# A channel failing this many times in a row (no success in between) deserves
# a runtime alert: something is broken even while cooldowns route around it.
RUNTIME_ALERT_ERROR_THRESHOLD = 3
# Consecutive failed cycles of one scheduled sweep before it raises a runtime
# alert (self-maintenance breaking must be visible without watching the console).
TASK_FAIL_ALERT_THRESHOLD = 2
# A sweep gate queues a contender for this long before giving up with the
# busy reply: the console button (and any other contender) waits out the
# running sweep instead of vanishing into a busy skip.
GATE_QUEUE_WAIT = 600.0
# A maintenance pass re-uses catalog results this fresh instead of fetching
# every upstream /models endpoint a second time: the catalog refresher's
# sweep runs minutes before the maintenance pass on every schedule, and the
# two sweeps hitting the same endpoints back to back doubled the traffic
# without learning anything new.
CATALOG_REUSE_WINDOW = 900.0
# The freshness freeze trips a little past one sweep interval, not exactly at
# it: the task loop anchors the next sweep on the previous cycle's start, so
# a healthy stamp-to-stamp gap is exactly one interval, but a cycle that
# runs long (vendor commands are capped by freealign.CMD_TIMEOUT) delays its
# successor past that anchor. Tripping at the bare interval would flap the
# channel frozen for that tail window every cycle even when nothing is
# wrong; the grace only defers real staleness by two minutes.
STALE_FREEZE_GRACE = 120.0
# Human names for the persisted artefacts whose failed writes runtime_alerts
# reports (see App._note_write_failure / usage / notifier write callbacks).
PERSIST_KIND_LABELS = {
    "usage": "用量统计 usage.json",
    "notifier": "通知状态 notifications.json",
    "state": "渠道状态快照 state.json",
    "subscriptions": "订阅与通知设置 subscriptions.json",
    "vendor_quota": "厂家额度快照 vendorquota.json",
    "maintenance_log": "维护日志 maintenance.jsonl",
    "freealign_log": "免费额度日志 freealign.jsonl",
    "probe_log": "探测记录 probe.jsonl",
    "config_backup": "配置备份 config.json.bak",
    "maintenance_pool": "维护模型池 maintenance_models.json",
    "change_log": "变更事件 change-events.jsonl",
    "benchmark_override": "能力分覆盖 model-scores.override.json",
    "benchmark_refresh_log": "能力分刷新日志 benchmark_refresh.jsonl",
}
# Report logs grow one line per sweep; past this size they keep only the last
# REPORT_LOG_KEEP lines (audit.jsonl rotates by bytes, usage.json by retention).
REPORT_LOG_MAX_BYTES = 2 * 1024 * 1024
REPORT_LOG_KEEP = 500


def append_jsonl(path: Path, entry: dict, kind: str, on_error=None,
                 on_ok=None) -> None:
    """Append one report line, trimming the file once it grows past the cap.

    Never raises: a failed append or trim is reported through the same
    write-failure channel as the other persisted artefacts, so a read-only or
    full data volume shows up as a runtime alert instead of a silent drop.
    """
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if path.stat().st_size > REPORT_LOG_MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text("\n".join(lines[-REPORT_LOG_KEEP:]) + "\n",
                           encoding="utf-8")
            os.replace(tmp, path)
    except OSError as exc:
        if on_error:
            on_error(kind, exc)
    else:
        if on_ok:
            on_ok(kind)


# Vendor capability registries (usage fetchers, wallets, check-in handlers)
# moved to mode_meta.py: one home next to the enums that classify them.
# server.py dispatches through mode_meta.KEY_API_FETCHERS /
# mode_meta.MGMT_WALLET_FETCHERS / mode_meta.CHECKIN_HANDLERS.


def _header_safe(value) -> str:
    """Client-controlled values must never be able to inject a response header."""
    return UNSAFE_HEADER.sub("-", str(value))[:120]


class UpstreamStreamError(Exception):
    """The vendor connection broke while its response body was being read."""

    def __init__(self, exc: BaseException):
        super().__init__(str(exc)[:300])
        self.exc = exc


def _read_upstream(resp, size: int) -> bytes:
    """Read the upstream body, naming transport failures as the vendor's.

    Only reads of the upstream socket go through here, so an
    UpstreamStreamError is always the vendor's transport dying mid-body — a
    truncated chunked stream, a reset, a read timeout — and never the
    gateway's own post-processing or the client hanging up.
    """
    try:
        data = resp.read(size)
    except (http.client.HTTPException, TimeoutError, OSError) as exc:
        raise UpstreamStreamError(exc) from exc
    remaining = getattr(resp, "length", None)
    if not data and remaining:
        # A Content-Length body that ends early comes back from read(amt) as a
        # clean EOF, not an exception: left alone it would be dressed up as a
        # complete answer. It is a truncated stream.
        raise UpstreamStreamError(http.client.IncompleteRead(b"", remaining))
    return data


def _stream_error_event(protocol: str, responses: bool, message: str) -> bytes:
    """SSE error event, in the client's protocol, for a stream cut mid-response.

    No terminal event follows it on purpose: a client that only watches for
    [DONE] / response.completed must still see the stream as broken, never as
    a complete answer that happens to be short.
    """
    if protocol == ANTHROPIC:
        body = {"type": "error",
                "error": {"type": "api_error", "message": message}}
    elif responses:
        body = {"type": "error", "code": "server_error",
                "message": message, "param": None}
    else:
        body = {"error": {"message": message, "type": "upstream_error",
                          "code": "stream_truncated", "param": None}}
    line = json.dumps(body, ensure_ascii=False)
    prefix = "event: error\n" if protocol == ANTHROPIC or responses else ""
    return f"{prefix}data: {line}\n\n".encode("utf-8")


def _audit_headers(decision) -> dict:
    return {
        "X-Routed-Model-Mode": decision.mode,
        "X-Routed-Model-Reason": decision.reason,
        "X-Routed-Requested-Model": _header_safe(decision.requested),
        "X-Route": _header_safe(decision.hint),
    }


def _status_channels(decision, route) -> tuple[str, ...]:
    """Names to surface in a 503 channel_status map.

    Exact-mode requests name their model, so the map lists the channels
    that actually map that name (chain-agnostic) instead of the nominal
    route members — the caller sees why every same-name channel is out.
    """
    if decision.mode == "exact":
        return decision.exact_channels
    return route.channels if route else ()


def _rid() -> str:
    """Short per-request id for the audit trail.

    Every attempt of one logical request shares it, so the console can group
    the audit entries into a single timeline without any extra plumbing.
    """
    return secrets.token_hex(4)


def _burn_ratio(record: dict, stamp: datetime) -> float:
    """Remaining quota fraction per remaining day of one subscription.

    Drives the same-expiry tiebreak of the billing ranks: among plans
    lapsing on the same day, the one with more quota left per day left is
    drained first — it is the one most likely to go unused before it
    expires. Plans without quota accounting return 0.0 and keep console
    chain order among themselves.
    """
    try:
        limit = float(record.get("quota_limit") or 0)
        used = float(record.get("quota_used") or 0)
    except (TypeError, ValueError):
        return 0.0
    if limit <= 0:
        return 0.0
    remaining = max(0.0, min(1.0, (limit - used) / limit))
    days = max((stamp.date() - datetime.now().date()).days, 0) + 1
    return remaining / days


def _quota_at_epoch(at: str) -> float | None:
    """Epoch seconds of a persisted vendor snapshot stamp, or None.

    The sweep writes local-time ``%Y-%m-%d %H:%M:%S``; legacy files may
    carry either that or the ISO form. An unparsable stamp means "unknown",
    which the freshness gate treats as unfrozen rather than frozen.
    """
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(at, fmt).timestamp()
        except ValueError:
            continue
    return None


def _part_tag(part) -> str:
    """Compact descriptor of one content part — types and sizes, never base64."""
    if not isinstance(part, dict):
        return type(part).__name__
    kind = str(part.get("type") or "text")
    if kind in ("image_url", "input_image", "image"):
        src = part.get("image_url") or part.get("source") or part
        url = str((src.get("url") if isinstance(src, dict) else None) or "")
        if not url and isinstance(src, dict):
            url = "data:%s;base64,%s" % (src.get("media_type") or "?",
                                         str(src.get("data") or "")[:8])
        if url.startswith("data:"):
            return f"image(data,{len(url) // 1024}kb)"
        return f"image(url,{url[:40]})" if url else "image(empty)"
    if kind == "text":
        return f"text({len(str(part.get('text') or ''))}c)"
    return kind


def _log_inbound_shape(body: dict, raw_len: int) -> None:
    """One line per request in the container log: what the client sent us.

    Multimodal disputes (an IDE that strips images before sending) are
    settled by looking at this line — it shows whether any image part
    actually reached the gateway and how large it was. Printed only when
    FACET_INBOUND_DEBUG is set; the per-request line is noise otherwise.
    """
    if not os.environ.get("FACET_INBOUND_DEBUG", "").strip():
        return
    try:
        messages = body.get("messages") or []
        tags = []
        for i, message in enumerate(messages[:8]):
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                desc = f"str({len(content)}c)"
            elif isinstance(content, list):
                desc = "list[" + ",".join(_part_tag(p) for p in content[:6]) + "]"
            elif content is None:
                desc = "null"
            else:
                desc = type(content).__name__
            tags.append(f"[{i}]{message.get('role') or '?'}:{desc}")
        if len(messages) > 8:
            tags.append(f"...+{len(messages) - 8}more")
        print(f"[inbound] model={body.get('model')} bytes={raw_len} "
              f"msgs={len(messages)} | " + " | ".join(tags), flush=True)
    except Exception:
        pass  # logging must never break the request


def _json(h: BaseHTTPRequestHandler, status: int, payload: dict, extra: dict | None = None,
          close: bool = False) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    if close:
        # send_header("Connection", "close") also flips h.close_connection so
        # the server stops reading further requests from this socket.
        h.send_header("Connection", "close")
    for key, value in (extra or {}).items():
        h.send_header(key, str(value))
    h.end_headers()
    h.wfile.write(data)


def _usage_fields(usage: dict) -> tuple[int, int, int]:
    """(total, input, cached) tokens from an upstream usage block.

    Vendors report prompt-cache hits differently (OpenAI
    prompt_tokens_details.cached_tokens, Anthropic cache_read_input_tokens,
    DeepSeek prompt_cache_hit_tokens), so the shape of the block decides how
    it is read rather than the client protocol (a gateway may convert the
    body but keep upstream usage verbatim).
    """
    try:
        cached = int(usage.get("cache_read_input_tokens")
                     or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                     or usage.get("prompt_cache_hit_tokens")
                     or usage.get("cached_tokens") or 0)
        if "prompt_tokens" in usage or "total_tokens" in usage:
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            total = int(usage.get("total_tokens") or prompt + completion)
            return total, prompt, min(cached, prompt)
        prompt = (int(usage.get("input_tokens") or 0)
                  + int(usage.get("cache_creation_input_tokens") or 0) + cached)
        return (prompt + int(usage.get("output_tokens") or 0), prompt,
                min(cached, prompt))
    except (TypeError, ValueError, AttributeError):
        return 0, 0, 0


def _usage(data: bytes, protocol: str = OPENAI) -> tuple[int, int, int]:
    try:
        usage = (json.loads(data).get("usage") or {})
    except (ValueError, AttributeError):
        return 0, 0, 0
    return _usage_fields(usage)


def _stream_tokens(data: bytes, protocol: str = OPENAI) -> tuple[int, int, int]:
    """Sum `usage` blocks from SSE data lines (OpenAI final chunk, Anthropic deltas)."""
    total = prompt = cached = 0
    for line in data.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            event = json.loads(payload)
            usage = event.get("usage") or {}
            if not usage and isinstance(event.get("message"), dict):
                usage = event["message"].get("usage") or {}
        except (ValueError, AttributeError):
            continue
        if not usage:
            continue
        total_add, prompt_add, cached_add = _usage_fields(usage)
        total += total_add
        prompt += prompt_add
        cached += cached_add
    return total, prompt, cached


def _parse_multipart(headers: dict, body: bytes) -> dict:
    """Parse multipart/form-data request body.

    Returns a dict with form fields and file parts. Used for /v1/audio/transcriptions
    and /v1/audio/translations endpoints.

    Format: {"fields": {field_name: value}, "files": {field_name: (filename, content, content_type)}}
    """
    import email.parser
    import email.policy

    content_type = headers.get("Content-Type") or headers.get("content-type") or ""
    if "multipart/form-data" not in content_type.lower():
        raise ValueError("请求体不是 multipart/form-data 格式")

    # Use email.parser to parse multipart data.
    # The parser needs the Content-Type header to know the boundary.
    # Prepend it to the body as a fake message header.
    fake_headers = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    parser = email.parser.BytesParser(policy=email.policy.default)
    msg = parser.parsebytes(fake_headers + body)

    fields = {}
    files = {}
    for part in msg.walk():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if name is None:
            continue
        filename = part.get_filename()
        if filename:
            # File part
            part_content_type = part.get_content_type() or "application/octet-stream"
            # For files, we need raw bytes, not decoded string
            raw_content = part.get_payload(decode=True)
            files[name] = (filename, raw_content, part_content_type)
        else:
            # Regular form field
            content = part.get_content()
            fields[name] = content

    return {"fields": fields, "files": files}


def _estimate_tokens(body: dict) -> int:
    """Cheap local estimate for /v1/messages/count_tokens; never calls an upstream."""
    parts: list[str] = []
    system = body.get("system")
    if system is not None:
        parts.append(system if isinstance(system, str) else json.dumps(system, ensure_ascii=False))
    for message in body.get("messages") or []:
        if isinstance(message, dict):
            content = message.get("content")
            parts.append(content if isinstance(content, str)
                         else json.dumps(content, ensure_ascii=False))
    for key in ("tools", "tool_choice", "thinking", "metadata"):
        if body.get(key) is not None:
            parts.append(json.dumps(body[key], ensure_ascii=False))
    text = "\n".join(parts)
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return max(1, (len(text) - non_ascii) // 4 + non_ascii + len(parts) * 2)


def normalize_kimi_temperature(payload: dict) -> dict:
    """Moonshot Kimi models (e.g. Ark-hosted kimi-k3) only accept
    temperature=1; any other value makes the upstream reject the call with
    HTTP 400. Enforce the vendor rule in every outgoing chat payload."""
    if "kimi" in str(payload.get("model") or "").lower():
        payload["temperature"] = 1
    return payload


def channel_tier(channel, subscriptions: list[dict]) -> str:
    """Explicit channel.tier wins; otherwise infer from linked subscriptions."""
    if channel.tier:
        return channel.tier
    for record in subscriptions:
        refs = record.get("channels") or []
        # Records store the stable channel id; files written before ids
        # existed still carry the name, so accept both.
        if channel_ref(channel) not in refs and channel.name not in refs:
            continue
        try:
            if float(record.get("price") or 0) > 0:
                return "paid"
        except (TypeError, ValueError):
            continue
    return ""


def model_tier(channel, subscriptions: list[dict], model: str) -> str:
    """Per-model tier wins (manual switching), then the channel default."""
    if model in channel.tiers:
        return channel.tiers[model]
    # OpenRouter names freeness in the model id itself: only the ":free"
    # suffix is the free variant, every other mapping bills the account. The
    # wizard files the whole channel under tier "free" (its :free catalogue
    # is the point), which would otherwise paint paid siblings like
    # aion-labs/aion-2.0 as 免费可用 in the model table.
    if channel.provider == "openrouter":
        return "free" if is_free_model(model) else "paid"
    return channel_tier(channel, subscriptions)


def merge_secrets(new_raw: dict, old_raw: dict) -> dict:
    """Keep stored secrets when the editor sends '***' or an empty value."""
    merged = copy.deepcopy(new_raw)
    by_name = {
        c.get("name"): c for c in (old_raw.get("channels") or [])
        if isinstance(c, dict)
    }
    by_id = {
        str(c.get("id")): c for c in (old_raw.get("channels") or [])
        if isinstance(c, dict) and c.get("id")
    }

    def previous_channel(channel: dict) -> dict:
        """Stored channel for this editor entry: id first, legacy name second."""
        ident = str(channel.get("id") or "")
        return by_id.get(ident) or by_name.get(channel.get("name")) or {}

    for channel in (merged.get("channels") or []):
        if not isinstance(channel, dict):
            continue
        prev = previous_channel(channel)
        # A rename keeps the id, and carrying it forward is what makes the
        # subscription link survive. An older console tab that omits the id
        # inherits it from the stored channel of the same name.
        if not channel.get("id") and prev.get("id"):
            channel["id"] = prev["id"]
        if channel.get("api_key") in (None, "", "***"):
            key = prev.get("api_key")
            if key:
                channel["api_key"] = key
            else:
                channel.pop("api_key", None)
        # Vendor bookkeeping is newer than some open console tabs: a stale
        # editor omits provider/mode entirely and would otherwise erase them
        # one channel per save. Absent or empty values inherit; explicit
        # values win — a bound vendor is replaced, never silently cleared.
        for field in ("provider", "mode"):
            if not channel.get(field) and prev.get(field):
                channel[field] = prev[field]
        # Channel-bound management creds (channel = account) follow the same
        # masked-inheritance rules as the vendor-level slot.
        creds = channel.get("creds")
        if creds is None and prev.get("creds"):
            channel["creds"] = copy.deepcopy(prev["creds"])
        if isinstance(creds, dict):
            prev_creds = prev.get("creds") or {}
            for env_name, value in list(creds.items()):
                if value in (None, "", "***"):
                    inherited = prev_creds.get(env_name)
                    if inherited:
                        creds[env_name] = inherited
                    else:
                        creds.pop(env_name, None)
            if not creds:
                channel.pop("creds", None)
    listen = merged.setdefault("listen", {})
    for field in ("api_key", "admin_key"):
        if listen.get(field) in (None, "", "***"):
            key = (old_raw.get("listen") or {}).get(field)
            if key:
                listen[field] = key
            else:
                listen.pop(field, None)
    # Vendor management credentials follow the same rules: the editor round-
    # trips masked values as "***", so empty/masked entries inherit the
    # stored value and an entirely absent section (a console tab opened
    # before the field existed) inherits the whole stored section.
    if "credentials" not in merged and old_raw.get("credentials"):
        merged["credentials"] = copy.deepcopy(old_raw["credentials"])
    creds = merged.get("credentials")
    old_creds = old_raw.get("credentials")
    if isinstance(creds, dict):
        for vendor_id, vendor_map in creds.items():
            if not isinstance(vendor_map, dict):
                continue
            old_map = old_creds.get(vendor_id) if isinstance(old_creds, dict) else None
            for env_name, value in list(vendor_map.items()):
                if value in (None, "", "***"):
                    inherited = (old_map or {}).get(env_name)
                    if inherited:
                        vendor_map[env_name] = inherited
                    else:
                        vendor_map.pop(env_name, None)
        for vendor_id in [k for k, v in creds.items()
                          if isinstance(v, dict) and not v]:
            creds.pop(vendor_id)
    # Accounts carry their own AK/SK; same masked-inheritance rules keyed by
    # account id. A console tab opened before accounts existed inherits the
    # whole stored list; a present list wins (deleted accounts stay deleted).
    if "accounts" not in merged and old_raw.get("accounts"):
        merged["accounts"] = copy.deepcopy(old_raw["accounts"])
    old_accounts = {
        str(a.get("id") or ""): a
        for a in (old_raw.get("accounts") or []) if isinstance(a, dict)
    }
    for account in (merged.get("accounts") or []):
        if not isinstance(account, dict):
            continue
        acc_creds = account.get("credentials")
        prev_creds = (old_accounts.get(str(account.get("id") or "")) or {}).get(
            "credentials") or {}
        if not isinstance(acc_creds, dict):
            # Entry posted without a credentials block (older console tab):
            # inherit whatever the account had instead of losing its AK/SK.
            if prev_creds:
                account["credentials"] = copy.deepcopy(prev_creds)
            else:
                account.pop("credentials", None)
            continue
        for env_name, value in list(acc_creds.items()):
            if value in (None, "", "***"):
                inherited = prev_creds.get(env_name)
                if inherited:
                    acc_creds[env_name] = inherited
                else:
                    acc_creds.pop(env_name, None)
        if not acc_creds:
            account.pop("credentials", None)
    return merged


def _maintenance_summary(report: dict) -> str:
    if report.get("busy"):
        return str(report.get("message") or "上一次自维护仍在运行，本次已跳过")
    return (f"应用 {len(report.get('applied') or [])} 项"
            f" · 建议 {len(report.get('suggestions') or [])} 条"
            f" · 注意 {len(report.get('alerts') or [])} 条")


def _free_align_summary(report: dict) -> str:
    if report.get("busy"):
        return str(report.get("message") or "上一次自维护仍在运行，本次已跳过")
    text = f"{len(report.get('aligned') or [])} 个免费渠道已核对"
    changed = len(report.get("patch") or {})
    if changed:
        text += f" · 改写 {changed} 个映射"
    alerts = len(report.get("alerts") or [])
    if alerts:
        text += f" · {alerts} 个失败"
    return text


def _row_better(old: dict, new: dict) -> bool:
    """Pick the best channel entry for one shared wire model, mirroring the
    main agent sort: known tool-call beats unknown beats none, then free
    tier, then benchmark score, then the higher stability score."""
    def rank(row: dict) -> tuple:
        tool = (0 if row["tool_call"] is True
                else 1 if row["tool_call"] is None else 2)
        score = row.get("agent_score")
        return (tool,
                0 if row["tier"] == "free" else 1,
                0 if score is not None else 1,
                -(score or 0.0),
                -row["score"])

    return rank(new) < rank(old)


class SerialGate:
    """One-at-a-time execution where contenders queue instead of being dropped.

    The scheduled sweep and the console button share one gate per concern
    (config-apply, free verify, source watch). A plain lock with a
    non-blocking acquire answered a manual click with a busy reply whenever
    a scheduled sweep was in its minutes-long cycle, and the operator had to
    know to click again later. Queueing instead: contenders wait up to
    GATE_QUEUE_WAIT for the holder to release, then run in arrival order.
    Ownership transfers directly from the releaser to the head of the queue
    (no thundering herd, no unfair re-acquire race against other waiters).
    """

    def __init__(self, wait: float = GATE_QUEUE_WAIT):
        self._wait = wait
        self._lock = threading.Lock()
        self._waiters: deque[threading.Event] = deque()
        self._held = False

    def acquire(self, timeout: float | None = None) -> bool:
        timeout = self._wait if timeout is None else timeout
        with self._lock:
            if not self._held:
                self._held = True
                return True
            if timeout <= 0:
                return False
            waiter = threading.Event()
            self._waiters.append(waiter)
        if waiter.wait(timeout):
            return True
        # Timed out — unless ownership was handed over in between, in which
        # case this caller does hold the gate and must release it.
        with self._lock:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
                return False
            return True

    def release(self) -> None:
        with self._lock:
            waiter = self._waiters.popleft() if self._waiters else None
            if waiter is None:
                self._held = False
        if waiter is not None:
            waiter.set()  # _held stays True: ownership moved to the waiter

    def __enter__(self):
        # Unlike threading.Lock, acquire can time out; callers that must be
        # certain they hold the gate should pass a short wait instead of
        # relying on the protocol (a False acquire here means the body runs
        # unguarded — do not use `with` across the busy path).
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class ScheduledTask:
    """Shared loop and status bookkeeping for the periodic sweeps.

    The console lists every task with its cycle, last outcome and next run,
    so all of them report through the same fields. The interval is re-read
    each cycle: hot-reloaded config takes effect without a restart, and 0
    still means "keep the default interval" until a restart disables it.
    """

    key = ""
    label = ""
    description = ""
    hours_key = ""
    FIRST_DELAY = 60.0

    def __init__(self, app: "App"):
        self.app = app
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"planrouter-{self.key}")
        self.last_run_at = 0.0
        self.next_run_at = 0.0
        self.last_summary = ""
        self.last_error = ""
        # Consecutive failed cycles; runtime_alerts alarms on this so a sweep
        # that keeps breaking does not just sit in the task table.
        self.consecutive_failures = 0
        self.running = False

    def hours(self) -> float:
        return float(getattr(self.app.config.defaults, self.hours_key) or 24)

    def interval(self) -> float:
        # Fractional hours are valid (free_align runs every 0.5h); the floor
        # only guards a nonsense near-zero value from spinning the loop.
        return max(0.05, self.hours()) * 3600.0

    def start(self) -> None:
        self.next_run_at = time.time() + self.FIRST_DELAY
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def note_run(self, summary: str) -> None:
        """Record a run triggered outside the loop (console buttons)."""
        self.last_run_at = time.time()
        self.last_summary = summary

    def _loop(self) -> None:
        if self._stop.wait(self.FIRST_DELAY):
            return
        while True:
            started = time.time()
            self._run()
            delay = self.next_delay()
            # Anchor the next cycle on this cycle's START, not its
            # completion: completion-anchoring adds the cycle's own duration
            # to every interval, so the beat drifts forward by however long
            # a sweep takes, indefinitely.
            self.next_run_at = started + delay
            if self._stop.wait(max(0.0, self.next_run_at - time.time())):
                return

    def next_delay(self) -> float:
        """Seconds until the next cycle; overridable for short retries."""
        return self.interval()

    def _run(self) -> None:
        self.running = True
        self.last_run_at = time.time()
        try:
            self.last_summary = self.run_once() or ""
            self.last_error = ""
            self.consecutive_failures = 0
        except Exception as exc:  # a broken sweep must never kill the gateway
            self.last_error = str(exc) or exc.__class__.__name__
            self.consecutive_failures += 1
        finally:
            self.running = False

    def run_once(self) -> str:
        raise NotImplementedError

    def status(self) -> dict:
        enabled = bool(getattr(self.app.config.defaults, self.hours_key))
        if self.running:
            state = "running"
        elif not enabled:
            state = "disabled"
        elif self.last_error:
            state = "warn"
        else:
            state = "idle"
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "enabled": enabled,
            "hours": self.hours() if enabled else 0,
            "state": state,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at if enabled else 0,
            "last_summary": self.last_summary,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }


class CatalogRefresher(ScheduledTask):
    """Background sweep: re-fetch every channel's upstream model catalog.

    Runs once shortly after startup, then every
    defaults.catalog_refresh_hours.
    """

    key = "catalog"
    label = "目录刷新"
    description = "拉取各渠道上游模型目录，变化写回覆盖层并持久化"
    hours_key = "catalog_refresh_hours"
    FIRST_DELAY = 30.0

    def run_once(self) -> str:
        results = self.app.refresh_all_catalogs()
        if not results:
            return "没有可刷新的渠道"
        failed = sum(1 for r in results if not r.get("ok"))
        if failed:
            return f"{len(results)} 个渠道，{failed} 个失败"
        return f"{len(results)} 个渠道全部刷新"


class MaintenanceRunner(ScheduledTask):
    """Daily self-maintenance: catalog-confirmed auto-fixes + free-quota LLM analysis.

    Runs once shortly after startup (after the first catalog sweep), then
    every defaults.maintenance_hours. One run = refresh catalogs, apply
    catalog-confirmed changes (with a config backup), optionally ask the
    maintenance LLM for advisory notes, and record a report.
    """

    key = "maintenance"
    label = "自维护"
    description = "刷新目录，自动应用目录确证的变更，LLM 分析出建议并记录报告"
    hours_key = "maintenance_hours"
    FIRST_DELAY = 120.0
    MAX_REPORTS = 20

    def run_once(self) -> str:
        return _maintenance_summary(self.app.run_maintenance("scheduled"))


class FreeAlignRunner(ScheduledTask):
    """Scheduled free-quota alignment for free-tier channels (freealign.py).

    Vendors like Bailian expose their free-quota state only through a local
    CLI, and the set of models carrying free quota moves over time (models
    expire, new ones join). This sweep re-runs the vendor command and rewrites
    the affected channels' mappings so the router never routes to a model
    whose free quota has run out. Runs once shortly after the first
    maintenance sweep, then every defaults.free_align_hours.
    """

    key = "free_align"
    label = "免费额度对齐"
    description = "重跑厂家免费额度工具，改写免费渠道的模型映射"
    hours_key = "free_align_hours"
    FIRST_DELAY = 150.0
    # The first cycle often collides with the startup maintenance sweep, which
    # holds the config-apply lock for as long as its LLM analysis runs. A busy
    # skip must retry soon: waiting a whole free_align_hours left vendor numbers
    # (and the resulting quota calibration) stale for a day.
    RETRY_AFTER_BUSY = 900.0

    _busy = False

    def run_once(self) -> str:
        report = self.app.run_free_align("scheduled")
        self._busy = bool(report.get("busy"))
        return _free_align_summary(report)

    def next_delay(self) -> float:
        return self.RETRY_AFTER_BUSY if self._busy else self.interval()


class FreeVerifyRunner(ScheduledTask):
    """Scheduled full verification of the free pool.

    The console button did this by hand, so without a schedule a dead free
    model kept getting routed to until someone happened to look. Shares
    App.free_verify_lock with that button, so the two never overlap. Runs
    once after startup settles, then every defaults.free_verify_hours.
    """

    key = "free_verify"
    label = "免费模型体检"
    description = "逐个实测免费渠道的模型映射，失效的自动打标签让路由避开"
    hours_key = "free_verify_hours"
    FIRST_DELAY = 240.0

    def run_once(self) -> str:
        report = self.app.verify_free_models(trigger="scheduled")
        if report.get("skipped"):
            return "已有体检在运行，本次跳过"
        return (f"实测 {report.get('checked', 0)} 个模型："
                f"可用 {report.get('alive', 0)} · 失效 {report.get('dead', 0)}"
                f" · 无法判定 {report.get('inconclusive', 0)}")


class WatchRunner(ScheduledTask):
    """Scheduled source watch (watch.py): observe upstream info sources.

    The vendor registry's docs/models URLs, every enabled channel's models
    endpoint and every subscription's plan doc are re-fetched; model-set
    changes land in the console's change events. Purely observational —
    nothing is auto-applied and no LLM is involved. Shares App.watch_lock
    with the console button, so the two never overlap. Runs once after
    startup settles, then every defaults.watch_hours (0 disables).
    """

    key = "watch"
    label = "信息源监视"
    description = "抓取厂家文档/模型接口、渠道目录与套餐文档，模型集变化记为变更事件"
    hours_key = "watch_hours"
    FIRST_DELAY = 180.0

    def run_once(self) -> str:
        report = self.app.run_change_watch("scheduled")
        if report.get("busy"):
            return "已有监视扫描在运行，本次跳过"
        return report.get("summary") or "监视完成"


class ModelScopeCheckinRunner(ScheduledTask):
    """Scheduled daily check-in claim for vendors that need one.

    ModelScope has no check-in API: the daily grant lands with the day's
    first website sign-in, and one signed-in request to login/info is
    enough to make that happen. Every ModelScope channel's api_key doubles
    as its web session token, so the sweep claims the grant over plain
    HTTP — no browser. Idempotent per Beijing day: a second run on the
    same day sees the grant already in the ledger and does nothing. The
    sweep runs once per Beijing morning inside the 08:30-10:00 window
    (random minute per day), not on a plain interval; the hours value is
    an enable switch (0 disables) and a status hint.

    Which channels need a claim comes from the registry classification
    (mode_meta.requires_checkin_of) and the executor from
    mode_meta.CHECKIN_HANDLERS — a second check-in vendor is a new
    registry row, not a new Runner class.
    """

    key = "modelscope_checkin"
    label = "魔搭魔粒领取"
    description = "以一次登录请求触发魔搭每日魔粒发放并核对流水（每天 08:30-10:00 随机执行）"
    hours_key = "modelscope_checkin_hours"
    FIRST_DELAY = 240.0
    _OUTCOMES = {"done": "今日已领", "claimed": "领取成功",
                 "inactive": "登录成功但未见发放"}

    def _channels(self):
        """Enabled channels whose mode declares a daily check-in, paired
        with their check-in executor."""
        out = []
        for channel in self.app.config.channels:
            if not getattr(channel, "enabled", True):
                continue
            provider = getattr(channel, "provider", "")
            mode = self.app.provider_mode(provider,
                                          getattr(channel, "mode", ""))
            if not mode_meta.requires_checkin_of(provider, mode):
                continue
            handler = mode_meta.CHECKIN_HANDLERS.get(provider)
            if handler is not None:
                out.append((channel, handler))
        return out

    def run_once(self) -> str:
        pairs = self._channels()
        if not pairs:
            return "没有需要签到的启用渠道"
        parts = []
        for channel, handler in pairs:
            try:
                result = handler(channel.api_key)
            except Exception as exc:
                parts.append(f"{channel.name} 失败：{exc}")
                continue
            word = self._OUTCOMES.get(result.get("outcome"),
                                      str(result.get("outcome")))
            balance = result.get("balance")
            tail = f"，余额 {balance:g}" if isinstance(balance, (int, float)) else ""
            parts.append(f"{channel.name} {word}{tail}")
        return "；".join(parts)

    def next_delay(self) -> float:
        """Seconds until the next 08:30-10:00 window slot (Beijing time).

        The minute is seeded by the Beijing date, so every calculation of
        "next run" for the same day agrees — the console display and the
        loop timer never diverge.
        """
        now = datetime.now(timezone(timedelta(hours=8)))
        day = now
        for _ in range(2):
            seed = random.Random(day.strftime("%Y%m%d"))
            minute = seed.randrange(0, 91)  # 08:30 .. 10:00
            target = (day.replace(hour=8, minute=30, second=0, microsecond=0)
                      + timedelta(minutes=minute))
            if target > now:
                return max(60.0, (target - now).total_seconds())
            day = day + timedelta(days=1)
        return 3600.0


class BenchmarkRefreshRunner(ScheduledTask):
    """Scheduled public-benchmark refresh (benchmark_sync).

    Vals Terminal-Bench 4.0 scores are fetched from the public leaderboard
    and written to a user-dir override file that benchmarks.py layers over
    the bundled snapshot, so new scores appear without reinstalling. Only
    models already known to the registry are updated; the override carries
    only the vals-harness entries, so a future bundled update to other
    sources is never frozen by a stale override. Shares
    App.benchmark_refresh_lock with the console button, so the two never
    overlap. Runs once after startup settles, then every
    defaults.benchmark_refresh_hours (0 disables).
    """

    key = "benchmark_refresh"
    label = "能力分刷新"
    description = "抓取公开榜单 Terminal-Bench 4.0 分数，覆盖到本地能力分快照"
    hours_key = "benchmark_refresh_hours"
    FIRST_DELAY = 600.0

    def run_once(self) -> str:
        report = self.app.refresh_benchmarks("scheduled")
        return report.get("summary") or "能力分刷新完成"


class HealthProber:
    """Background liveness probes for channels sitting in a short cooldown.

    A transient cooldown (30s network blip, a few minutes of Retry-After)
    usually outlives the outage; with no traffic the half-open probe never
    fires, so a route idles on later channels until the timer runs out. This
    loop probes such channels directly and clears the cooldown on the first
    success. Quota/plan-window cooldowns, per-model cooldowns, half-open
    channels and long Retry-After freezes are left to their own timers.
    """

    MAX_WORKERS = 3
    BACKOFF_START = 30.0
    BACKOFF_MAX = 300.0

    def __init__(self, app, interval: float, probe_fn=None):
        self.app = app
        self.interval = max(5.0, float(interval))
        self._probe = probe_fn or (lambda channel: app._probe_channel(channel))
        self._attempts: dict[str, int] = {}
        self._deferred: dict[str, float] = {}  # name -> earliest next probe
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="planrouter-prober")
        # Bookkeeping for the scheduled-tasks table (see status()).
        self.last_run_at = 0.0
        self.next_run_at = 0.0
        self.last_summary = ""

    def status(self) -> dict:
        """Row for the console's scheduled-task table."""
        return {
            "key": "probe",
            "label": "冷却探测",
            "description": "对处于短冷却的渠道做可用性探测，成功即提前解除冷却",
            "enabled": self.interval > 0,
            "hours": 0,
            "cycle": f"每 {int(self.interval)} 秒",
            "state": "idle" if self.interval > 0 else "disabled",
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "last_summary": self.last_summary,
            "last_error": "",
        }

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    @staticmethod
    def _quota_bound(reason: str) -> bool:
        """True when the cooldown ends by its window, not by recovery."""
        return (reason in ("daily_quota_reset", "http_404_not_found")
                or "_exhausted" in reason
                or any(m in reason for m in QUOTA_MARKERS))

    def _targets(self, now: float) -> list:
        """Enabled channels whose short, non-quota cooldown is worth one probe."""
        max_cooldown = self.app.config.defaults.probe_max_cooldown_seconds
        targets = []
        for channel in self.app.config.channels:
            if not channel.enabled:
                continue
            with self.app.store.lock:
                state = self.app.store.states.get(channel.name)
                if not state or state.cooldown_until <= now or state.half_open:
                    continue
                remaining = state.cooldown_until - now
                reason = (state.reason or "").lower()
            if remaining > max_cooldown or self._quota_bound(reason):
                continue
            targets.append(channel)
        return targets

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.last_run_at = time.time()
            self.next_run_at = self.last_run_at + self.interval
            self._cycle()

    def _cycle(self) -> None:
        now = time.time()
        targets = [c for c in self._targets(now)
                   if now >= self._deferred.get(c.name, 0.0)]
        if not targets:
            self.last_summary = "当前无短冷却渠道需要探测"
            return
        recovered = 0
        with ThreadPoolExecutor(
                max_workers=min(self.MAX_WORKERS, len(targets))) as pool:
            futures = [(channel, pool.submit(self._probe, channel))
                       for channel in targets]
            for channel, future in futures:
                try:
                    ok = bool(future.result(timeout=60))
                except Exception:
                    ok = False
                if ok:
                    recovered += 1
                    self._attempts.pop(channel.name, None)
                    self._deferred.pop(channel.name, None)
                    with self.app.store.lock:
                        state = self.app.store.states.get(channel.name)
                        if state:
                            state.clear_cooldown()
                else:
                    k = self._attempts.get(channel.name, 0) + 1
                    self._attempts[channel.name] = k
                    self._deferred[channel.name] = time.time() + min(
                        self.BACKOFF_MAX, self.BACKOFF_START * (2 ** (k - 1)))
        self.last_summary = (f"探测 {len(targets)} 个冷却渠道，{recovered} 个恢复"
                             f"，{len(targets) - recovered} 个仍在冷却")


class App:
    def __init__(
        self,
        config: Config,
        config_path: str | Path | None = None,
        state_file: str | Path | None = None,
        subscriptions_file: str | Path | None = None,
        usage_file: str | Path | None = None,
        notifications_file: str | Path | None = None,
        audit_file: str | Path | None = None,
        keys_file: str | Path | None = None,
    ):
        self.config_path = Path(config_path) if config_path else None
        # Point the benchmark loader at a user-dir override file (next to
        # config.json) where the refresh sweep writes freshly fetched public
        # scores; layered on top of the bundled snapshot by benchmarks.py.
        bench_mod.set_override_path(
            self.config_path.parent / "model-scores.override.json"
            if self.config_path else None)
        self.raw = self._read_raw()
        # Channel ids are the stable anchors subscriptions link to; assign and
        # persist them once so later console renames cannot break the links.
        if ensure_channel_ids(self.raw) and self.config_path:
            try:
                tmp = self.config_path.with_name(self.config_path.name + ".tmp")
                tmp.write_text(json.dumps(self.raw, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
                os.replace(tmp, self.config_path)
            except OSError:
                pass
        # Reparse from disk so the in-memory channels carry the ids assigned
        # above; a test-constructed App without a config file keeps its config.
        if self.raw.get("channels"):
            config = parse_config(self.raw)
        self.config = config
        self.store = StateStore(config)
        self.subscriptions_path = Path(subscriptions_file) if subscriptions_file else None
        try:
            self.subscriptions = subs.load_file(self.subscriptions_path)
            self.subscription_settings = subs.load_settings(self.subscriptions_path)
        except (OSError, ValueError):
            self.subscriptions = []
            self.subscription_settings = dict(subs.DEFAULT_SETTINGS)
        # Persisted-state failures are counted here instead of vanishing into
        # `except OSError: pass`; runtime_alerts turns them into an alarm and
        # the next successful write clears it. Created before the recorders so
        # every one of them (and the migration save just below) can report.
        self._write_failure_lock = threading.Lock()
        self.write_failures: dict[str, dict] = {}
        # Subscriptions written before channel ids existed link by name; point
        # them at the ids now, while those names still resolve.
        if self._migrate_subscription_channels(self.subscriptions) \
                and self.subscriptions_path:
            try:
                subs.save_file(self.subscriptions_path, self.subscriptions,
                               self.subscription_settings)
            except OSError as exc:
                self._note_write_failure("subscriptions", exc)
        self._refresh_billing_ranks()
        # Vendor registry (builtin providers.json + optional user overlay in
        # the config dir); a broken overlay must not keep the gateway down.
        try:
            self.providers = providers_mod.load_providers(
                self.config_path.parent if self.config_path else None)
        except (OSError, ValueError):
            self.providers = providers_mod.load_providers(None)
        self.persistence = (StatePersistence(
            state_file, lambda: self.store,
            on_error=self._note_write_failure, on_ok=self._note_write_ok,
        ) if state_file else None)
        if self.persistence:
            self.persistence.restore_from_disk()
        self.usage = UsageRecorder(
            usage_file,
            retention_days=self.config.defaults.usage_retention_days,
            on_error=self._note_write_failure,
            on_ok=self._note_write_ok,
        ) if usage_file else None
        self.notifier = ReminderService(
            self.subscriptions_path, notifications_file,
            lambda: self._sub_records(), lambda: self.subscription_settings,
            get_health=lambda: self.runtime_alerts(),
            on_write_error=self._note_write_failure,
            on_write_ok=self._note_write_ok,
        ) if notifications_file else None
        # 中心服务客户端：同步注册表/情报、更新检查、心跳、预填、消息中心。
        # center.json（设置）与 center.state.json（状态/缓存）放在配置目录；
        # 无配置文件（如测试直接构造 App）时不启用，客户端相关功能整体
        # 降级为"未配置中心"。
        if self.config_path:
            self.center = CenterClient(
                self.config_path.parent / "center.json",
                self.config_path.parent / "center.state.json",
                config_path=self.config_path,
                get_raw=lambda: self.raw,
                apply_config=lambda raw: self.apply_config(raw, persist=True),
                get_webhook=lambda: self.subscription_settings,
                version=__version__,
                log=self._note_center,
            )
        else:
            self.center = None
        self.audit = RequestLog(audit_file) if audit_file else None
        # Member keys for small-team attribution; absent file -> no members,
        # and the gateway behaves exactly as before.
        self.keys = KeyStore(keys_file) if keys_file else None
        # Sliding-window timestamps for per-member rpm caps (see
        # _member_limited). Daily/monthly quotas read the usage recorder
        # instead, so they survive restarts without extra bookkeeping.
        self.member_rpm: dict[str, list[float]] = {}
        self._member_rpm_lock = threading.Lock()
        # Billing bucket of the last served request (free/plan/payg) for the
        # free→paid transition alert; in-memory by design, see _note_billing.
        self._billing_state = {"billing": None, "prev": None, "at": 0.0}
        # Vendor-reported free quota from the last alignment sweep. Kept next
        # to the config so a restart keeps showing real numbers, and so the
        # free-quota gate can see consumption the gateway itself did not make.
        self.vendor_quota: dict = {}
        self.vendor_quota_at = ""
        # Per-channel epoch of the last successful vendor quota read. A
        # channel whose vendor-derived numbers are older than one sweep
        # interval is frozen whole (free_quota_status): the budget only
        # extrapolates safely while the sweep that seeded it keeps running —
        # a stalled sweep must stop dispatch instead of spending against
        # numbers nobody has refreshed while the vendor grant may be gone.
        self.vendor_synced: dict = {}
        # Self-metered free budget per channel: the vendor's remaining figure
        # at the last sweep plus the usage counter's position at that moment
        # (see _free_budget_for). Volcengine never errors once free quota runs
        # dry with balance in the account — it silently bills — so between
        # sweeps this seed is what lets the dispatch gate stop the model, with
        # a provider margin held back (FREE_RESERVE_TOKENS) so a single large
        # request cannot dip into the balance.
        self.free_budget: dict = {}
        # Vendor-side plan usage for the subscriptions page (Volcengine
        # Coding/Agent Plan via GetPersonalPlan + per-plan usage APIs).
        # Cached per credential pair with a TTL and persisted like
        # vendor_quota: a restart shows the snapshot and refreshes on the
        # first page load. A channel-bound AK/SK and the vendor-level
        # fallback therefore each get their own cache slot.
        self.plan_entries: dict[str, dict] = {}
        self._plan_lock = threading.Lock()
        # Stale-while-revalidate workers: expired plan/usage snapshots are
        # served immediately and refreshed off-thread, so a console poll or
        # a 429-cooldown lookup never waits on the vendor management plane.
        self._plan_pool = ThreadPoolExecutor(max_workers=2,
                                             thread_name_prefix="vendor-refresh")
        self._load_vendor_quota()
        self.catalog_refresher: CatalogRefresher | None = None
        self.maintenance_runner: "MaintenanceRunner | None" = None
        self.free_align_runner: "FreeAlignRunner | None" = None
        self.free_verify_runner: "FreeVerifyRunner | None" = None
        self.watch_runner: "WatchRunner | None" = None
        self.modelscope_checkin_runner: "ModelScopeCheckinRunner | None" = None
        self.benchmark_refresh_runner: "BenchmarkRefreshRunner | None" = None
        self.maintenance_reports: list[dict] = self._load_report_history("maintenance.jsonl")
        self.free_align_reports: list[dict] = self._load_report_history("freealign.jsonl")
        self.maintenance_lock = threading.Lock()
        # One config-patching sweep at a time: the maintenance pass and the
        # free-quota alignment sweep both rewrite channel mappings through
        # apply_config (read self.raw -> write file -> swap), so a second pass
        # would silently drop the other's mapping. Scheduled runners and the
        # console buttons share this gate; a contender arriving mid-run
        # queues and runs when the holder releases, instead of being dropped
        # with a busy reply while a sweep runs for minutes.
        self.config_apply_lock = SerialGate()
        # Same for the free-pool verification sweep (scheduled + button).
        self.free_verify_lock = SerialGate()
        # Same for the source-watch sweep (scheduled + button).
        self.watch_lock = SerialGate()
        # Same for the public-benchmark refresh sweep (scheduled + button).
        self.benchmark_refresh_lock = SerialGate()
        # Verified maintenance-model queue (see maintenance.probe_maintenance_models).
        self.maintenance_pool = maint.load_maintenance_pool(self)
        self.maintenance_pool_lock = threading.Lock()

    def log_request(self, **fields) -> None:
        if self.audit:
            self.audit.record(**fields)
        # 诊断计数（T4.3）：失败请求按厂家聚合错误类型，只记厂家 id + 错误
        # 类型 + 次数，不含 key 与地址；随心跳聚合上报（用户显式开启）。
        if self.center is not None and fields.get("status") not in (None, "ok"):
            try:
                self.center.note_upstream_error(
                    fields.get("channel") or "",
                    str(fields.get("status") or ""),
                    fields.get("http_status"),
                )
            except Exception:
                pass

    def request_log_payload(self, limit: int = 50, channel: str | None = None) -> dict:
        if not self.audit:
            return {"entries": [], "path": None, "size": 0,
                    "note": "请求审计未启用（启动时未指定 --audit-file）"}
        return {
            "entries": self.audit.recent(limit, channel=channel),
            "path": str(self.audit.path),
            "size": self.audit.size(),
            "note": "仅记录选路元数据，不含 prompt、密钥与模型输出",
        }

    def diagnostics_payload(self) -> dict:
        """Sanitized troubleshooting bundle for the console's export button.

        Every source is already masked (public_config, store snapshot, audit
        metadata), so the bundle is safe to attach to a bug report.
        """
        return {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "config": self.public_config(),
            "channels": self.store.snapshot()["channels"],
            "requests": self.request_log_payload(200),
        }

    def start_background(self) -> None:
        if self.persistence:
            self.persistence.start()
        if self.usage:
            self.usage.start()
        if self.notifier:
            self.notifier.start()
        # 中心服务客户端：注册表同步/情报/更新/心跳/预填（默认开启，
        # 失败静默降级，不影响网关转发）。
        if self.center:
            self.center.start()
        # The task objects exist even when a sweep is switched off, so the
        # console can still list it (and show the last manual run).
        self.catalog_refresher = CatalogRefresher(self)
        self.maintenance_runner = MaintenanceRunner(self)
        self.free_align_runner = FreeAlignRunner(self)
        self.free_verify_runner = FreeVerifyRunner(self)
        self.watch_runner = WatchRunner(self)
        self.modelscope_checkin_runner = ModelScopeCheckinRunner(self)
        self.benchmark_refresh_runner = BenchmarkRefreshRunner(self)
        if self.config.defaults.catalog_refresh_hours:
            self.catalog_refresher.start()
        if self.config.defaults.maintenance_hours:
            self.maintenance_runner.start()
        if self.config.defaults.free_align_hours:
            self.free_align_runner.start()
        if self.config.defaults.free_verify_hours:
            self.free_verify_runner.start()
        if self.config.defaults.watch_hours:
            self.watch_runner.start()
        if self.config.defaults.modelscope_checkin_hours:
            self.modelscope_checkin_runner.start()
        if self.config.defaults.benchmark_refresh_hours:
            self.benchmark_refresh_runner.start()
        # Created even when switched off, so the console can list the sweep;
        # the thread only runs with a positive probe_interval_seconds.
        self.health_prober = HealthProber(self, self.config.defaults.probe_interval_seconds)
        if self.config.defaults.probe_interval_seconds > 0:
            self.health_prober.start()

    def stop_background(self) -> None:
        if self.maintenance_runner:
            self.maintenance_runner.stop()
        if self.free_align_runner:
            self.free_align_runner.stop()
        if self.free_verify_runner:
            self.free_verify_runner.stop()
        if self.watch_runner:
            self.watch_runner.stop()
        if self.modelscope_checkin_runner:
            self.modelscope_checkin_runner.stop()
        if self.catalog_refresher:
            self.catalog_refresher.stop()
        if self.health_prober:
            self.health_prober.stop()
        if self.notifier:
            self.notifier.stop()
        if self.center:
            self.center.stop()
        if self.usage:
            self.usage.stop()
        if self.persistence:
            self.persistence.stop()

    def record_usage(self, channel: str, success: bool, tokens: int = 0,
                     model: str = "", input_tokens: int = 0,
                     cached_tokens: int = 0, h=None) -> None:
        if self.usage:
            self.usage.record(channel, success, tokens, model=model,
                              input_tokens=input_tokens, cached_tokens=cached_tokens,
                              user=getattr(h, "user_key", ""))

    def apply_quota(self, name: str, headers) -> None:
        """Observe upstream quota headers; cool early when the window is empty."""
        quota = parse_quota(headers)
        if not quota:
            return
        now = time.time()
        with self.store.lock:
            state = self.store.states.get(name)
            if not state:
                return
            state.quota = quota
            if not self.config.defaults.preempt_quota:
                return
            seconds = quota_exhaustion_wait(
                quota, now,
                self.config.defaults.cooldown_minutes * 60,
                self.config.defaults.max_cooldown_hours * 3600,
            )
            if seconds:
                state.cooldown_until = now + seconds
                state.reason = "quota-preempted"
                state.half_open = False
                state.last_error = "上游额度头显示本周期已用尽，提前切换"
                self.log_request(
                    api="quota", model="", mode="preempt",
                    reason="upstream-quota-exhausted", channel=name,
                    status="cooldown", cooldown_seconds=round(seconds, 1),
                )

    def report_payload(self, month: str | None = None) -> dict:
        days = {}
        if self.usage:
            with self.usage.lock:
                days = {day: dict(buckets) for day, buckets in self.usage.data["days"].items()}
        return build_report(self._sub_records(), days, month)

    def member_costs(self, month: str | None = None) -> dict:
        days: dict = {}
        users: dict = {}
        if self.usage:
            with self.usage.lock:
                days = {day: dict(buckets) for day, buckets in self.usage.data["days"].items()}
                users = {name: dict(bucket)
                         for name, bucket in (self.usage.data.get("users") or {}).items()}
        return build_member_costs(self._sub_records(), users, days, month)

    def blocked_channels(self) -> dict[str, str]:
        """Channels whose every linked subscription has expired."""
        if not self.subscription_settings.get("block_expired_channels", True):
            return {}
        expired: dict[str, str] = {}
        usable: set[str] = set()
        for record in self.subscriptions:
            status = subs.enrich(record)["status"]
            label = self.subscription_label(record)
            for ref in record.get("channels") or []:
                channel = self.config.channel_by_ref(str(ref))
                # Downstream routing indexes channels by name, so every
                # reference is resolved before it leaves this method.
                name = channel.name if channel else str(ref)
                if status == "expired":
                    expired[name] = (
                        f"订阅已过期：{label} / "
                        f"{record['account'] or '—'} / {record['plan']}"
                    )
                else:
                    usable.add(name)
        return {name: reason for name, reason in expired.items() if name not in usable}

    def provider_mode(self, provider: str, mode: str) -> dict:
        """Registry mode metadata (free quota baseline, credential envs, ...)."""
        for entry in self.providers:
            if entry.get("id") == provider:
                return (entry.get("modes") or {}).get(mode) or {}
        return {}

    def _vendor_quota_path(self):
        return self.config_path.parent / "vendorquota.json" if self.config_path else None

    def _load_vendor_quota(self) -> None:
        path = self._vendor_quota_path()
        if not path:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        channels = data.get("channels")
        if isinstance(channels, dict):
            self.vendor_quota = {
                str(name): entry for name, entry in channels.items()
                if isinstance(entry, dict)}
        self.vendor_quota_at = str(data.get("at") or "")
        budget = data.get("budget")
        if isinstance(budget, dict):
            self.free_budget = {
                str(name): entry for name, entry in budget.items()
                if isinstance(entry, dict)}
        synced = data.get("synced")
        if isinstance(synced, dict):
            self.vendor_synced = {
                str(name): float(ts) for name, ts in synced.items()
                if isinstance(ts, (int, float)) and not isinstance(ts, bool)}
        elif self.vendor_quota_at:
            # Legacy file: one shared stamp covers every channel it carries,
            # so the freshness gate works across the upgrade instead of
            # trusting an undated snapshot forever.
            ts = _quota_at_epoch(self.vendor_quota_at)
            if ts is not None:
                self.vendor_synced = {
                    name: ts for name in {*self.vendor_quota, *self.free_budget}}
        plans = data.get("plans")
        if isinstance(plans, dict):
            # Show the persisted snapshots right after a restart; the first
            # page load still re-fetches (freshness beats availability).
            self.plan_entries = {
                str(key): {"snapshot": snap, "error": "", "ts": 0.0}
                for key, snap in plans.items() if isinstance(snap, dict)}
        elif isinstance(data.get("plan"), dict):
            # Pre-cred-binding single snapshot from the first rollout.
            self.plan_entries = {"coding:legacy": {
                "snapshot": data["plan"], "error": "", "ts": 0.0}}

    def _save_vendor_quota(self) -> None:
        path = self._vendor_quota_path()
        if not path:
            return
        plans = {key: entry.get("snapshot") for key, entry in
                 self.plan_entries.items() if entry.get("snapshot")}
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"at": self.vendor_quota_at, "channels": self.vendor_quota,
                 "budget": self.free_budget, "synced": self.vendor_synced,
                 "plans": plans},
                ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            # Quota display must never break the sweep, but a full disk must
            # not stay invisible either: runtime_alerts reports the counter.
            self._note_write_failure("vendor_quota", exc)
        else:
            self._note_write_ok("vendor_quota")

    def _note_write_failure(self, kind: str, exc) -> None:
        """Remember a failed persisted write for runtime_alerts."""
        with self._write_failure_lock:
            row = self.write_failures.setdefault(kind, {"count": 0})
            row["count"] = int(row.get("count") or 0) + 1
            row["last_at"] = time.time()
            row["last_error"] = str(exc)[:160]

    def _note_center(self, text: str) -> None:
        """中心服务同步失败（断网/超时/验签失败）的告警通道。

        约定与 CenterClient.log 一致：空文本 = 同步恢复，清除告警；
        非空文本按写失败计入 runtime_alerts，坏中心看得见，恢复不用手清。
        """
        if text:
            self._note_write_failure("center", OSError(text))
        else:
            self._note_write_ok("center")

    def _note_write_ok(self, kind: str) -> None:
        """A successful write clears the alarm for that artefact."""
        with self._write_failure_lock:
            self.write_failures.pop(kind, None)

    # -------------------------------------------------- vendor plan usage
    def _plan_creds_for(self, record: dict) -> tuple[str, str, str]:
        """(kind, ak, sk) for a subscription row: bound channel first.

        Both the vendor gate and the plan kind come from the linked channel
        (its provider plus business line / billing mode), never from free text
        on the subscription: a subscription has no vendor of its own.

        Plan endpoints exist for Volcengine (IAM-signed management plane),
        OpenCode Go (inference gateway, same oc_sk_ key as chat), Zhipu
        Coding Plan (same key as chat), Bailian Token Plan (host-level
        ``bl`` CLI console session, so no key is required) and Hunyuan
        Token Plan (TokenHub management plane, CAM SecretId/Key). Rows of
        the pay-as-you-go modes must not get decorated with (or fail on) a
        plan call.

        If the record has an explicit `account_id`, we look up the account
        by id from config.accounts and use its credentials; otherwise the
        channel credential chain applies.
        """
        linked = self._linked_channels(record)
        oc = next((c for c in linked if c.provider == "opencode"), None)
        if oc:
            # Only the Go subscription reports usage; the pay-as-you-go
            # endpoint has no usage route.
            line = (oc.business_line or oc.mode or "").strip().lower()
            if line != "agent":
                return "", "", ""
            from .accounts import resolve_creds
            creds: dict = resolve_creds(oc, self.config)
            key = str(creds.get("api_key") or oc.api_key or "")
            return "opencode", key, ""
        hy = next((c for c in linked if c.provider == "hunyuan"), None)
        if hy:
            # Only the Token Plan (agent) subscription has a package; the
            # pay-as-you-go endpoint reports no plan window.
            line = (hy.business_line or hy.mode or "").strip().lower()
            if line != "agent":
                return "", "", ""
            from .accounts import resolve_creds
            creds: dict = resolve_creds(hy, self.config)
            ak = str(creds.get(hunyuan_quota.ID_ENV) or "")
            sk = str(creds.get(hunyuan_quota.KEY_ENV) or "")
            return "hunyuan", ak, sk
        zp = next((c for c in linked if c.provider == "zhipu"), None)
        if zp:
            # The Coding Plan reports usage to the same key the channel
            # sends upstream; the pay-as-you-go key has no coding window.
            line = (zp.business_line or zp.mode or "").strip().lower()
            if line != "coding":
                return "", "", ""
            from .accounts import resolve_creds
            creds: dict = resolve_creds(zp, self.config)
            key = str(creds.get("api_key") or zp.api_key or "")
            return "zhipu", key, ""
        bl = next((c for c in linked if c.provider == "bailian"), None)
        if bl:
            # The Token Plan usage comes from the host-level bl CLI console
            # session, so a RAM key is optional; when present it only names
            # the cache slot ("console" otherwise).
            line = (bl.business_line or bl.mode or "").strip().lower()
            if line != "agent":
                return "", "", ""
            from .accounts import resolve_creds
            creds: dict = resolve_creds(bl, self.config)
            ak = str(creds.get(bailian_quota.AK_ENV) or "") or "console"
            return "bailian", ak, ""
        volc = next((c for c in linked if c.provider == "volcengine"), None)
        if not volc:
            return "", "", ""
        line = (volc.business_line or volc.mode or "").strip().lower()
        kind = "agent" if line == "agent" else "coding"

        # Check for explicit account_id first (new model)
        account_id = str(record.get("account_id") or "").strip()
        if account_id:
            account = self._find_account(account_id)
            if account and account.get("credentials"):
                ak = str(account["credentials"].get(ark_quota.AK_ENV) or "")
                sk = str(account["credentials"].get(ark_quota.SK_ENV) or "")
                return kind, ak, sk

        # Full chain: channel.creds > account.credentials > vendor slot.
        from .accounts import resolve_creds
        creds: dict = resolve_creds(volc, self.config)
        ak = str(creds.get(ark_quota.AK_ENV) or "")
        sk = str(creds.get(ark_quota.SK_ENV) or "")
        return kind, ak, sk

    def _cached_vendor(self, cache_key: str, fetch) -> dict:
        """TTL-cached vendor snapshot, shared by plans and channel usage.

        ``fetch`` takes no arguments and returns the snapshot dict. A failure
        is remembered for PLAN_ERROR_TTL and keeps the last good snapshot
        (marked stale) so the console shows both the numbers and the error
        that needs fixing; a success is persisted through vendorquota.json.

        Stale-while-revalidate: a cleanly expired snapshot is served at once
        and refreshed on a background thread — only a key with no snapshot
        at all (first run, new creds) or one shadowed by an error still
        fetches inline, because those callers want the authoritative answer
        in this very response.
        """
        submitted = False
        with self._plan_lock:
            now = time.time()
            entry = self.plan_entries.get(cache_key) or {}
            snapshot, error, ts = (entry.get("snapshot"), entry.get("error", ""),
                                   entry.get("ts", 0.0))
            if now - ts < (PLAN_TTL if snapshot else PLAN_ERROR_TTL):
                if error and not snapshot:
                    return {"error": error}
                if error and snapshot:
                    return {**snapshot, "stale": True, "error": error}
                return snapshot
            if snapshot and not error:
                # Serve the stale snapshot now and refresh off-thread. The
                # submit happens outside the lock because the refresh takes
                # the lock itself — a synchronous pool (tests) must be able
                # to complete within this call.
                if not entry.get("refreshing"):
                    self.plan_entries[cache_key] = {**entry, "refreshing": True}
                    submitted = True
        if submitted:
            self._plan_pool.submit(self._refresh_vendor, cache_key, fetch)
        if snapshot and not error:
            # Re-read after submit: a synchronous refresh that already
            # landed surfaces its error here; a landed success serves clean.
            entry = self.plan_entries.get(cache_key) or {}
            if entry.get("ts", 0.0) > ts and not entry.get("error"):
                return entry.get("snapshot") or snapshot
            out = {**(entry.get("snapshot") or snapshot), "stale": True}
            if entry.get("error"):
                out["error"] = entry["error"]
            return out
        try:
            plan = fetch()
        except Exception as exc:
            error = str(exc)[:160]
            self.plan_entries[cache_key] = {"snapshot": snapshot,
                                            "error": error, "ts": now}
            if snapshot:
                return {**snapshot, "stale": True, "error": error}
            return {"error": error}
        snapshot = {**plan, "fetched_at": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S")}
        self.plan_entries[cache_key] = {"snapshot": snapshot, "error": "",
                                        "ts": now}
        self._save_vendor_quota()
        return snapshot

    def _refresh_vendor(self, cache_key: str, fetch) -> None:
        """Background half of the stale-while-revalidate vendor cache.

        Runs on the refresh pool without holding the lock; a failure keeps
        the last good snapshot and is remembered for PLAN_ERROR_TTL, which
        also routes the next read back onto the inline (authoritative)
        path instead of another background attempt.
        """
        try:
            plan = fetch()
        except Exception as exc:
            with self._plan_lock:
                entry = self.plan_entries.get(cache_key) or {}
                self.plan_entries[cache_key] = {"snapshot": entry.get("snapshot"),
                                                "error": str(exc)[:160],
                                                "ts": time.time()}
            return
        snapshot = {**plan, "fetched_at": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S")}
        with self._plan_lock:
            self.plan_entries[cache_key] = {"snapshot": snapshot, "error": "",
                                            "ts": time.time()}
        self._save_vendor_quota()

    def plan_for(self, record: dict) -> dict | None:
        """Vendor plan usage for one subscription row, TTL-cached per creds.

        Returns None when the row has no usable credentials: OpenCode Go and
        Zhipu Coding Plan rows only need the channel key, Bailian Token Plan
        runs on the host ``bl`` CLI console session (no key at all), while
        Volcengine and Hunyuan rows need AK and SK; a fetch failure returns
        ``{"error": ...}`` (plus the last good snapshot marked stale when
        one exists). Snapshots persist through vendorquota.json.
        """
        kind, ak, sk = self._plan_creds_for(record)
        if not ak or (kind not in ("opencode", "zhipu", "bailian") and not sk):
            return None
        if kind == "opencode":
            fetch = lambda: opencode_quota.fetch_go_plan(ak)
        elif kind == "zhipu":
            fetch = lambda: zhipu_quota.fetch_coding_plan(ak)
        elif kind == "bailian":
            fetch = bailian_quota.fetch_token_plan
        elif kind == "hunyuan":
            fetch = lambda: hunyuan_quota.fetch_token_plan(ak, sk)
        else:
            ark_fetch = (ark_quota.fetch_agent_plan if kind == "agent"
                         else ark_quota.fetch_coding_plan)
            fetch = lambda: ark_fetch(ak, sk)
        key = f"{kind}:{hashlib.sha1(f'{ak}:{sk}'.encode()).hexdigest()[:12]}"
        return self._cached_vendor(key, fetch)

    def channel_usage(self, channel) -> dict | None:
        """Pay-as-you-go usage for one channel key (OpenRouter, DeepSeek).

        The key the channel already sends upstream also authenticates the
        vendor's usage endpoint, so the existing credential chain is enough;
        None means there is nothing to show (no fetcher for the vendor, or
        no key configured), which the channel card renders as no row.
        Management-plane wallets (百炼/腾讯云) instead take the AK/SK pair
        from the same chain, because the wallet is account-wide and the
        data-plane key cannot sign it.
        """
        from .accounts import resolve_creds
        creds: dict = resolve_creds(channel, self.config)
        mgmt = mode_meta.MGMT_WALLET_FETCHERS.get(channel.provider)
        if mgmt is not None:
            fetch, ak_env, sk_env = mgmt
            ak = str(creds.get(ak_env) or "")
            sk = str(creds.get(sk_env) or "")
            if not ak or not sk:
                return None
            cache_key = (f"wallet:{channel.provider}:"
                         + hashlib.sha1(f"{ak}:{sk}".encode()).hexdigest()[:12])
            return self._cached_vendor(cache_key, lambda: fetch(ak, sk))
        fetch = mode_meta.KEY_API_FETCHERS.get(channel.provider)
        if fetch is None:
            return None
        key = str(creds.get("api_key") or channel.api_key or "")
        if not key:
            return None
        cache_key = (f"usage:{channel.provider}:"
                     + hashlib.sha1(key.encode()).hexdigest()[:12])
        return self._cached_vendor(cache_key, lambda: fetch(key))

    def channels_usage_payload(self) -> dict:
        """Per-channel pay-as-you-go usage for the channel config page."""
        usage = {}
        for channel in self.config.channels:
            snapshot = self.channel_usage(channel)
            if snapshot:
                self._ledger_money_snapshot(channel, snapshot)
                usage[channel.name] = snapshot
        return {"usage": usage}

    def _ledger_money_snapshot(self, channel, snapshot: dict) -> None:
        """Persist an account-level money balance as a vendor_quota row.

        Voucher channels (free_kind=voucher) hold money, not tokens: when
        the vendor's balance fetch returns structured numbers (moonshot's
        cash/voucher split), they land in vendorquota.json under the
        reserved ``_money`` key so the ledger survives a restart. Wire rows
        keep their token shape; ``_money`` can never collide with a wire id.
        vendor_synced stays untouched — the stale freeze keys on the token
        sweep's cadence, not on console page visits.
        """
        balance = snapshot.get("balance")
        if not isinstance(balance, dict) or not balance:
            return
        mode = self.provider_mode(channel.provider, channel.mode)
        if mode_meta.free_kind_of(channel.provider, mode or {}) != "voucher":
            return
        entry = {"kind": "money",
                 "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 **balance}
        row = self.vendor_quota.setdefault(str(channel.name), {})
        if row.get("_money") == entry:
            return
        row["_money"] = entry
        self._save_vendor_quota()

    def _plan_reset_cooldown(self, channel, now: float) -> tuple[float, str]:
        """Cooldown seconds + reason when a linked plan window is exhausted.

        Reads the TTL-cached vendor snapshot (at most one management-plane
        call per PLAN_TTL), so a 429 on a plan whose window already reads
        used >= quota cools the whole channel until that window resets
        instead of the fixed cooldown_minutes — no more probing a dead plan
        every five hours. Returns (0.0, "") when nothing is exhausted or no
        future reset is scheduled, keeping the parse_cooldown verdict.
        """
        ref = channel_ref(channel)
        best, level = 0.0, ""
        for record in self.subscriptions:
            refs = record.get("channels") or []
            if ref not in refs and channel.name not in refs:
                continue
            plan = self.plan_for(record) or {}
            for window in plan.get("windows") or []:
                if not isinstance(window, dict):
                    continue
                quota, used = window.get("quota"), window.get("used")
                percent = window.get("percent")
                exhausted = (
                    isinstance(quota, (int, float)) and isinstance(used, (int, float))
                    and quota > 0 and used >= quota
                ) or (isinstance(percent, (int, float)) and percent >= 100)
                if not exhausted:
                    continue
                try:
                    reset = float(window.get("reset_ts") or 0)
                except (TypeError, ValueError):
                    continue
                if reset <= now:
                    continue  # no reset scheduled: the fixed cooldown stands
                best = max(best, reset - now)
                level = str(window.get("level") or "plan")
        if best <= 0.0:
            return 0.0, ""
        return min(best, PLAN_RESET_CAP_HOURS * 3600.0), f"plan_{level}_exhausted"

    def _disable_account_channel(self, channel, detail: str) -> bool:
        """Retire a channel the vendor says is out of money, and persist it.

        A balance/arrears rejection never heals on its own — no reset header
        will arrive — so cooling it for hours only guarantees the same 4xx
        again (and again). This flips the channel's `enabled` flag off through
        the same apply_config path the console's on/off toggle uses, so the
        change lands in router.json and survives restarts; re-enabling stays
        a deliberate operator action after the account is topped up. Returns
        True when this call flipped the flag (False: already disabled).
        """
        channels = list(self.raw.get("channels") or [])
        hit = False
        for entry in channels:
            if isinstance(entry, dict) and entry.get("name") == channel.name:
                if entry.get("enabled") is False:
                    return False
                entry["enabled"] = False
                hit = True
                break
        if not hit:
            return False
        try:
            self.apply_config({**self.raw, "channels": channels})
        except (ValueError, OSError):
            # Persisting failed (config edited concurrently, disk trouble):
            # keep the runtime ban so traffic stops, the console still shows
            # the state, and the next failure retries the persist.
            pass
        try:
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = f"渠道已停用（余额不足）: {detail}"
        except KeyError:
            pass
        return True

    def _model_quota_for(self, channel, wire: str, exposed: str = ""):
        """This wire's per-model one-time pack, or None.

        The exposed name wins over the upstream wire id (same resolution
        order as `tiers`), so an operator renaming an exposed model keeps the
        pack bound to the friendly name they configured.
        """
        packs = channel.model_quota or {}
        for key in (exposed, wire):
            if key and key in packs:
                return packs[key]
        return None

    def free_quota_spec(self, channel, wire: str = "", exposed: str = ""):
        """Resolved free-tier allowance for one model on this channel.

        Only models that actually count as free are capped: a paid channel
        sharing the same vendor must never be throttled by the free baseline.
        Priority: the channel's per-model one-time pack (`model_quota`), then
        the channel-wide `free_quota`, then the vendor registry baseline.
        """
        tiers = channel.tiers or {}
        explicit = tiers.get(exposed) if exposed else None
        explicit = explicit or (tiers.get(wire) if wire else None)
        if explicit:
            # A mixed channel may expose paid models next to free ones; the
            # per-model override decides, not the channel-wide tier.
            eligible = explicit == "free"
        else:
            eligible = channel.tier == "free"
            # Same OpenRouter rule as model_tier: without a ":free" wire id
            # the call bills the account and must skip the free baseline.
            if eligible and channel.provider == "openrouter":
                eligible = is_free_model(wire)
        if not eligible:
            return None
        pack = self._model_quota_for(channel, wire, exposed)
        if pack is not None:
            return pack
        if channel.free_quota is not None:
            return channel.free_quota
        raw = self.provider_mode(channel.provider, channel.mode).get("free_quota")
        try:
            return parse_free_quota(raw)
        except ValueError:
            return None

    def _vendor_quota_for(self, channel, wire: str) -> dict:
        """Vendor-reported quota row for this wire (exact model id only).

        A dated variant (``deepseek-v4.1-flash-1017``) does not inherit the
        family row (``deepseek-v4.1-flash``): the vendor answers HTTP 403 for
        variants outside its free list, so assuming a shared grant only bought
        a needless upstream error. Stale ``auto`` ids are repointed by the
        alignment sweep instead.
        """
        return (self.vendor_quota.get(channel.name) or {}).get(wire) or {}

    def _money_snapshot_for(self, channel) -> dict:
        """Account-level money balance row (voucher channels), or {}.

        Written by the console's usage refresh (moonshot cash/voucher split)
        under the reserved ``_money`` key; a registered face value without a
        vendor API (xiaomi, anthropic) has no row here — the ledger then
        shows only the grant and the expiry.
        """
        row = (self.vendor_quota.get(channel.name) or {}).get("_money")
        return row if isinstance(row, dict) else {}

    def _seed_free_budget(self, quota_report: dict) -> None:
        """Reset the self-metered free budget from fresh sweep numbers.

        Each seeded channel keeps the vendor's remaining figure plus the
        usage counter's position at seed time; the dispatch gate subtracts
        the counter's growth since (see _free_budget_for). A channel whose
        sweep failed keeps its previous seed — its quota really was spent,
        only the re-read failed.
        """
        for name, rows in (quota_report or {}).items():
            remaining: dict[str, float] = {}
            for wire, row in rows.items():
                value = (row or {}).get("remaining")
                if isinstance(value, (int, float)) \
                        and not isinstance(value, bool):
                    remaining[str(wire)] = float(value)
            if not remaining:
                continue
            spent0 = ({wire: int(self.usage.model_usage(
                str(name), wire, "total").get("tokens") or 0)
                for wire in remaining}
                if self.usage is not None else {})
            self.free_budget[str(name)] = {"remaining": remaining,
                                           "spent0": spent0}

    def _free_budget_for(self, channel, wire: str) -> float | None:
        """Vendor-seeded free tokens left for this wire, or None.

        The seed (vendor remaining + counter position at the last sweep)
        persists in vendorquota.json and the counter in usage.json, so the
        subtraction is restart-safe with no per-request bookkeeping. The
        counter sums raw prompt tokens — cache hits included — so it reads
        high against vendor billing: the gate closes early rather than
        letting a dry model silently bill the account balance.
        """
        entry = self.free_budget.get(channel.name)
        if not isinstance(entry, dict) or self.usage is None:
            return None
        remaining = entry.get("remaining") or {}
        if wire not in remaining:
            return None
        base = int((entry.get("spent0") or {}).get(wire) or 0)
        total = int(self.usage.model_usage(
            channel.name, wire, "total").get("tokens") or 0)
        return float(remaining[wire]) - max(0, total - base)

    def free_quota_status(self, channel, wire: str, exposed: str = "") -> dict | None:
        """Local usage vs the free allowance; exhausted means "never route it".

        Three signals, freshest first: the self-metered budget (the vendor's
        remaining figure at the last sweep minus what the gateway has routed
        since), the raw vendor snapshot, and the local spec counter. A vendor
        that silently bills the account balance once free quota runs dry
        (Volcengine) never errors, so between sweeps the extrapolated budget
        is the only signal that can stop dispatch before money moves.

        Voucher channels (``free_kind=voucher``) ledger money instead of
        tokens: the persisted account balance (``_money`` snapshot) closes
        dispatch at zero, the registered face value documents the grant and
        carries the expiry, and both are reported alongside the token side
        (``grant_money`` / ``balance_money`` / ``balance_tokens``) so the
        console can split the columns.

        The two vendor-observed signals additionally honour the provider's
        safety margin (FREE_RESERVE_TOKENS, default FREE_RESERVE_DEFAULT_TOKENS
        for providers without an entry): Volcengine counts as exhausted while
        100k tokens still remain on that wire, because a single request can
        spend ~90k and crossing zero there is billed silently. The margin is
        per wire — a depleted model cools alone and its siblings on the same
        channel keep serving — never a channel-wide freeze. The local spec
        counter stays a raw comparison — its baseline is a hand-written
        estimate and its total already reads high (cached prompt tokens are
        summed as billed ones), so stacking the margin on top would close
        paid-safe models for a whole sweep cycle.

        One deliberate channel-wide freeze exists: while the vendor numbers
        are older than one sweep interval (defaults.free_align_hours), every
        wire of that channel counts as exhausted. The budget only
        extrapolates safely while the sweep that seeded it keeps running;
        when the sweep stalls (vendor command failing, host asleep) the
        extrapolation keeps spending against numbers nobody has refreshed —
        exactly the window in which a grant may already be gone and the
        vendor keeps serving silently. The freeze lifts on the next
        successful sweep. The trip point is one sweep interval plus a small
        grace (STALE_FREEZE_GRACE) so a healthy cycle never trips it, and
        channels the sweep never refreshes (no vendor command) carry live
        signals only and are never frozen, and neither is anything while the
        sweep is disabled.
        """
        spec = self.free_quota_spec(channel, wire, exposed)
        vendor = self._vendor_quota_for(channel, wire)
        budget = self._free_budget_for(channel, wire)
        reserve = FREE_RESERVE_TOKENS.get(channel.provider,
                                          FREE_RESERVE_DEFAULT_TOKENS)
        if spec is None and not vendor and budget is None \
                and not self._money_snapshot_for(channel):
            return None
        used = (self.usage.model_usage(channel.name, wire, spec.period)
                if spec is not None and self.usage is not None else {})
        tokens_used = int(used.get("tokens") or 0)
        # Vendors bill answered requests only; failed calls stay out of the cap.
        requests_used = int(used.get("success") or 0)
        remaining_tokens = (None if spec is None or spec.tokens is None
                            else max(0, spec.tokens - tokens_used))
        remaining_requests = (None if spec is None or spec.requests is None
                              else max(0, spec.requests - requests_used))
        local_exhausted = bool(
            (spec is not None and spec.tokens is not None
             and tokens_used >= spec.tokens)
            or (spec is not None and spec.requests is not None
                and requests_used >= spec.requests)
        )
        # One-time packs die by calendar, not by spend: a grant that lapsed
        # yesterday must stop routing exactly like a drained one, even while
        # the ledger still shows headroom.
        expired = bool(spec is not None and spec.expires is not None
                       and datetime.now() >= spec.expires)
        # Freshness order: budget > vendor snapshot > local spec counter. The
        # snapshot beats the counter because the gateway sums raw prompt
        # tokens (cache hits included) and routinely reads higher than what
        # the vendor actually debited — letting the local counter override a
        # positive vendor remainder once kept dead models blocked for a whole
        # sweep cycle. The budget beats the snapshot because it also counts
        # what the gateway routed since the sweep: the snapshot alone goes
        # stale for a whole sweep interval, exactly when silent billing
        # happens.
        vendor_remaining = vendor.get("remaining")
        # Money ledger (voucher channels): the vendor's account balance is
        # the money-side truth; a registered face value (spec.amount) only
        # documents the grant and feeds the expiry gate — the gateway cannot
        # self-meter spend in currency units, so there is no local money
        # extrapolation here.
        money = self._money_snapshot_for(channel)
        available = money.get("available")
        money_exhausted = (isinstance(available, (int, float))
                           and not isinstance(available, bool)
                           and available <= 0)
        if budget is not None:
            exhausted = budget <= reserve
        elif money_exhausted:
            exhausted = True
        elif (isinstance(vendor_remaining, (int, float))
              and not isinstance(vendor_remaining, bool)):
            exhausted = vendor_remaining <= reserve
        else:
            exhausted = local_exhausted
        # Staleness freeze — the one channel-wide gate. It keys on the
        # channel's vendor-derived state (snapshot or budget seed), not on
        # this wire's: once the sweep stalls, no observation for the channel
        # can be trusted, manual sibling mappings included. Channels the
        # sweep never refreshes carry live signals only and are never
        # frozen, and neither is anything while the sweep is disabled.
        sync_age = None
        if (self.config.defaults.free_align_hours > 0
                and (self.vendor_quota.get(channel.name)
                     or isinstance(self.free_budget.get(channel.name), dict))):
            synced = self.vendor_synced.get(channel.name)
            if synced is not None:
                sync_age = max(0.0, time.time() - float(synced))
        stale = bool(
            sync_age is not None
            and sync_age > self.config.defaults.free_align_hours * 3600.0
            + STALE_FREEZE_GRACE)
        if stale:
            exhausted = True
        # Expired one-time packs also count as exhausted regardless of tokens.
        if expired:
            exhausted = True
        ratios = []
        if spec is not None and spec.tokens:
            ratios.append(tokens_used / spec.tokens)
        if spec is not None and spec.requests:
            ratios.append(requests_used / spec.requests)
        if vendor and vendor.get("total"):
            ratios.append(1 - (vendor.get("remaining") or 0) / vendor["total"])
        if budget is not None and vendor.get("total"):
            ratios.append(1 - max(0.0, budget) / vendor["total"])
        if (money and isinstance(available, (int, float))
                and not isinstance(available, bool)
                and spec is not None and spec.amount):
            ratios.append(1 - max(0.0, float(available)) / spec.amount)
        ratio = max(ratios) if ratios else 0.0
        source = "vendor"
        if spec is not None:
            if self._model_quota_for(channel, wire, exposed) is not None:
                source = "model"
            elif channel.free_quota is not None:
                source = "channel"
            else:
                source = "provider_baseline"
        return {
            "source": source,
            "period": spec.period if spec is not None else "total",
            "limit_tokens": spec.tokens if spec is not None else None,
            "limit_requests": spec.requests if spec is not None else None,
            "used_tokens": tokens_used,
            "used_requests": requests_used,
            "remaining_tokens": remaining_tokens,
            "remaining_requests": remaining_requests,
            "ratio": round(ratio, 4),
            "warning": bool(ratio >= 0.8 and not exhausted),
            "exhausted": exhausted,
            "expires": (spec.expires.strftime("%Y-%m-%d %H:%M")
                        if spec is not None and spec.expires is not None
                        else None),
            "expired": expired,
            "vendor_remaining": vendor.get("remaining"),
            "budget_remaining": budget,
            "reserve_tokens": reserve,
            "vendor_total": vendor.get("total"),
            "vendor_consumed": vendor.get("consumed"),
            "vendor_at": self.vendor_quota_at or None,
            "sync_age": round(sync_age, 1) if sync_age is not None else None,
            "stale": stale,
            # Money/token split (design §8): voucher channels ledger money
            # (grant face value + vendor account balance); token channels
            # ledger the token remainder. Each is None when not applicable.
            "grant_money": ({"amount": spec.amount,
                             "currency": spec.currency or "CNY"}
                            if spec is not None and spec.amount is not None
                            else None),
            "balance_money": dict(money) if money else None,
            "balance_tokens": (budget if budget is not None
                               else (vendor_remaining
                                     if isinstance(vendor_remaining, (int, float))
                                     and not isinstance(vendor_remaining, bool)
                                     else None)),
        }

    def free_quota_exhausted(self, channel, wire: str, exposed: str = "") -> bool:
        status = self.free_quota_status(channel, wire, exposed)
        return bool(status and status["exhausted"])

    def runtime_alerts(self) -> list[dict]:
        """Live conditions worth alarming about (P1-2 runtime alerts).

        Families: a route chain whose every usable channel is cooling or
        blocked; a free-tier model whose allowance is exhausted; a channel
        failing repeatedly; data volume headroom; persisted artefacts failing
        to write; a scheduled sweep that keeps failing. Config gaps (no key,
        everything disabled) stay silent — those are console hints, not
        runtime alarms.
        """
        now = time.time()
        alerts: list[dict] = []
        blocked = self.blocked_channels()
        for key, route in self.config.routes.items():
            down: list[str] = []
            usable = 0
            for name in route.channels:
                channel = self.config.channel(name)
                if channel is None or not channel.enabled or not channel.api_key:
                    continue  # config gaps are not runtime alarms
                if name in blocked:
                    down.append(f"{name}：{blocked[name]}")
                    continue
                state = self.store.states[name]
                if state.cooldown_until > now and not state.half_open:
                    down.append(f"{name}：cooling:{state.reason or 'unknown'}")
                else:
                    usable += 1
            if down and not usable:
                alerts.append({
                    "kind": "route_all_cooling",
                    "key": f"route:{key}",
                    "title": f"渠道链全冷却：{key}",
                    "message": ("[PlanRouter 运行告警] 渠道链全冷却\n"
                                f"路由：{key}\n渠道状态：\n"
                                + "\n".join(f"- {item}" for item in down)),
                })
        for channel in self.config.channels:
            if not channel.enabled:
                continue
            state = self.store.states[channel.name]
            if state.consecutive_errors >= RUNTIME_ALERT_ERROR_THRESHOLD:
                alerts.append({
                    "kind": "probe_failing",
                    "key": f"probe:{channel.name}",
                    "title": f"渠道连续失败：{channel.name}",
                    "message": ("[PlanRouter 运行告警] 渠道连续失败\n"
                                f"渠道：{channel.name}\n"
                                f"连续失败：{state.consecutive_errors} 次\n"
                                f"最近错误：{(state.last_error or '—')[:160]}"),
                })
            wires = sorted({str(w) for w in channel.models.values()
                            if w and str(w) != "*"})
            for wire in wires:
                if self.free_quota_exhausted(channel, wire):
                    alerts.append({
                        "kind": "free_quota_exhausted",
                        "key": f"freequota:{channel.name}:{wire}",
                        "title": f"免费额度已用尽：{channel.name}/{wire}",
                        "message": ("[PlanRouter 运行告警] 免费额度已用尽\n"
                                    f"渠道：{channel.name}\n模型：{wire}\n"
                                    "已停止路由该模型以避免按量计费。"),
                    })
        # Paid budget exhausted: once this month's plan/payg tokens cross the
        # configured line, paid candidates are skipped and free channels keep
        # serving — the operator should know before the next invoice does.
        budget = self.config.defaults.paid_budget_tokens
        if budget > 0:
            spent = self._paid_month_tokens()
            if spent >= budget:
                paid = sorted({c.name for c in self.config.channels
                               if c.enabled and self._billing_label(c) != "free"})
                alerts.append({
                    "kind": "paid_budget_exhausted",
                    "key": "paid_budget",
                    "title": f"付费预算已用尽：{spent:,}/{budget:,} tokens",
                    "message": ("[PlanRouter 运行告警] 本月付费预算已用尽\n"
                                f"预算：{budget:,} tokens/月\n"
                                f"已用：{spent:,} tokens\n"
                                f"付费渠道：{'、'.join(paid) or '（无）'}\n"
                                "已停止调度付费渠道（免费渠道不受影响），"
                                "可在控制台「路由链」页调高预算。"),
                })
        # Free→paid transition: the first paid request after free traffic
        # costs real money, so surface it briefly (window-limited, one shot).
        state = self._billing_state
        if state.get("prev") == "free" and state.get("at") \
                and time.time() - state["at"] < 900:
            alerts.append({
                "kind": "billing_switch",
                "key": "billing_switch",
                "title": "已切换到付费渠道",
                "message": ("[PlanRouter 通知] 本次请求开始走付费渠道\n"
                            f"当前计费方式：{state.get('billing')}\n"
                            "免费渠道不可用时自动切换，可能产生费用；"
                            "可在控制台「路由链」页查看策略预设。"),
            })
        # Disk headroom: usage, notification state, maintenance reports and
        # config backups all fail once the data volume fills up. Warn while
        # writes still succeed so the operator has time to act. Every volume
        # that holds a persisted artefact is checked (usually the same dir).
        disk_min = self.config.defaults.disk_min_free_mb
        if disk_min > 0:
            roots: list[Path] = []
            if self.config_path:
                roots.append(self.config_path.parent)
            for holder in (self.persistence, self.usage, self.notifier, self.audit):
                path = getattr(holder, "path", None)
                if path:
                    roots.append(Path(path).parent)
            checked: set[str] = set()
            for root in roots:
                if str(root) in checked:
                    continue
                checked.add(str(root))
                try:
                    free_mb = shutil.disk_usage(str(root)).free / (1024 * 1024)
                except OSError:
                    continue
                if free_mb < disk_min:
                    alerts.append({
                        "kind": "disk_low",
                        "key": f"disk:{root}",
                        "title": f"磁盘剩余空间不足：{free_mb:.0f} MB",
                        "message": ("[PlanRouter 运行告警] 数据目录磁盘空间不足\n"
                                    f"目录：{root}\n剩余：{free_mb:.0f} MB\n"
                                    f"阈值：{disk_min} MB\n"
                                    "用量、通知历史与维护报告可能写盘失败。"),
                    })
        # Failed persisted writes that have not succeeded since. A transient
        # failure clears on the next successful write of the same artefact.
        with self._write_failure_lock:
            failures = {kind: dict(row)
                        for kind, row in self.write_failures.items()}
        for kind, row in failures.items():
            label = PERSIST_KIND_LABELS.get(kind, kind)
            alerts.append({
                "kind": "persist_write_failed",
                "key": f"write:{kind}",
                "title": f"本地状态写入失败：{label}",
                "message": ("[PlanRouter 运行告警] 本地状态写入失败\n"
                            f"对象：{label}\n"
                            f"累计失败：{row.get('count', 0)} 次\n"
                            f"最近错误：{(row.get('last_error') or '—')[:160]}"),
            })
        # Self-maintenance that keeps breaking is itself an alarm: the sweep
        # only reports into its own task row otherwise, and nobody watches the
        # console 24/7 after go-live.
        for runner in (self.catalog_refresher, self.maintenance_runner,
                       self.free_align_runner, self.free_verify_runner):
            if not runner or runner.consecutive_failures < TASK_FAIL_ALERT_THRESHOLD:
                continue
            alerts.append({
                "kind": "task_failing",
                "key": f"task:{runner.key}",
                "title": f"定时任务连续失败：{runner.label}",
                "message": ("[PlanRouter 运行告警] 定时任务连续失败\n"
                            f"任务：{runner.label}（每 {runner.hours():g} 小时）\n"
                            f"连续失败：{runner.consecutive_failures} 次\n"
                            f"最近错误：{(runner.last_error or '—')[:160]}"),
            })
        return alerts

    def _read_raw(self) -> dict:
        if self.config_path and self.config_path.is_file():
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
        return {}

    # -------------------------------------------------- subscription management
    def _linked_channels(self, record: dict):
        """Channels a subscription points at, resolving ids (or legacy names)."""
        resolved = []
        for ref in record.get("channels") or []:
            channel = self.config.channel_by_ref(str(ref))
            if channel:
                resolved.append(channel)
        return resolved

    def subscription_channels(self, record: dict) -> list[str]:
        """Linked channel names, for rows that join usage/report data by name."""
        return [channel.name for channel in self._linked_channels(record)]

    def subscription_label(self, record: dict) -> str:
        """Vendor line derived from the linked channel's provider.

        A subscription stores no vendor of its own: the vendor belongs to the
        channel, so this falls back to the plan name when nothing is linked.
        """
        for channel in self._linked_channels(record):
            provider = str(channel.provider or "")
            if not provider:
                continue
            for entry in self.providers:
                if str(entry.get("id") or "") == provider:
                    return str(entry.get("name") or provider)
            return provider
        return str(record.get("plan") or "")

    def _sub_records(self) -> list[dict]:
        """Subscription rows in the shape report/notifier/CLI already consume.

        Those modules join by channel name and group by vendor; the stored
        record has neither (ids only, no vendor), so each row is copied with
        the ids resolved to names and the vendor derived from the channel.
        """
        rows = []
        for record in self.subscriptions:
            row = dict(record)
            row["channels"] = self.subscription_channels(record)
            row["vendor"] = self.subscription_label(record)
            rows.append(row)
        return rows

    def _migrate_subscription_channels(self, records: list[dict]) -> bool:
        """Rewrite legacy name references to channel ids; True when changed."""
        changed = False
        for record in records:
            refs = [str(ref) for ref in record.get("channels") or []]
            updated = []
            for ref in refs:
                channel = self.config.channel_by_ref(ref)
                ident = channel_ref(channel) if channel else ref
                if ident != ref:
                    changed = True
                updated.append(ident)
            record["channels"] = updated
        return changed

    def subscription_summary(self) -> dict:
        return subs.summarize(self.subscriptions, label=self.subscription_label)

    def subscriptions_payload(self) -> dict:
        rows = []
        for record in self.subscriptions:
            row = subs.enrich(record)
            row["channel_names"] = self.subscription_channels(record)
            row["vendor"] = self.subscription_label(record)
            plan = self.plan_for(record)
            if plan:
                row["vendor_plan"] = plan
            rows.append(row)
        return {
            "subscriptions": rows,
            "summary": self.subscription_summary(),
            "settings": self.subscription_settings,
            "path": str(self.subscriptions_path) if self.subscriptions_path else None,
        }

    def _refresh_billing_ranks(self) -> None:
        """Feed the router the billing priority of every channel.

        An explicit model name (e.g. deepseek-flash) should drain quota that
        would otherwise expire before spending cash: monthly plans first
        (soonest linked subscription expiry first, undated plans next), then
        free-tier channels, then pay-as-you-go. Plans sharing an expiry day
        tiebreak on remaining quota per remaining day (the plan most at risk
        of going unused drains first). Internal aliases (auto/free) ignore
        this map and keep the console chain order.

        Non-plan channels additionally get per-wire entries keyed
        "channel|wire": freeness can live on the wire id (see model_tier) —
        an OpenRouter ":free" model stays in the free group while its paid
        siblings rank pay-as-you-go, and a manual tiers entry flips either
        way. The router resolves the pair key first, then the channel key.
        """
        ranks: dict[str, tuple[int, float, float]] = {}
        for channel in self.config.channels:
            line = channel.business_line or channel.mode
            monthly = line in ("coding", "agent")
            expiry = float("inf")
            burn = 0.0
            for record in self.subscriptions:
                refs = record.get("channels") or []
                if channel_ref(channel) not in refs and channel.name not in refs:
                    continue
                try:
                    if subs.enrich(record).get("status") == "expired":
                        continue
                except (TypeError, ValueError, KeyError):
                    continue
                monthly = True
                try:
                    stamp = datetime.strptime(
                        str(record.get("expires_at") or ""), "%Y-%m-%d")
                except ValueError:
                    continue  # undated plan: still monthly, just no deadline
                when = stamp.timestamp()
                ratio = _burn_ratio(record, stamp)
                # Earliest expiry wins; on a tie the fuller plan does.
                if when < expiry or (when == expiry and ratio > burn):
                    expiry, burn = when, ratio
            if monthly:
                # A plan window bills every model on the channel; no
                # per-wire refinement applies.
                ranks[channel.name] = ((0, expiry, -burn) if expiry != float("inf")
                                       else (1, 0.0, 0.0))
                continue
            ranks[channel.name] = ((2, 0.0, 0.0) if channel.tier == "free"
                                   else (3, 0.0, 0.0))
            for wire in sorted({v for v in channel.models.values()
                                if v and v != "*"}):
                free = model_tier(channel, self.subscriptions, wire) == "free"
                ranks[f"{channel.name}|{wire}"] = ((2, 0.0, 0.0) if free
                                                   else (3, 0.0, 0.0))
        self.store.billing_ranks = ranks

    def save_subscriptions(self, raw) -> list[dict]:
        settings = self.subscription_settings
        if isinstance(raw, dict) and "settings" in raw:
            settings = subs.normalize_settings(raw["settings"])
        records = subs.normalize_all(raw)
        # The editor may still send legacy names (an old console tab); store
        # ids so the next rename cannot break the link.
        self.subscriptions = records
        self._migrate_subscription_channels(self.subscriptions)
        if self.subscriptions_path:
            subs.save_file(self.subscriptions_path, self.subscriptions, settings)
        self.subscription_settings = settings
        self._refresh_billing_ranks()
        return records

    def save_subscription_settings(self, settings) -> dict:
        incoming = dict(settings or {})
        if incoming.get("webhook_url") == "***":
            incoming["webhook_url"] = self.subscription_settings.get("webhook_url", "")
        normalized = subs.normalize_settings(incoming)
        if self.subscriptions_path:
            subs.save_file(self.subscriptions_path, self.subscriptions, normalized)
        self.subscription_settings = normalized
        return normalized

    def renew_subscription(self, subscription_id: str, payload: dict | None = None) -> dict:
        for index, record in enumerate(self.subscriptions):
            if record["id"] == subscription_id:
                updated = subs.renew_record(record, payload)
                self.subscriptions[index] = updated
                if self.subscriptions_path:
                    subs.save_file(self.subscriptions_path, self.subscriptions)
                self._refresh_billing_ranks()
                return subs.enrich(updated)
        raise ValueError(f"订阅不存在: {subscription_id}")

    # ------------------------------------------------------- vendor registry
    def save_provider(self, payload) -> dict:
        """Validate one vendor entry and persist it into the user overlay."""
        entry = providers_mod.normalize_vendor(payload)
        if self.config_path:
            providers_mod.save_overlay(self.config_path.parent, [entry])
        self.providers = providers_mod.load_providers(
            self.config_path.parent if self.config_path else None)
        merged = next((p for p in self.providers
                       if str(p.get("id") or "") == entry["id"]), None)
        return merged or entry

    def delete_provider(self, provider_id: str) -> list[dict]:
        """Drop one vendor's overlay entry, restoring the builtin copy."""
        if self.config_path:
            providers_mod.delete_overlay_entry(self.config_path.parent, provider_id)
        self.providers = providers_mod.load_providers(
            self.config_path.parent if self.config_path else None)
        return self.providers

    def docs_proxy(self) -> str:
        """Proxy for best-effort doc/catalog fetches: first channel proxy.

        Vendor doc sites rarely need a dedicated relay; when the user runs
        one for foreign channels, reuse it so docs.x.ai etc. stay reachable
        from CN networks.
        """
        for ch in self.config.channels:
            if ch.proxy:
                return ch.proxy
        return ""

    def maintain_providers(self, vendor_id=None) -> dict:
        """Run the registry refresh on demand and return its report."""
        report = {"applied": [], "suggestions": [], "alerts": []}
        # Surface which extractor will run so the console can show it; in
        # pool mode pick may probe first when the verified queue is empty.
        try:
            models, _mode = maint.pick_maintenance_models(self)
        except Exception:
            models = []
        report["extraction"] = "llm" if models else "regex"
        maint.refresh_providers(self, report, vendor_id=vendor_id)
        report["finished_at"] = time.time()
        return report

    # ------------------------------------------------------- config management
    def _compute_config_version(self, raw: dict) -> str:
        """Compute a stable hash of the visible config (excluding secrets).

        Used for optimistic locking: the console sends the version it saw on GET,
        and POST fails with 409 if the server version has changed. Secret values
        are dropped before hashing instead of masked: a mask keeps the key
        present, so a first-run password change (listen.admin_key going from
        absent to "***") would read as a config edit and reject the wizard's
        save with a version mismatch. env: references stay in the hash — they
        are visible config, not secrets.
        """
        import hashlib
        visible = copy.deepcopy(raw)
        # The wizard flag is server-held state written by /api/setup/wizard,
        # not config the console edits; finishing the wizard must not
        # invalidate the version a page already holds, or its next save
        # fails with a version mismatch.
        visible.pop("setup", None)

        def drop_secret(mapping: dict, field: str) -> None:
            value = mapping.get(field)
            if value and not str(value).startswith("env:"):
                mapping.pop(field, None)

        def drop_cred_map(creds: dict) -> None:
            for env_name, value in list(creds.items()):
                if value and not str(value).startswith("env:"):
                    creds.pop(env_name, None)

        for channel in (visible.get("channels") or []):
            drop_secret(channel, "api_key")
            own_creds = channel.get("creds")
            if isinstance(own_creds, dict):
                drop_cred_map(own_creds)
                if not own_creds:
                    channel.pop("creds", None)
        listen = visible.get("listen") or {}
        drop_secret(listen, "api_key")
        drop_secret(listen, "admin_key")
        creds = visible.get("credentials")
        if isinstance(creds, dict):
            for vendor, vendor_map in list(creds.items()):
                if isinstance(vendor_map, dict):
                    drop_cred_map(vendor_map)
                    if not vendor_map:
                        creds.pop(vendor, None)
        for account in (visible.get("accounts") or []):
            if not isinstance(account, dict):
                continue
            acc_creds = account.get("credentials")
            if isinstance(acc_creds, dict):
                drop_cred_map(acc_creds)
                if not acc_creds:
                    account.pop("credentials", None)
        # Stable JSON representation for hashing
        serialized = json.dumps(visible, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    def public_config(self) -> dict:
        visible = copy.deepcopy(self.raw)
        for channel in (visible.get("channels") or []):
            key = channel.get("api_key")
            if key and not str(key).startswith("env:"):
                channel["api_key"] = "***"
            # Channel-bound management creds mask like the vendor-level slot:
            # the console may echo them back but never needs the plaintext.
            own_creds = channel.get("creds")
            if isinstance(own_creds, dict):
                for env_name, value in own_creds.items():
                    if value and not str(value).startswith("env:"):
                        own_creds[env_name] = "***"
        listen = visible.get("listen") or {}
        for field in ("api_key", "admin_key"):
            if listen.get(field) and not str(listen[field]).startswith("env:"):
                listen[field] = "***"
        creds = visible.get("credentials")
        if isinstance(creds, dict):
            for vendor_map in creds.values():
                if not isinstance(vendor_map, dict):
                    continue
                for env_name, value in vendor_map.items():
                    if value and not str(value).startswith("env:"):
                        vendor_map[env_name] = "***"
        policy = self.config.models
        visible["models"] = {
            "branded": dict(policy.branded),
            "fallback_unknown": policy.fallback_unknown,
            "fallback_route": policy.fallback_route,
        }
        # Account management-plane creds mask exactly like the vendor slot.
        for account in (visible.get("accounts") or []):
            if not isinstance(account, dict):
                continue
            acc_creds = account.get("credentials")
            if isinstance(acc_creds, dict):
                for env_name, value in acc_creds.items():
                    if value and not str(value).startswith("env:"):
                        acc_creds[env_name] = "***"
        # Include version for optimistic locking
        visible["_version"] = self._compute_config_version(self.raw)
        return visible

    def _find_account(self, account_id: str) -> dict | None:
        """Lookup account by id in config.accounts (Account or dict)."""
        from .accounts import account_lookup
        return account_lookup(self.config, str(account_id))

    def apply_config(self, raw: dict, persist: bool = True, expected_version: str | None = None) -> Config:
        # Optimistic locking: check version before applying
        if expected_version is not None:
            current_version = self._compute_config_version(self.raw)
            if current_version != expected_version:
                raise ValueError(
                    f"config version mismatch: expected {expected_version}, "
                    f"current {current_version} — the config has been modified by another user; "
                    f"please refresh and re-apply your changes"
                )
        merged = merge_secrets(raw, self.raw)
        # The wizard flag lives server-side only: /api/setup/wizard owns it,
        # and the console always replays the setup section it fetched earlier
        # — stale by definition. Keep the in-memory truth, otherwise the
        # first save after finishing the wizard flips wizard_done back to
        # False and the wizard pops up again on the next visit.
        merged.pop("setup", None)
        if isinstance(self.raw.get("setup"), dict):
            merged["setup"] = copy.deepcopy(self.raw["setup"])
        ensure_channel_ids(merged)
        config = parse_config(merged)  # validate before touching the file
        self._check_vendor_binding(config)
        # 存储层去重：同一条链里重复同一渠道没有意义（failover 会反复撞同一
        # 个已经失败的渠道）。运行时 parse_config 在 $free 展开后已经去重，
        # 这里把落盘文件也清干净——无论重复从哪混进来（旧版批量改写、直接
        # 编辑文件、导入迁移包），任意一次保存都会自动修复，前端展示随之干净。
        for route in merged.get("routes") or []:
            ch = route.get("channels")
            if isinstance(ch, list) and len(ch) != len(set(ch)):
                route["channels"] = list(dict.fromkeys(ch))
        if persist and self.config_path:
            tmp = self.config_path.with_name(self.config_path.name + ".tmp")
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.config_path)
        self.raw = merged
        self._swap(config)
        # A channel renamed through the console keeps its id, so records stay
        # linked; records still carrying a name are migrated here.
        if self._migrate_subscription_channels(self.subscriptions) \
                and self.subscriptions_path:
            try:
                subs.save_file(self.subscriptions_path, self.subscriptions,
                               self.subscription_settings)
            except OSError as exc:
                self._note_write_failure("subscriptions", exc)
        return config

    def duplicate_channel(self, name: str) -> dict:
        """Clone a channel (endpoint, key, models, rates) under a new name.

        The multi-account × multi-plan setup makes near-identical channels the
        common case: copy one, then edit name/key/priority on the clone. The
        stored key is copied verbatim (env: references stay references); the
        clone starts with a clean state keyed by its new name and is inserted
        right after the source so the priority order stays readable.
        """
        channels = list(self.raw.get("channels") or [])
        index = next(
            (i for i, c in enumerate(channels)
             if isinstance(c, dict) and c.get("name") == name), None)
        if index is None:
            raise ValueError(f"渠道不存在：{name}")
        existing = {c.get("name") for c in channels if isinstance(c, dict)}
        base = f"{name}-copy"
        new_name = base
        n = 2
        while new_name in existing:
            new_name = f"{base}-{n}"
            n += 1
        clone = copy.deepcopy(channels[index])
        clone["name"] = new_name
        # A clone is a new entity: keeping the source id would make two
        # channels share one subscription reference.
        clone.pop("id", None)
        channels.insert(index + 1, clone)
        self.apply_config({**self.raw, "channels": channels})
        return {"ok": True, "name": new_name}

    def _verify_admin(self, provided: str) -> bool:
        """True when `provided` is the current management password."""
        expected = self.config.listen.admin_key
        if not expected:
            # No management password yet: a keyless console (no api_key
            # either) stays open — the browser forces the operator to set a
            # password on first use; a gateway api_key without an admin_key
            # is a locked management API, never a fallback to open access.
            return not self.config.listen.api_key
        return hmac.compare_digest(
            expected.encode("utf-8"), (provided or "").strip().encode("utf-8"))

    def rotate_admin_key(self, key: str = "", old_key: str = "") -> str:
        """Replace the management key; the current one must be confirmed."""
        if not self._verify_admin(old_key):
            raise ValueError("原管理密钥不正确")
        value = (key or "").strip() or "sk-admin-" + secrets.token_hex(16)
        if len(value) < 4:
            raise ValueError("管理密钥至少 4 个字符")
        raw = dict(self.raw)
        listen = dict(raw.get("listen") or {})
        listen["admin_key"] = value
        raw["listen"] = listen
        self.apply_config(raw)
        return value

    def set_master_key_enabled(self, enabled: bool, admin_key: str = "") -> bool:
        """Pause or resume the master gateway key; member keys are unaffected."""
        if not self._verify_admin(admin_key):
            raise ValueError("管理密码不正确")
        raw = dict(self.raw)
        listen = dict(raw.get("listen") or {})
        listen["api_key_enabled"] = bool(enabled)
        raw["listen"] = listen
        self.apply_config(raw)
        return bool(enabled)

    def rotate_master_key(self, key: str = "", admin_key: str = "") -> str:
        """Replace the master gateway key after confirming the password.

        A keyless console (no api_key, no admin_key yet) may rotate the
        master key without a password; leaving keyless mode then also seeds
        a random admin_key and returns it to the browser, otherwise the
        management API would lock itself out.
        """
        if not self._verify_admin(admin_key):
            raise ValueError("管理密码不正确")
        value = (key or "").strip() or "sk-router-" + secrets.token_hex(20)
        if len(value) < 8:
            raise ValueError("主密钥至少 8 个字符")
        raw = dict(self.raw)
        listen = dict(raw.get("listen") or {})
        listen["api_key"] = value
        listen["api_key_enabled"] = True
        if not listen.get("admin_key"):
            listen["admin_key"] = "sk-admin-" + secrets.token_hex(16)
        raw["listen"] = listen
        self.apply_config(raw)
        return value

    def _check_vendor_binding(self, config: Config) -> None:
        """Every channel must be bound to a real vendor entry.

        Creation is strongly bound: a channel that was not in the previous
        config needs a complete provider+mode pair, and any pair must exist
        in the vendor registry (builtin + overlay). Channels that predate
        vendor bookkeeping stay tolerated until they are edited. Enforcement
        is file-deployment-only: without a config file (library/test embeds)
        there is no on-disk previous state to anchor the legacy rule on.
        """
        if not self.config_path:
            return
        legacy = {
            c.get("name") for c in (self.raw.get("channels") or [])
            if isinstance(c, dict)
        }
        registry = {p.get("id"): p for p in self.providers
                    if isinstance(p, dict) and p.get("id")}
        for channel in config.channels:
            provider, mode = channel.provider, channel.mode
            if not provider and not mode:
                if channel.name in legacy:
                    continue  # predates vendor bookkeeping; tolerated
                raise ValueError(
                    f"渠道 {channel.name} 必须选择厂家："
                    "请填写 provider 与 mode 后再创建")
            if not provider or not mode:
                raise ValueError(
                    f"渠道 {channel.name} 的厂家信息不完整："
                    "provider 与 mode 必须同时提供")
            vendor = registry.get(provider)
            if vendor is None:
                raise ValueError(
                    f"渠道 {channel.name} 的厂家 {provider} 不存在，"
                    "可在管理页「厂家」查看可用厂家或添加自定义厂家")
            if mode not in (vendor.get("modes") or {}):
                available = "/".join(sorted(vendor.get("modes") or {}))
                raise ValueError(
                    f"渠道 {channel.name} 的厂家 {provider} 不支持模式 {mode}"
                    f"（可用：{available}）")

    def reload_from_disk(self) -> Config:
        raw = self._read_raw()
        if not raw:
            raise ValueError("配置文件不存在或无法读取")
        return self.apply_config(raw, persist=False)

    def _swap(self, config: Config) -> None:
        """Hot-reload config while preserving live cooldown/usage state."""
        with self.store.lock:
            old = self.store
            new = StateStore(config)
            for name, state in old.states.items():
                if name in new.states:
                    new.states[name] = state
            new.sessions = old.sessions
            new.catalogs = old.catalogs
            new.rr_cursor = old.rr_cursor
            # Keep stability scores for channels that still exist; scores of
            # deleted/renamed channels are dropped.
            new.scores = {key: list(value) for key, value in old.scores.items()
                          if key.split("|", 1)[0] in new.states}
            self.config = config
            self.store = new
        self._refresh_billing_ranks()

    def reset_channel(self, name: str) -> bool:
        with self.store.lock:
            state = self.store.states.get(name)
            if not state:
                return False
            state.cooldown_until = 0.0
            state.reason = ""
            state.consecutive_errors = 0
            state.half_open = False
            state.last_error = ""
            state.quota = {}
            return True

    def reset_scores(self, channel: str | None = None,
                     wire: str | None = None) -> int:
        """Clear stability scores (all / one channel / one channel×wire)."""
        return self.store.reset_scores(channel, wire)

    def test_channels(self, channels, model: str | None = None,
                      timeout: float | None = None) -> dict:
        """Probe console drafts without touching cooldown, usage or audit state.

        Items may be full channel objects (an unsaved channel draft) or bare
        names (a subscription's linked channels, resolved from saved config).
        ``timeout`` caps each probe in seconds (clamped 3..60); the wizard's
        model sweep passes a short one so a hung candidate is skipped instead
        of stalling the loop, while the console's single manual click keeps
        the default 60s (slow proxies legitimately need 10-20s to succeed).
        """
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = TEST_TIMEOUT
        timeout = min(60.0, max(3.0, timeout))
        if not isinstance(channels, list) or not channels:
            raise ValueError("channels 必须是非空数组")
        if len(channels) > MAX_CHANNELS:
            raise ValueError(f"一次最多测试 {MAX_CHANNELS} 个渠道")
        wanted = model.strip() if isinstance(model, str) else ""
        wanted = wanted or None
        saved = {
            c.get("name"): c for c in (self.raw.get("channels") or [])
            if isinstance(c, dict)
        }
        prepared = []
        for item in channels:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict):
                raise ValueError("channels 元素必须是渠道对象或渠道名")
            name = item.get("name")
            base = saved.get(name, {}) if isinstance(name, str) else {}
            prepared.append({**base, **item})
        merged = merge_secrets({"channels": prepared}, self.raw)["channels"]
        with ThreadPoolExecutor(max_workers=min(4, len(merged))) as pool:
            results = list(pool.map(
                lambda channel: probe_channel(channel, wanted, timeout),
                merged))
        self._append_probe_log(results)
        return {
            "ok": all(result.get("ok") for result in results),
            "results": results,
            "note": "已向每个渠道发送一次最小非流式请求（max_tokens=16）；"
                    "不计入冷却、用量与审计，可能产生极少量费用",
        }

    def _append_probe_log(self, results: list[dict], kind: str = "probe") -> None:
        """Persist one JSON line per probe/catalog batch; secrets are masked.

        Shares the report-log rotation (append_jsonl) and the write-failure
        alerts of the other persisted artefacts; a failed write never breaks
        the console.
        """
        if not self.config_path:
            return
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                 "kind": kind, "results": results}
        append_jsonl(self.config_path.parent / "probe.jsonl", entry, "probe_log",
                     on_error=self._note_write_failure,
                     on_ok=self._note_write_ok)

    # ------------------------------------------------------ wizard verify
    def _verify_route_status(self, protocol: str, route_name: str,
                             blocked: dict, requested: str = AUTO_MODEL) -> dict[str, str]:
        """Per-channel schedulability for the wizard's verify endpoint.

        Same vocabulary as the no-candidate gateway reply: missing/disabled/
        blocked, or whatever the state machine reports (cooldown windows,
        rate limits, free-quota freezes) — the console renders it verbatim.

        An otherwise-healthy channel can still be unschedulable at the wire
        level: a 429 from the acceptance call itself cools only the mapped
        model (5h default, no Retry-After), and reporting plain "ok" while
        pick comes back empty is exactly the contradiction that once sent a
        user deleting a working channel. When the wire model this request
        would dispatch is cooling, the map says so with the minutes left.
        """
        route = self.config.route(protocol, route_name)
        status: dict[str, str] = {}
        now = time.time()
        for name in (route.channels if route else []):
            channel = self.config.channel(name)
            if channel is None:
                status[f"{protocol}:{name}"] = "missing"
            elif not channel.enabled:
                status[f"{protocol}:{name}"] = "disabled"
            elif name in blocked:
                status[f"{protocol}:{name}"] = "blocked"
            else:
                with self.store.lock:
                    state = self.store.states[name]
                    ok, why = state.available(now, channel.rate)
                    if not ok:
                        status[f"{protocol}:{name}"] = why
                        continue
                    wire = dispatch_wire_model(channel, requested)
                    until = state.model_cooldown_until.get(wire or "", 0.0)
                    if wire and until > now:
                        minutes = max(1, int((until - now + 59) // 60))
                        status[f"{protocol}:{name}"] = (
                            f"模型冷却中：{wire}（剩约 {minutes} 分钟）")
                    else:
                        status[f"{protocol}:{name}"] = "ok"
        return status

    @staticmethod
    def _verify_hints(kind: str, http_status: int | None,
                      detail: dict) -> list[str]:
        """Chinese repair suggestions for the wizard's failure panel."""
        if kind == "no_candidate":
            return [
                "没有可调度的渠道：在「渠道配置」页检查渠道是否启用、Key 是否填写完整。",
                "确认路由链包含该渠道（向导保存的渠道会自动追加进 default 链）。",
                "渠道处于冷却/限速/免费额度耗尽状态时同样不可调度，稍后点「重新验收」。",
                "若渠道显示「模型冷却中」：该模型刚被上游限速/额度冷却（OpenRouter 免费模型"
                "尤其常见，通常几十秒到几分钟就恢复），可等冷却结束再验收，或在「渠道配置」页"
                "把 auto 映射换一个模型。",
            ]
        hints: list[str] = []
        if http_status == 401:
            hints.append("上游返回 401：厂家 API Key 无效或已过期——回「填写厂家 Key」更正，或在「渠道配置」页更新。")
            hints.append("若报错来自网关本身（密钥不识别），请回「生成调用密钥」重新生成。")
        elif http_status == 403:
            hints.append("上游返回 403：Key 没有该模型的调用权限或被风控，检查厂家控制台的模型开通状态。")
        elif http_status == 404:
            hints.append("上游返回 404：模型名可能已下线或渠道映射错误——在「渠道配置」页修正模型映射。")
        elif http_status == 429:
            hints.append("上游返回 429：限速或额度耗尽——稍后重试、更换渠道，或在「渠道配置」页调整限速设置。")
        elif http_status and http_status >= 500:
            hints.append("上游服务异常（5xx）：通常是厂家临时故障，稍后点「重新验收」。")
        else:
            hints.append("调用失败：可结合「请求日志」页的失败详情进一步排查。")
        status_map = (detail.get("error") or {}).get("channel_status")
        if status_map:
            bad = [f"{name}: {state}" for name, state in status_map.items()
                   if state != "ok"]
            if bad:
                hints.append("网关侧渠道状态：" + "，".join(bad))
        return hints

    def verify_setup_call(self, model: str | None = None,
                          key: str | None = None) -> dict:
        """End-to-end acceptance for the setup wizard.

        Plans the route without sending anything, then loops back one real
        minimal call through this very gateway — so routing, failover and
        the audit trail behave exactly like live traffic — and reports which
        channel/wire model was hit plus the per-attempt trace. Never writes
        config; the call may spend a tiny amount of upstream quota (the same
        trade-off the channel test already makes, and the reply states it).
        """
        requested = str(model or "").strip() or "auto"
        blocked = self.blocked_channels()
        plans: dict[str, tuple] = {}
        for protocol in (OPENAI, ANTHROPIC):
            decision = resolve_model(self.config, protocol, requested, None,
                                     blocked)
            candidates: list = []
            route_name = decision.hint or "default"
            if decision.mode != "rejected":
                candidates, route = self.store.pick(
                    protocol, decision.hint, decision.exposed, None,
                    decision=decision, blocked=set(blocked),
                    agent_candidates=None)
                if route is not None:
                    route_name = route.name
            plans[protocol] = (decision, list(candidates), route_name)

        chosen = next((p for p in (OPENAI, ANTHROPIC) if plans[p][1]), None)
        if chosen is None:
            status: dict[str, str] = {}
            for protocol in (OPENAI, ANTHROPIC):
                status.update(self._verify_route_status(
                    protocol, plans[protocol][2], blocked, requested))
            return {
                "ok": False, "kind": "no_candidate", "model": requested,
                "attempts": [],
                "channel_status": status,
                "hints": self._verify_hints("no_candidate", None, {}),
                "note": "路由链上没有可调度的渠道，未发起上游调用",
            }

        decision, candidates, route_name = plans[chosen]
        planned = [{"channel": ch.name, "wire_model": wire}
                   for ch, wire in candidates]
        listen = self.config.listen
        host = "127.0.0.1" if listen.host in ("0.0.0.0", "::", "") else listen.host
        if chosen == ANTHROPIC:
            url = f"http://{host}:{listen.port}/v1/messages"
            body = {"model": requested, "max_tokens": 16, "stream": False,
                    "messages": [{"role": "user", "content": "ping"}]}
        else:
            url = f"http://{host}:{listen.port}/v1/chat/completions"
            body = {"model": requested, "max_tokens": 16, "temperature": 0,
                    "stream": False,
                    "messages": [{"role": "user", "content": "ping"}]}
        token = str(key or "").strip() or listen.api_key or ""
        call_headers = {"Content-Type": "application/json"}
        if token:
            call_headers["Authorization"] = f"Bearer {token}"
        offset = self.audit.offset_snapshot() if self.audit else None
        started = time.perf_counter()
        routed, failover, http_status, detail = "", None, None, {}
        try:
            req = Request(
                url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=call_headers, method="POST")
            with urlopen(req, timeout=max(60, self.config.defaults.timeout)) as resp:
                resp.read()
                routed = resp.headers.get("X-Routed-Via") or ""
                failover = resp.headers.get("X-Failover-Count")
                http_status = getattr(resp, "status", 200)
        except HTTPError as exc:
            http_status = exc.code
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except Exception:
                detail = {}
        except (URLError, OSError) as exc:
            return {
                "ok": False, "kind": "call_failed", "model": requested,
                "protocol": chosen, "route": f"{chosen}:{route_name}",
                "planned": planned, "hit": None,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": {"message": f"网关回环调用失败：{exc}", "http_status": None},
                "hints": ["无法连接本机网关端口：确认服务仍在运行，再点「重新验收」。"],
            }
        ms = round((time.perf_counter() - started) * 1000, 1)

        attempts: list[dict] = []
        if self.audit and offset is not None:
            # The serving thread appends audit rows after sending the
            # response, so poll briefly instead of racing it.
            for _ in range(10):
                attempts = [e for e in self.audit.entries_since(offset)
                            if e.get("protocol") == chosen]
                if attempts:
                    break
                time.sleep(0.1)

        ok = 200 <= (http_status or 0) < 300
        hit = None
        if routed and routed != "none" and "/" in routed:
            channel, wire = routed.split("/", 1)
            hit = {"channel": channel, "wire_model": wire}
        result = {
            "ok": ok,
            "kind": "ok" if ok else "call_failed",
            "model": requested,
            "protocol": chosen,
            "route": f"{chosen}:{route_name}",
            "planned": planned,
            "hit": hit,
            "failover": int(failover) if failover and str(failover).isdigit()
            else None,
            "latency_ms": ms,
            "attempts": attempts,
        }
        if ok:
            return result
        message = ((detail.get("error") or {}).get("message")
                   or f"HTTP {http_status}")
        result["error"] = {"message": message, "http_status": http_status}
        result["hints"] = self._verify_hints("call_failed", http_status, detail)
        return result

    def _registry_plan_models(self, channel: dict) -> tuple[str, list[str]] | None:
        """Curated plan-model list for a channel linked to a vendor mode.

        Coding/agent plan endpoints do not serve their plan models through
        the upstream /models route, so when the channel is linked to a vendor
        mode whose registry models are known, that list is the catalog.
        Returns None when unlinked, payg, or the registry has no list yet —
        the caller falls back to the upstream fetch.
        """
        pid = str(channel.get("provider") or "").strip()
        mode_key = str(channel.get("mode") or "").strip()
        if not pid or not mode_key or mode_key == "payg":
            return None
        provider = next((p for p in self.providers
                         if isinstance(p, dict) and p.get("id") == pid), None)
        mode = (provider or {}).get("modes", {}).get(mode_key) if provider else None
        models = [str(m).strip() for m in (mode or {}).get("models") or [] if str(m).strip()]
        if not models:
            return None
        return f"registry:{pid}/{mode_key}", models

    def _ark_credentials(self, channel: dict) -> dict[str, str]:
        """IAM AK/SK for the volcengine management plane.

        Precedence mirrors the free-align injection: unsaved editor draft
        (plain value or env:VAR) beats the channel's own bound creds
        (channel = account), which beat the vendor-level config.credentials
        slot, which beat the process environment.
        """
        saved_vendor = (getattr(self.config, "credentials", None) or {}).get(
            "volcengine") or {}
        draft = (channel.get("credentials") or {}).get("volcengine") or {}
        own = (channel.get("creds") or {}) if isinstance(
            channel.get("creds"), dict) else {}
        if not own:
            saved_ch = next((c for c in self.raw.get("channels") or []
                             if isinstance(c, dict)
                             and c.get("name") == channel.get("name")), {})
            own = (saved_ch.get("creds") or {}) if isinstance(
                saved_ch.get("creds"), dict) else {}
        out: dict[str, str] = {}
        for key in (ark_quota.AK_ENV, ark_quota.SK_ENV):
            value = str(draft.get(key) or "").strip()
            if value and value != "***":
                value = resolve_secret(value) or ""
            elif str(own.get(key) or "").strip() and str(own.get(key)) != "***":
                value = resolve_secret(str(own.get(key)).strip()) or ""
            else:
                value = str(saved_vendor.get(key) or "").strip()
            if not value:
                value = os.environ.get(key, "").strip()
            if value:
                out[key] = value
        return out

    def _probe_ark_quota(self, channel: dict) -> dict:
        """Catalog pull for a volcengine payg channel, combining both planes.

        The collaborative-reward free quota is only visible through the
        IAM-signed ListModelActivations action, and it reports family names
        (``kimi-k3-1-pro``), not the versioned id a chat call needs (the
        data-plane /models route lists ``kimi-k3-1-pro-261024``). So the
        quota list from the management plane is filtered here against the
        data-plane /models list (fetched with the channel's own ark-... key)
        to resolve each free family into its callable name. No IAM
        credentials -> refuse the pull instead of falling back to the
        data-plane /models list: the free channel's mapping is defined by
        its free quota, and a 53-model paid-catalog dump would mislead.
        A failed signed query is reported instead of silently switching
        sources.
        """
        name = channel.get("name")
        creds = self._ark_credentials(channel)
        ak = creds.get(ark_quota.AK_ENV, "")
        sk = creds.get(ark_quota.SK_ENV, "")
        if not ak or not sk:
            return {"ok": False, "name": name,
                    "error": "未配置 IAM AK/SK，无法拉取免费额度模型清单",
                    "hint": ("在渠道编辑器填写并保存 IAM Access Key / Secret Key"
                             "（或设置环境变量 VOLC_ACCESSKEY / VOLC_SECRETKEY）"
                             "后重试；免费渠道的映射以免费额度为准，"
                             "数据面全量清单不用于映射")}
        try:
            rows = ark_quota.rows_from_items(ark_quota.list_items(ak, sk))
        except Exception as exc:
            return {"ok": False, "name": name,
                    "error": f"方舟管理面查询失败：{exc}",
                    "hint": "检查 IAM AK/SK（访问控制 IAM → API 访问密钥）；"
                            "ark- 开头的数据面 Key 无法查询免费额度"}
        families: list[str] = []
        for row in rows:
            m = str(row.get("model") or "").strip()
            if m and m not in families:
                families.append(m)
        dp = probe_models(channel)
        if not dp.get("ok"):
            return {"ok": False, "name": name,
                    "error": f"数据面 /models 查询失败：{dp.get('error')}",
                    "hint": "已拿到免费额度清单，但无法解析可调用名；"
                            "检查渠道 api_key 与 base_url"}
        models = ark_quota.resolve_callable_names(
            families, dp.get("models") or [])
        result = {"ok": True, "name": name, "source": "ark-quota",
                  "endpoint": (f"management:{ark_quota.ACTION}"
                               f" + data:{dp.get('endpoint') or 'models'}"),
                  "models": models, "count": len(models)}
        if not families:
            result["hint"] = ("该账号当前没有剩余免费额度"
                              "（协作奖励资源包未发放或已用尽）")
        elif not models:
            result["hint"] = ("管理面有免费额度，但 /models 里找不到对应可调用名；"
                              "确认渠道 api_key 与 IAM 属同一账号")
        return result

    def _free_quota_mode(self, channel: dict) -> tuple[list[str], list[str]] | None:
        """Registry (free_quota_cmd, credential_envs) for a free-tier channel.

        volcengine keeps its dedicated two-plane flow (_probe_ark_quota): its
        CLI reports family names that still need data-plane resolution. Other
        vendors (bailian) name callable models directly, so their CLI output
        alone defines what the free channel may map.
        """
        pid = str(channel.get("provider") or "")
        mode_key = str(channel.get("mode") or "")
        if not pid or not mode_key or pid == "volcengine":
            return None
        if str(channel.get("tier") or "") != "free":
            return None
        provider = next((p for p in getattr(self, "providers", None) or []
                         if isinstance(p, dict) and p.get("id") == pid), None)
        mode = (provider or {}).get("modes", {}).get(mode_key) if provider else None
        if not isinstance(mode, dict):
            return None
        cmd = mode.get("free_quota_cmd")
        if not (isinstance(cmd, list) and cmd
                and all(isinstance(x, str) for x in cmd)):
            return None
        envs = []
        for entry in mode.get("credential_envs") or []:
            env = str((entry.get("env") if isinstance(entry, dict) else entry)
                      or "").strip()
            if env:
                envs.append(env)
        return cmd, envs

    def _registry_free_models(self, channel: dict) -> list[str]:
        """Registry ``free_models`` subset of the linked vendor mode.

        Vendors whose free trial covers only part of the data-plane catalog
        (TokenHub: every language/multimodal model ships a 1M-token free
        pack, image/video/3D/speech ids do not) declare that subset as
        ``free_models`` on the mode, so a console catalog pull can keep just
        the free-and-served ids instead of dumping the whole 140+ catalog
        onto a free channel.
        """
        pid = str(channel.get("provider") or "").strip()
        mode_key = str(channel.get("mode") or "").strip()
        if not pid or not mode_key:
            return []
        provider = next((p for p in getattr(self, "providers", None) or []
                         if isinstance(p, dict) and p.get("id") == pid), None)
        mode = (provider or {}).get("modes", {}).get(mode_key) if provider else None
        if not isinstance(mode, dict):
            return []
        return [str(m).strip() for m in mode.get("free_models") or []
                if str(m).strip()]

    def _probe_registry_free_models(self, channel: dict,
                                    free_ids: list[str]) -> dict:
        """Data-plane /models filtered down to the registry free subset.

        The upstream list stays authoritative for what is actually callable
        (withdrawn ids never get mapped); the registry supplies which of
        them the vendor's free trial covers. An empty intersection means
        the registry list went stale — return the full catalog with a hint
        instead of silently emptying the channel.
        """
        result = probe_models(channel)
        if not result.get("ok"):
            return result
        upstream = {str(m) for m in result.get("models") or []}
        kept = [m for m in free_ids if m in upstream]
        if not kept:
            result["hint"] = ("上游清单里没有注册表登记的免费模型"
                              "（登记清单可能已过时），已返回全量清单，请手动挑选")
            return result
        dropped = max(0, len(upstream) - len(kept))
        result.update({"source": "free-quota", "models": kept,
                       "count": len(kept)})
        if dropped:
            result["hint"] = (f"已按免费额度过滤：{len(kept)} 个免费模型，"
                              f"另有 {dropped} 个为计费/视觉生成模型")
        return result

    def _free_quota_credentials(self, channel: dict,
                                envs: list[str]) -> dict[str, str]:
        """Management-plane credentials for a free-quota CLI pull.

        Per key, first non-empty source wins: unsaved editor draft (plain
        value or env:VAR) > channel-bound creds (channel = account) > account
        credentials > vendor-level config.credentials > process environment.
        """
        from .accounts import account_lookup
        provider = str(channel.get("provider") or "")
        draft = (channel.get("credentials") or {}).get(provider) or {}
        own = channel.get("creds") if isinstance(channel.get("creds"), dict) else {}
        account_id = str(channel.get("account") or "")
        account: dict = {}
        if account_id:
            account = (account_lookup(self.config, account_id) or {}).get(
                "credentials") or {}
        vendor = (getattr(self.config, "credentials", None) or {}).get(provider) or {}
        out: dict[str, str] = {}
        for key in envs:
            for source in (draft, own, account, vendor):
                value = str(source.get(key) or "").strip()
                if value == "***":
                    continue
                value = resolve_secret(value) or ""
                if value:
                    out[key] = value
                    break
            else:
                value = os.environ.get(key, "").strip()
                if value:
                    out[key] = value
        return out

    def _probe_free_quota(self, channel: dict, cmd: list[str],
                          envs: list[str]) -> dict:
        """Catalog pull for a non-volcengine free channel, quota-driven.

        Same principle as _probe_ark_quota: the free channel's mapping is
        defined by the quota the vendor still grants, so the data-plane
        /models dump is never used as a fallback — mapping a billed model
        onto a free channel would silently charge per token. The vendor CLI
        (bailian: ``bl usage free``; tokenhub: ``facet-tokenhub-quota``)
        already names callable models, so no family→version resolution is
        needed. Without management credentials a registry free_models
        subset (TokenHub's documented policy list) keeps the pull working —
        it is the curated fallback, the CLI is the live source.
        """
        name = channel.get("name")
        creds = self._free_quota_credentials(channel, envs)
        missing = [key for key in envs if not creds.get(key)]
        if missing:
            free_ids = self._registry_free_models(channel)
            if free_ids:
                result = self._probe_registry_free_models(channel, free_ids)
                if result.get("ok"):
                    hint = ("未配置管理面凭据（%s），已回退注册表静态免费清单；"
                            "在渠道凭据区填写 %s 后可按官方政策实时拉取"
                            % ("、".join(missing), " / ".join(missing)))
                    if result.get("hint"):
                        hint = f"{result['hint']}；{hint}"
                    result["hint"] = hint
                return result
            return {"ok": False, "name": name,
                    "error": "未配置免费额度查询凭据（%s）" % "、".join(missing),
                    "hint": ("在渠道编辑器的凭据区填写并保存后重试"
                             "（或设置环境变量 %s）；免费渠道的映射以免费额度为准，"
                             "上游全量清单不用于映射" % " / ".join(missing))}
        try:
            rows = freealign_mod.parse_free_quota_output_rows(
                freealign_mod.run_vendor_cmd(list(cmd), extra_env=creds))
        except Exception as exc:
            return {"ok": False, "name": name,
                    "error": f"免费额度命令执行失败：{str(exc)[:200]}",
                    "hint": "确认厂家 CLI 已安装、AK/SK 属于该厂家账号，"
                            "且子账号有免费额度查询权限"}
        reserve = FREE_RESERVE_TOKENS.get(
            str(channel.get("provider") or ""), FREE_RESERVE_DEFAULT_TOKENS)
        models = freealign_mod.free_models_from_rows(rows, reserve=reserve)
        result = {"ok": True, "name": name, "source": "free-quota",
                  "endpoint": "cmd:" + " ".join(cmd),
                  "models": models, "count": len(models)}
        if not models:
            result["hint"] = ("免费额度接口没有返回仍可用的 Text 模型"
                              "（额度已用尽或已过期）")
        return result

    def probe_channel_models(self, channel, allow_ark_quota: bool = False) -> dict:
        """Fetch one channel's upstream model list for the console.

        `channel` may be a full draft (unsaved form) or a bare name. Secrets
        marked *** are merged from the saved config; a successful fetch for a
        saved channel name is recorded in the channel's catalog.

        allow_ark_quota=True (console-initiated pulls) lets free channels
        take their catalog from the vendor's quota source instead of the
        data-plane /models dump: volcengine payg uses the IAM-signed
        management plane, other vendors declaring a registry free_quota_cmd
        (bailian ``bl usage free``, tokenhub ``facet-tokenhub-quota``) run
        that CLI and keep only models with free quota left; when the
        management credentials are missing but the mode declares a
        ``free_models`` subset, the pull falls back to that curated list.
        The background catalog sweep keeps using the data-plane route.
        """
        return self._record_catalog_result(
            self._fetch_catalog(channel, allow_ark_quota))

    def _fetch_catalog(self, channel, allow_ark_quota: bool = False) -> dict:
        """Fetch half of probe_channel_models: no store writes, no probe log.

        Pure upstream I/O, so the background sweep may run several of these
        concurrently (see refresh_all_catalogs).
        """
        if not isinstance(channel, dict):
            raise ValueError("channel 必须是对象")
        name = channel.get("name")
        saved = {
            c.get("name"): c for c in (self.raw.get("channels") or [])
            if isinstance(c, dict)
        }
        base = saved.get(name, {}) if isinstance(name, str) else {}
        if isinstance(name, str) and name and name not in saved and not base.get("base_url") \
                and not channel.get("base_url"):
            return {"ok": False, "name": name,
                    "error": "本地配置中找不到该渠道",
                    "hint": "请先保存渠道，或在表单里填好 base_url 后再拉取"}
        merged = merge_secrets({"channels": [{**base, **channel}]}, self.raw)["channels"][0]
        if not merged.get("models"):
            merged["models"] = {"auto": "*"}  # catalog fetch does not need a mapping
        plan = self._registry_plan_models(merged)
        if plan:
            endpoint, models = plan
            result = {"ok": True, "name": name, "source": "registry",
                      "endpoint": endpoint, "models": models, "count": len(models)}
        elif allow_ark_quota and str(merged.get("provider") or "") == "volcengine" \
                and str(merged.get("mode") or "") == "payg":
            result = self._probe_ark_quota(merged)
        elif allow_ark_quota and (quota_mode := self._free_quota_mode(merged)):
            result = self._probe_free_quota(merged, *quota_mode)
        elif allow_ark_quota and (free_ids := self._registry_free_models(merged)):
            result = self._probe_registry_free_models(merged, free_ids)
        else:
            result = probe_models(merged)
        if result.get("ok") and result.get("models"):
            # Catalogs are alphabetical: speech/embedding ids can lead, and a
            # chat-completion test against them is rejected upstream. Callers
            # (setup wizard, console) use this instead of models[0]. 免费
            # 渠道先挑 :free/-free 对话模型——字母序头部常全是付费模型
            # （openrouter 的 aion-labs/amazon 块），默认测试模型不能落在
            # 付费模型上。
            result["suggested"] = pick_probe_model(
                result["models"], prefer_free=str(merged.get("tier") or "") == "free")
        result.setdefault("name", name)  # some probe paths omit the echo
        return result

    def _record_catalog_result(self, result: dict) -> dict:
        """Bookkeeping half of probe_channel_models: record + probe log.

        Serial on purpose: catalog diffs against the previous snapshot and
        report-log appends must not interleave when a sweep fetches channels
        concurrently.
        """
        name = result.get("name")
        if result.get("ok") and result.get("models") \
                and isinstance(name, str) and self.config.channel(name):
            self.store.record_catalog(name, result["models"], result["endpoint"])
            view = self.store.catalog_view(name)
            result["catalog"] = view
            result["changes"] = view["changes"]
        self._append_probe_log([result], kind="catalog")
        return result

    def refresh_all_catalogs(self) -> list[dict]:
        """Re-fetch every enabled channel's upstream model catalog.

        Fetches run concurrently — one slow upstream must not serialize the
        whole sweep — while catalog records and probe-log lines are written
        serially afterwards, keeping the sweep's channel order.
        """
        payloads = []
        for channel in list(self.config.channels):
            if not channel.enabled or not channel.api_key:
                continue
            payload = {"name": channel.name}
            if channel.provider:
                payload["provider"] = channel.provider
            if channel.mode:
                payload["mode"] = channel.mode
            payloads.append((channel.name, payload))

        def fetch(entry: tuple[str, dict]) -> dict:
            name, payload = entry
            try:
                return self._fetch_catalog(payload)
            except Exception as exc:  # one broken channel must not stop the sweep
                return {"ok": False, "name": name, "error": str(exc)[:200]}

        results = []
        if payloads:
            with ThreadPoolExecutor(max_workers=min(maint.PROBE_WORKERS,
                                                    len(payloads))) as executor:
                fetched = list(executor.map(fetch, payloads))
            results = [self._record_catalog_result(result) for result in fetched]
        return results

    # ---------------------------------------------------------- maintenance
    def run_maintenance(self, trigger: str = "manual") -> dict:
        """One maintenance pass: refresh catalogs, auto-fix, analyse, report.

        Only catalog-confirmed changes are applied automatically (after a
        config backup); LLM output and quota heuristics become suggestions
        for the console. Never raises: failures land in the report.
        """
        if not self.config_apply_lock.acquire():
            # The scheduled runner and the console button must not interleave:
            # both patch channel mappings and write reports. The contender
            # queues on the gate; only a sweep running past GATE_QUEUE_WAIT
            # still lands here.
            return {"busy": True, "trigger": trigger, "patch": {},
                    "applied": [], "suggestions": [], "alerts": [],
                    "message": f"上一次自维护仍在运行（已排队等待"
                               f"{int(GATE_QUEUE_WAIT)}秒），本次已跳过"}
        try:
            return self._run_maintenance(trigger)
        finally:
            self.config_apply_lock.release()

    def _catalogs_fresh(self, window: float) -> bool:
        """True when every refreshable channel's catalog was fetched lately.

        Mirrors refresh_all_catalogs' channel filter (enabled + api_key), so
        "fresh" means exactly "a recent sweep covered everything this pass
        would fetch".
        """
        now = time.time()
        for channel in self.config.channels:
            if not channel.enabled or not channel.api_key:
                continue
            with self.store.lock:
                catalog = self.store.catalogs.get(channel.name) or {}
                fetched = float(catalog.get("fetched_at") or 0)
            if not fetched or now - fetched > window:
                return False
        return True

    def _run_maintenance(self, trigger: str) -> dict:
        # The catalog refresher's sweep runs minutes ahead of this pass and
        # fetches exactly the same endpoints; re-fetching them all again
        # doubled the traffic without learning anything new. Reuse when
        # fresh enough, sweep otherwise.
        reuse = self._catalogs_fresh(CATALOG_REUSE_WINDOW)
        refresh = [] if reuse else self.refresh_all_catalogs()
        with self.store.lock:
            catalogs = {name: dict(entry) for name, entry in self.store.catalogs.items()}
            states = self.store.snapshot()["channels"]
        report = maint.analyze(self.config, catalogs, states, refresh)
        if reuse:
            report["catalogs_reused"] = True

        if report["patch"]:
            self._backup_config("maintenance")
            raw = copy.deepcopy(self.raw)
            for channel in raw.get("channels") or []:
                if isinstance(channel, dict) and channel.get("name") in report["patch"]:
                    channel["models"] = report["patch"][channel["name"]]
            try:
                self.apply_config(raw)
            except (ValueError, OSError) as exc:
                report["patch"] = {}
                report["alerts"].append({
                    "channel": "", "kind": "apply_failed",
                    "message": f"自动应用变更失败，配置已回读未提交：{exc}",
                })
            else:
                # The repoint is live; prove its new target answers before the
                # report goes out, or a wrong pick bills through a broken
                # mapping until the next sweep (paid channels included).
                maint.verify_repoints(self, report)

        maint.refresh_plan_models(self, report)
        maint.refresh_providers(self, report)

        usage = None
        if self.usage is not None:
            usage = {c["name"]: c for c in self.usage.series(1)["channels"]}
        reply, model, llm_errors = self._maintenance_llm_call_any(
            maint.build_prompt(self.config, catalogs, states, report, usage))
        if reply is not None:
            try:
                parsed = maint.parse_llm_output(reply)
                report["llm_model"] = model
                report["llm_notes"] = parsed["notes"]
                report["suggestions"].extend(parsed["suggestions"])
            except Exception as exc:  # LLM analysis is best-effort
                report["llm_error"] = f"LLM 输出处理失败：{str(exc)[:160]}"
        elif llm_errors:
            # Every candidate was tried and failed: say so, or the report
            # would read as "the analysis found nothing wrong" when in fact
            # no analysis ran at all.
            report["llm_error"] = (
                "全部候选模型分析失败：" + "；".join(llm_errors[:2]))[:300]
        else:
            report["llm_error"] = (
                "本次没有 AI 分析：未找到可用的维护模型"
                "（可点「测试维护模型」探测）")

        report["trigger"] = trigger
        report["finished_at"] = time.time()
        report["channels_total"] = len(self.config.channels)
        self._record_maintenance(report)
        if self.maintenance_runner:
            self.maintenance_runner.note_run(_maintenance_summary(report))
        return report

    def _maintenance_llm_call(self, model: str, prompt: str) -> str:
        """Answer a maintenance prompt with one model id.

        The id is usually an upstream id taken from a channel's own mapping
        (or its catalog), so the owning channel is addressed directly —
        maintenance must not depend on route membership. Only ids that no
        channel exposes (branded names such as facet-auto) fall back to the
        loopback gateway call, i.e. ordinary routing.
        """
        channel = self._maintenance_channel_for(model)
        if channel is not None:
            return self._maintenance_direct_call(channel, model, prompt)
        return self._maintenance_loopback_call(model, prompt)

    def _maintenance_channel_for(self, model: str):
        """First enabled channel whose own mapping exposes this upstream id,
        tier-free channels first, config order otherwise."""
        hits = [c for c in self.config.channels
                if c.enabled and model in c.models.values()
                and not self.free_quota_exhausted(c, model)]
        hits.sort(key=lambda c: c.tier != "free")
        return hits[0] if hits else None

    @staticmethod
    def _http_error_text(exc: HTTPError) -> str:
        """Compact upstream error line: status + server message + retry hint.

        str(HTTPError) alone is just "HTTP Error 429: Too Many Requests";
        vendors such as OpenRouter say which quota fired in the JSON body
        (e.g. free-models-per-day), and that is what a probe report needs.
        """
        detail = ""
        try:
            body = exc.read(2048).decode("utf-8", "replace")
            try:
                data = json.loads(body)
                detail = str((data.get("error") or {}).get("message")
                             or data.get("message") or "").strip()
            except ValueError:
                match = re.search(r'"message"\s*:\s*"([^"]+)"', body)
                detail = (match.group(1) if match else body.strip())[:220]
        except Exception:
            pass
        parts = [f"HTTP Error {exc.code}"]
        if detail:
            parts.append(detail[:220])
        retry_after = (exc.headers or {}).get("Retry-After")
        if retry_after:
            parts.append(f"Retry-After {retry_after}")
        return ": ".join(parts)

    def _maintenance_direct_call(self, channel, model: str, prompt: str,
                                 max_tokens: int = 6000) -> str:
        """One non-streaming chat turn straight against the owning channel.

        The 6000-token default leaves room for the LLM maintenance analysis;
        verify/ping probes pass a tiny budget instead — a reasoning model
        allowed its full thinking budget would burn thousands of grant tokens
        just to say "OK".
        """
        if self.free_quota_exhausted(channel, model):
            raise RuntimeError(
                f"free quota exhausted for {model} on {channel.name}; "
                "maintenance call blocked to avoid billed usage")
        protocol = channel.protocol
        if protocol == ANTHROPIC:
            payload = {"model": model, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}
        else:
            payload = {"model": model, "max_tokens": max_tokens, "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}
        normalize_kimi_temperature(payload)
        headers = {"Content-Type": "application/json"}
        if channel.api_key:
            if protocol == ANTHROPIC:
                headers["x-api-key"] = channel.api_key
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {channel.api_key}"
        apply_upstream_headers(headers, channel.base_url,
            body=payload, api_key=channel.api_key)
        req = Request(
            channel.base_url + upstream_path_for(
                channel.base_url, channel.upstream_path, protocol),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with open_with_proxy(req, max(60, self.config.defaults.timeout),
                                 channel.proxy) as resp:
                data = json.loads(resp.read())
        except HTTPError as exc:
            raise RuntimeError(self._http_error_text(exc)) from None
        # Book the spend like routed traffic: probe pings and maintenance
        # analysis are real completions, and the free-quota ledger
        # (usage.model_usage against the registered face value) must not
        # report a grant as fuller than it is. h stays unset so the overhead
        # lands on no member's costs — it is system traffic, not user traffic.
        # A failed call raises before here and books nothing: upstream bills
        # answered requests only.
        total, input_tokens, cached_tokens = _usage_fields(
            data.get("usage") or {})
        self.record_usage(channel.name, True, total, model=model,
                          input_tokens=input_tokens, cached_tokens=cached_tokens)
        if protocol == ANTHROPIC:
            return "".join(b.get("text") or "" for b in data.get("content") or []
                           if isinstance(b, dict))
        return data["choices"][0]["message"]["content"]

    def _probe_channel(self, channel) -> bool:
        """One cheap liveness probe for the background HealthProber.

        "models" sends GET /v1/models (free, verifies reachability + auth);
        "chat" spends one 1-token completion via the maintenance caller.
        """
        if self.config.defaults.probe_mode == "chat":
            model = pick_model(dict(channel.models) or {})
            if not model:
                return False
            try:
                self._maintenance_direct_call(channel, model, "ping", max_tokens=16)
                return True
            except Exception:
                return False
        raw = {
            "name": channel.name,
            "protocol": channel.protocol,
            "base_url": channel.base_url,
            "api_key": channel.api_key,
            "models": dict(channel.models) or {"auto": "*"},
            "upstream_path": channel.upstream_path,
            "proxy": channel.proxy,
        }
        try:
            return bool(probe_models(raw, timeout=8.0).get("ok"))
        except Exception:
            return False

    def _maintenance_loopback_call(self, model: str, prompt: str) -> str:
        """Ordinary client request through the gateway itself, so the model
        lands on the selected route's channels with normal failover."""
        listen = self.config.listen
        host = "127.0.0.1" if listen.host in ("0.0.0.0", "::", "") else listen.host
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            # Room for reasoning-style free models so the JSON array survives;
            # reasoning is disabled via OpenRouter's unified param when the
            # upstream supports it (ignored otherwise).
            "max_tokens": 6000,
            "reasoning": {"enabled": False, "exclude": True},
            "temperature": 0,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if listen.api_key:
            headers["Authorization"] = f"Bearer {listen.api_key}"
        req = Request(
            f"http://{host}:{listen.port}/v1/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        with urlopen(req, timeout=max(60, self.config.defaults.timeout)) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]

    def _maintenance_llm_call_any(self, prompt: str,
                                  validate=None) -> tuple:
        """Run a maintenance prompt through the first working model.

        Candidates come from defaults.maintenance_model when it lists explicit
        ids, otherwise from the auto-discovered verified queue (see
        maintenance.probe_maintenance_models). A queue model that raises or
        fails validation is dropped on the spot so a dead free model cannot
        break the next run. Returns (reply, model, errors) where errors lists
        why each candidate failed — the maintenance report surfaces them so
        "no AI analysis ran" is visible instead of silent. (None, None, [])
        means no candidate existed at all.
        """
        models, mode = maint.pick_maintenance_models(self)
        errors: list[str] = []
        for model in models:
            try:
                reply = self._maintenance_llm_call(model, prompt)
            except Exception as exc:
                errors.append(f"{model}: {str(exc)[:120]}")
                if mode == "pool":
                    maint.drop_maintenance_model(self, model, str(exc))
                continue
            reply = str(reply or "")
            if reply.strip() and (validate is None or validate(reply)):
                return reply, model, errors
            errors.append(f"{model}: " + (
                "空回复" if not reply.strip() else "输出未通过校验"))
            if mode == "pool":
                maint.drop_maintenance_model(
                    self, model, "空回复" if not reply.strip() else "输出未通过校验")
        return None, None, errors

    def probe_maintenance_models(self) -> dict:
        """Quiz candidate maintenance models now and rebuild the queue."""
        return maint.probe_maintenance_models(self)

    def verify_free_models(self, channels=None, trigger: str = "manual") -> dict:
        """Ping every free-channel mapping; dead ids get an expiring label
        that routing then skips (see maint.dead_model_set).

        channels narrows the sweep to the named free channels, so checking
        one tier does not wait on every other tier's mappings.

        The scheduled sweep and the console button share one gate: with a
        full pool a sweep takes minutes, and two overlapping sweeps would
        double the upstream calls and race on the pool labels. A contender
        arriving mid-sweep queues instead of being dropped.
        """
        if not self.free_verify_lock.acquire():
            # The contender queues on the gate; this branch only fires when
            # a sweep runs past GATE_QUEUE_WAIT.
            return {"skipped": "running", "trigger": trigger, "checked": 0,
                    "alive": 0, "dead": 0, "inconclusive": 0,
                    "scanned_at": 0, "results": [], "dead_models": []}
        try:
            pairs = None
            if channels:
                only = set(channels)
                pairs = [pair for pair in maint.free_mapping_pairs(self)
                         if pair[0].name in only]
            return maint.verify_free_models(self, pairs)
        finally:
            self.free_verify_lock.release()

    def run_change_watch(self, trigger: str = "manual") -> dict:
        """One source-watch sweep (watch.py): fetch, diff, record events.

        The scheduled sweep and the console button share App.watch_lock —
        both fetch the same dozens of URLs, and two overlapping sweeps would
        race on change-snapshots.json and duplicate events.
        """
        if not self.watch_lock.acquire():
            # The contender queues on the gate; this branch only fires when
            # a sweep runs past GATE_QUEUE_WAIT.
            return {"busy": True, "trigger": trigger, "sources": 0,
                    "appended": 0, "events": [], "summary": ""}
        try:
            report = watch_mod.run_watch(self)
            report["trigger"] = trigger
            # A manual run refreshes the task table's last outcome too.
            if trigger == "manual" and self.watch_runner:
                self.watch_runner.note_run(report.get("summary") or "")
            return report
        finally:
            self.watch_lock.release()

    # -------------------------------------------------- benchmark refresh
    def refresh_benchmarks(self, trigger: str = "manual") -> dict:
        """One public-benchmark refresh sweep: fetch Vals TB4, write override.

        The scheduled sweep and the console button share
        App.benchmark_refresh_lock — both fetch the same public page and
        rewrite the same override file, so two overlapping sweeps would race
        on the atomic replace. A contender arriving mid-sweep queues instead
        of being dropped. Never raises; failures land in the returned report
        (and the task table's last_error via the runner).
        """
        if not self.benchmark_refresh_lock.acquire():
            return {"busy": True, "trigger": trigger,
                    "summary": "已有能力分刷新在运行，本次跳过",
                    "matched": {}, "errors": ["busy"]}
        try:
            from .. import benchmark_sync as bs
            live = bench_mod._load("model-scores.json", {"models": {}})
            new_live, report = bs.sync_terminal_bench(live)
            summary = self._apply_benchmark_override(new_live, report)
            report["trigger"] = trigger
            report["summary"] = summary
            if trigger == "manual" and self.benchmark_refresh_runner:
                self.benchmark_refresh_runner.note_run(summary)
            return report
        except Exception as exc:  # noqa: BLE001 — a broken sweep must not kill the gateway
            return {"trigger": trigger, "matched": {},
                    "errors": [f"{exc!r}"],
                    "summary": f"能力分刷新失败：{exc}"}
        finally:
            self.benchmark_refresh_lock.release()

    def _apply_benchmark_override(self, new_live: dict, report: dict) -> str:
        """Persist the refreshed Vals scores to the user-dir override file.

        Only the vals-harness score entries are written, so non-Vals sources
        keep coming from the bundled snapshot (and stay fresh across
        releases). A failed/empty match never blanks the existing file.
        Returns a one-line summary for the task table.
        """
        from .. import benchmark_sync as bs
        matched = report.get("matched") or {}
        errors = report.get("errors") or []
        if not matched:
            if errors:
                return "能力分刷新失败：" + "；".join(errors[:2])
            return "没有匹配到可更新的模型"
        override_models: dict = {}
        for key in matched:
            mdl = (new_live.get("models") or {}).get(key) or {}
            vals_scores = [s for s in (mdl.get("scores") or [])
                           if s.get("bench") == bs.BENCH_ID
                           and s.get("harness") == bs.VALS_HARNESS]
            if vals_scores:
                override_models[key] = {"scores": vals_scores}
        if not override_models:
            return "没有可写入的分数"
        override = {"models": override_models}
        if not self.config_path:
            bench_mod.clear_cache()
            return f"已刷新 {len(override_models)} 个模型（未持久化）"
        path = self.config_path.parent / "model-scores.override.json"
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(override, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            self._note_write_failure("benchmark_override", exc)
            return f"能力分写入失败：{exc}"
        self._note_write_ok("benchmark_override")
        bench_mod.clear_cache()
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "trigger": report.get("trigger"),
            "matched": len(override_models),
            "changes": dict(matched),
            "errors": errors,
        }
        append_jsonl(self.config_path.parent / "benchmark_refresh.jsonl", entry,
                     "benchmark_refresh_log", on_error=self._note_write_failure,
                     on_ok=self._note_write_ok)
        names = ", ".join(sorted(override_models)[:5])
        more = f" 等 {len(override_models)} 个" if len(override_models) > 5 else ""
        return f"已刷新 {len(override_models)} 个模型的能力分：{names}{more}"

    def changes_payload(self, limit: int = watch_mod.EVENTS_VIEW_LIMIT) -> dict:
        """Console payload: change events (newest first) + per-source state."""
        return watch_mod.changes_payload(self, limit)

    def mark_changes_read(self, ids: list[str], read: bool = True) -> dict:
        """Flip read flags on change events; ids live in the snapshots file."""
        changed = watch_mod.mark_read(self, ids, read)
        return {"ok": True, "changed": changed}

    def set_changes_muted(self, keys: list[str], muted: bool = True) -> dict:
        """Silence (or resume) change reporting for source keys."""
        return watch_mod.set_muted(self, keys, muted)

    def map_change_models(self, payload: dict) -> dict:
        """Map a ``new_model`` event's models into one channel, verbatim.

        Explicitly operator-driven: the console picks the channel (the
        backend only suggests candidates per event), and the write goes
        through the same path maintenance suggestions use. The event is
        marked read on success so the card reflects what was handled.
        """
        if not isinstance(payload, dict):
            raise ValueError("body 必须是 JSON 对象")
        event_id = str(payload.get("id") or "").strip()
        if not event_id:
            raise ValueError("需要事件 id")
        event = next((e for e in watch_mod.load_events(self, 500)
                      if e.get("id") == event_id), None)
        if event is None:
            raise ValueError("事件不存在或已被日志轮转")
        if event.get("kind") != "new_model":
            raise ValueError("只有「新增模型」事件可以直接映射进渠道")
        models = payload.get("models")
        if not isinstance(models, list) or not models:
            models = list((event.get("detail") or {}).get("added") or [])
        if not models:
            raise ValueError("事件没有可映射的模型")
        channel = str(payload.get("channel") or "").strip()
        if not channel:
            targets = watch_mod.map_targets(self, event.get("source") or {})
            if len(targets) == 1:
                channel = targets[0]
            else:
                raise ValueError("请指定目标渠道" if targets
                                 else "该信息源没有关联的渠道")
        result = self.apply_maintenance_suggestion(
            {"channel": channel, "models": models})
        result["changed_read"] = watch_mod.mark_read(self, [event_id])
        return result

    def dsh_status(self) -> dict:
        """Report whether the DeepSeek Harness client shares this host."""
        state = dsh_mod.status()
        base_url = state.get("base_url") or ""
        if base_url:
            host = (self.config.listen.host or "").strip()
            if host in ("", "0.0.0.0", "::"):
                host = "127.0.0.1"
            own = f"http://{host}:{self.config.listen.port}/v1"
            if base_url.rstrip("/") != own:
                # Profile seeded on an earlier port: the console shows the
                # stale address and the next 启动 DSH rewrites it in place.
                state["stale_base_url"] = True
                state["note"] = (f"DSH 档案还指向旧地址 {base_url}：点一次"
                                 f"「启动 DSH」会自动改写为 {own} 并重启。")
        return state

    def dsh_models(self) -> list[dict]:
        """Catalogue entries the DSH picker should offer.

        DSH builds its model list from the profile's static `models` block, so
        the console publishes auto ids and backend ids side by side there. Left
        out: the bare `auto` / `auto:<route>` / `facet:<route>` aliases (they
        schedule the same chains as the branded ids and only duplicate them),
        and models the free-pool probe already failed — offering a known
        broken id just produces errors in the picker.

        Free-tier models must additionally carry a passing probe result: the
        daily maintenance sweep tests them and a free channel's mappings are
        mostly unverified, so untested ones stay hidden until the sweep (or
        the per-model 测试 button) records a pass.

        The probe records results per *upstream* id (the mapping value) while
        /v1/models exposes the mapping key, so each id is resolved through its
        channel before the check.
        """
        branded = set(self.config.models.branded)
        with self.maintenance_pool_lock:
            tests = dict(self.maintenance_pool.get("tests") or {})
        channels = {channel.name: channel for channel in self.config.channels}
        entries: list[dict] = []
        for item in self.model_list()["data"]:
            name = item["id"]
            if item["mode"] == "auto":
                if name not in branded:
                    continue
            else:
                channel = channels.get(item["owned_by"])
                wire = channel.models.get(name) if channel else None
                test = tests.get(wire) if wire else None
                free = channel is not None and (
                    (channel.tiers.get(name) or channel.tier) == "free")
                if test and not test.get("ok"):
                    continue
                if free and not (test and test.get("ok")):
                    continue
            entries.append({"id": name, "mode": item["mode"],
                            "owned_by": item["owned_by"]})
        return entries

    def start_dsh_web(self, port=None) -> dict:
        """Spawn DSH's facet-web profile and return its tokenised URL.

        The gateway key is handed over as ROUTER_API_KEY, which the profile's
        patch layer reads, so the spawned client can authenticate without the
        operator copying a secret into its config. A fresh install ships no
        profile, so it is seeded pointed at this gateway before the model
        list is refreshed: auto modes and real backend models stay selectable
        together, and channels enabled since the last launch show up.
        """
        host = (self.config.listen.host or "").strip()
        if host in ("", "0.0.0.0", "::"):
            host = "127.0.0.1"
        base_url = f"http://{host}:{self.config.listen.port}"
        dsh_mod.ensure_profile(base_url=base_url)
        dsh_mod.sync_models(self.dsh_models())
        return dsh_mod.start_web(api_key=self.config.listen.api_key, port=port,
                                 base_url=base_url)

    def _backup_config(self, tag: str) -> None:
        if not self.config_path or not self.config_path.is_file():
            return
        try:
            backup = self.config_path.with_name(self.config_path.name + f".{tag}.bak")
            backup.write_bytes(self.config_path.read_bytes())
        except OSError as exc:
            # A failed backup must not block maintenance, but it removes the
            # rollback net for an auto-apply: report it as a runtime alert.
            self._note_write_failure("config_backup", exc)
        else:
            self._note_write_ok("config_backup")

    def _record_maintenance(self, report: dict) -> None:
        """Keep the last reports in memory and append one JSON line to disk."""
        with self.maintenance_lock:
            self.maintenance_reports.append(report)
            del self.maintenance_reports[:-MaintenanceRunner.MAX_REPORTS]
        if not self.config_path:
            return
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                 **{k: v for k, v in report.items() if k != "patch"}}
        append_jsonl(self.config_path.parent / "maintenance.jsonl", entry,
                     "maintenance_log", on_error=self._note_write_failure,
                     on_ok=self._note_write_ok)

    def _load_report_history(self, name: str) -> list[dict]:
        """Oldest-first tail of a report log, so a restart keeps the cards.

        Memory held only the reports produced since process start; the JSONL
        log already has them, so the console's latest-report view no longer
        starts empty (and stays right even before the first sweep runs).
        """
        if not self.config_path:
            return []
        lines, _ = maint.read_jsonl_tail(self.config_path.parent / name,
                                         MaintenanceRunner.MAX_REPORTS)
        reports = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                reports.append(entry)
        return reports

    # ----------------------------------------------------------- free align
    def run_free_align(self, trigger: str = "manual") -> dict:
        """Align free-tier channel mappings with vendor free-quota tools.

        Applies the sweep's patch through the normal config pipeline (after a
        config backup); never raises — failures land in the report alerts and
        the channels keep their current mapping until the next sweep.

        Shares App.config_apply_lock with run_maintenance: both passes rewrite
        channel mappings through apply_config, so a manual click during a
        scheduled run (either direction) queues behind it instead of
        interleaving.
        """
        if not self.config_apply_lock.acquire():
            # The contender queues on the gate; this branch only fires when
            # a pass runs past GATE_QUEUE_WAIT.
            return {"busy": True, "trigger": trigger, "aligned": [],
                    "patch": {}, "alerts": [], "quota": {},
                    "message": f"上一次自维护仍在运行（已排队等待"
                               f"{int(GATE_QUEUE_WAIT)}秒），本次对齐已跳过"}
        try:
            return self._run_free_align(trigger)
        finally:
            self.config_apply_lock.release()

    def _run_free_align(self, trigger: str) -> dict:
        report = freealign_mod.run_free_align(self, trigger)
        if report["patch"]:
            self._backup_config("freealign")
            raw = copy.deepcopy(self.raw)
            for channel in raw.get("channels") or []:
                if isinstance(channel, dict) \
                        and channel.get("name") in report["patch"]:
                    channel["models"] = report["patch"][channel["name"]]
            try:
                self.apply_config(raw)
            except (ValueError, OSError) as exc:
                report["patch"] = {}
                report["alerts"].append({
                    "channel": "", "kind": "apply_failed",
                    "message": f"自动应用免费额度对齐失败，配置已回读未提交：{exc}",
                })
        if report.get("quota"):
            # Merge: a channel whose vendor command failed keeps its last
            # known numbers (and the alert above explains the staleness).
            self.vendor_quota.update(report["quota"])
            self.vendor_quota_at = report.get("quota_at") or self.vendor_quota_at
            # Freshness stamp per refreshed channel: a channel the sweep
            # keeps failing to read stops being stamped, ages past one sweep
            # interval and gets frozen by free_quota_status until a sweep
            # succeeds again — stale numbers must not keep dispatch open.
            now = time.time()
            for name in report["quota"]:
                self.vendor_synced[str(name)] = now
            # Re-seed the self-metered budget from the fresh numbers so the
            # dispatch gate stops extrapolating from the previous sweep.
            self._seed_free_budget(report["quota"])
            self._save_vendor_quota()
        report["finished_at"] = time.time()
        self._record_free_align(report)
        if self.free_align_runner:
            self.free_align_runner.note_run(_free_align_summary(report))
        return report

    def _record_free_align(self, report: dict) -> None:
        with self.maintenance_lock:
            self.free_align_reports.append(report)
            del self.free_align_reports[:-MaintenanceRunner.MAX_REPORTS]
        if not self.config_path:
            return
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                 **{k: v for k, v in report.items() if k != "patch"}}
        append_jsonl(self.config_path.parent / "freealign.jsonl", entry,
                     "freealign_log", on_error=self._note_write_failure,
                     on_ok=self._note_write_ok)

    def maintenance_payload(self) -> dict:
        with self.maintenance_lock:
            reports = [
                {k: v for k, v in report.items() if k != "patch"}
                for report in self.maintenance_reports
            ]
            free_reports = [
                {k: v for k, v in report.items() if k != "patch"}
                for report in self.free_align_reports
            ]
        with self.maintenance_pool_lock:
            pool = {k: (list(v) if isinstance(v, list) else v)
                    for k, v in self.maintenance_pool.items()}
        return {
            "enabled": bool(self.config.defaults.maintenance_hours),
            "hours": self.config.defaults.maintenance_hours,
            "llm_model": self.config.defaults.maintenance_model,
            "llm_available": bool(
                (self.config.defaults.maintenance_model or pool.get("queue"))
                and any(c.enabled for c in self.config.channels)),
            "llm_queue": list(pool.get("queue") or []),
            "llm_queue_verified_at": float(pool.get("verified_at") or 0),
            "llm_queue_results": list(pool.get("results") or []),
            "dead_models": maint.dead_models_payload(self),
            "dead_ttl_hours": maint.dead_label_ttl(self) / 3600,
            "free_scan_at": float(pool.get("free_scan_at") or 0),
            "free_models": maint.free_models_payload(self),
            "reports": list(reversed(self._without_ignored(reports))),
            "free_align": {
                "enabled": bool(self.config.defaults.free_align_hours),
                "hours": self.config.defaults.free_align_hours,
                "reports": list(reversed(free_reports)),
            },
            "free_verify": {
                "enabled": bool(self.config.defaults.free_verify_hours),
                "hours": self.config.defaults.free_verify_hours,
            },
            "notify": self._notify_delivery(),
        }

    def _notify_delivery(self) -> dict:
        """Whether runtime alerts can actually reach anyone (see A1)."""
        if not self.notifier:
            return {"ok": False, "reason": "disabled",
                    "hint": "通知服务未启动（启动时未指定 notifications 文件），运行告警不会外发。"}
        return self.notifier.delivery()

    def _notifier_task(self) -> dict:
        """ReminderService loop as a scheduled-task row."""
        status = self.notifier.status()
        delivery = status.get("delivery") or {}
        interval = max(1, int((status.get("settings") or {}).get("interval_minutes") or 30))
        last = float(status.get("last_check") or 0)
        last_error = status.get("last_error") or ""
        # A configured webhook that keeps failing to deliver is not "ok": the
        # delivery check only proves the settings are complete.
        if not delivery.get("ok"):
            state = "warn"
            summary = f"告警无法外发：{delivery.get('hint') or '未配置'}"
        elif last_error:
            state = "warn"
            summary = f"告警外发失败：{last_error[:160]}"
        else:
            state = "ok"
            summary = "告警外发正常"
        return {
            "key": "reminder",
            "label": "续费与运行告警通知",
            "description": "每分钟扫描运行告警，按订阅设置检查到期提醒",
            "enabled": bool(status.get("settings", {}).get("notify_enabled")),
            "hours": 0,
            "cycle": f"运行告警每 60 秒 · 到期提醒每 {interval} 分钟",
            "state": state,
            "last_run_at": last,
            "next_run_at": last + 60 if last else 0,
            "last_summary": summary,
            "last_error": last_error,
        }

    def tasks_payload(self) -> dict:
        """The console's task list: cycle, latest outcome and next run."""
        runners = (self.catalog_refresher, self.maintenance_runner,
                   self.free_align_runner, self.free_verify_runner,
                   self.watch_runner, self.modelscope_checkin_runner,
                   self.benchmark_refresh_runner)
        tasks = [runner.status() for runner in runners if runner]
        if self.notifier:
            tasks.append(self._notifier_task())
        prober = getattr(self, "health_prober", None)
        if prober is not None:
            tasks.append(prober.status())
        return {"tasks": tasks}

    def recommend_models(self, purpose: str = "agent",
                         limit: int = 20) -> dict:
        """Pick models from every channel's catalog that suit a purpose.

        "agent" buckets models by their registry tool-call capability:
        known-supporting first, then unknown (a catalog id the bundled
        registry does not recognise), then known-not-supporting. Cooled
        ("dead") models are excluded outright — an agent cannot use them.
        """
        import time as _time

        facts = facts_mod.snapshot()
        dead = maint.dead_model_set(self)
        blocked = set(self.blocked_channels())
        candidates: dict[str, dict] = {}

        for channel in self.config.channels:
            if not channel.enabled or channel.name in blocked:
                continue
            catalog = self.store.catalog_view(channel.name)
            for wire in catalog.get("models") or []:
                if (channel.name, wire) in dead:
                    continue
                if self.free_quota_exhausted(channel, wire):
                    continue
                key = facts.resolve(wire) if facts.available else None
                rec = facts.record(key) if key else {}
                tier = model_tier(channel, self.subscriptions, wire)
                score = self.store.scores_for(channel.name).get(wire, 0)
                # 内置榜单的 Agent 能力分（benchmarks.py，0-100）：同档内
                # 得分高的排前，榜单未收录（None）的排在有得分的之后。
                bench_score = bench_mod.agent_score(key) if key else None
                row = {
                    "wire_model": wire,
                    "channel": channel.name,
                    "registry_key": key or "",
                    "matched": bool(rec),
                    "tier": tier,
                    "tool_call": (rec.get("tool_call")
                                  if isinstance(rec, dict) and rec else None),
                    "context_window": rec.get("context_window") if rec else None,
                    "cost": rec.get("cost") or {} if rec else {},
                    "agent_score": bench_score,
                    "score": int(score or 0),
                }
                existing = candidates.get(wire)
                if existing is None or _row_better(existing, row):
                    candidates[wire] = row

        if purpose == "agent":
            # Models the registry says cannot call tools are unusable for an
            # agent, so they never reach this list (and never reach runtime
            # selection either — see _agent_candidates).
            rows = [row for row in candidates.values() if row["tool_call"] is not False]

            def bucket(row: dict) -> int:
                return 0 if row["tool_call"] is True else 1

            rows.sort(
                key=lambda r: (
                    bucket(r),
                    0 if r["tier"] == "free" else 1,
                    r["tier"],
                    0 if r["agent_score"] is not None else 1,
                    -(r["agent_score"] or 0.0),
                    -r["score"],
                    -(r["context_window"] or 0),
                    r["wire_model"],
                ),
            )
        else:
            rows = sorted(
                candidates.values(),
                key=lambda r: (
                    0 if r["tier"] == "free" else 1,
                    r["tier"],
                    -r["score"],
                    r["wire_model"],
                ),
            )

        rows = rows[: max(1, int(limit))] if limit else rows
        return {
            "purpose": purpose,
            "generated_at": _time.time(),
            "coverage": facts.coverage() if facts.available else {},
            "items": rows,
            "unknown_capability": sum(1 for r in rows if not r["matched"]),
        }

    def _agent_candidates(self) -> list[tuple[str, str]]:
        """Ranked (channel_name, wire_model) pairs for facet-agent, best first.

        Same ranking as the console's "Agent 模型推荐" table so the attempts
        logged on the request page read top-down like that table: models the
        bundled registry knows can call tools first, capability-unknown next,
        and models it says *cannot* call tools are dropped outright — an
        agent cannot drive them. Within one tool/tier group the bundled
        benchmark table's aggregate agent score orders models (see
        benchmarks.agent_score); models without a score rank after scored
        ones, then stability, context and name break ties. Dead
        (probe-verified) models and disabled or blocked channels are
        skipped too.
        """
        facts = facts_mod.snapshot()
        dead = maint.dead_model_set(self)
        blocked = set(self.blocked_channels())
        rows: list[tuple[tuple, str, str]] = []
        for channel in self.config.channels:
            if not channel.enabled or channel.name in blocked:
                continue
            scores = self.store.scores_for(channel.name)
            for wire in (self.store.catalog_view(channel.name).get("models") or []):
                if (channel.name, wire) in dead:
                    continue
                if self.free_quota_exhausted(channel, wire):
                    continue
                key = facts.resolve(wire) if facts.available else None
                rec = facts.record(key) if key else {}
                tool = rec.get("tool_call") if isinstance(rec, dict) and rec else None
                if tool is False:
                    continue
                tier = model_tier(channel, self.subscriptions, wire)
                bench_score = bench_mod.agent_score(key) if key else None
                rows.append((
                    (0 if tool is True else 1,
                     0 if tier == "free" else 1,
                     tier,
                     0 if bench_score is not None else 1,
                     -(bench_score or 0.0),
                     -int(scores.get(wire, 0)),
                     -int(rec.get("context_window") or 0) if rec else 0,
                     wire,
                     channel.name),
                    channel.name,
                    wire,
                ))
        rows.sort(key=lambda row: row[0])
        return [(name, wire) for _, name, wire in rows]

    def _jsonl_history_payload(self, filename: str, memory_reports: list,
                               limit: int = 50) -> dict:
        """Newest-first run history from a JSONL log (maintenance / free-align).

        Reads the file tail so a long history cannot grow the response; falls
        back to the in-memory reports when no log file exists (e.g. tests).
        """
        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit = 50
        entries: list[dict] = []
        source = "memory"
        path = (self.config_path.parent / filename
                if self.config_path else None)
        if path is not None and path.is_file():
            source = "file"
            raw_lines, has_more = maint.read_jsonl_tail(path, limit)
            for line in raw_lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # skip torn or hand-edited lines
                if isinstance(entry, dict):
                    entries.append(entry)
            entries.reverse()
        else:
            with self.maintenance_lock:
                reports = [
                    {k: v for k, v in report.items() if k != "patch"}
                    for report in memory_reports
                ]
            has_more = len(reports) > limit
            entries = list(reversed(reports[-limit:]))
        return {"logs": self._without_ignored(entries), "source": source,
                "has_more": has_more, "limit": limit}

    def maintenance_log_payload(self, limit: int = 50) -> dict:
        """History of maintenance runs, newest first, from maintenance.jsonl."""
        return self._jsonl_history_payload(
            "maintenance.jsonl", self.maintenance_reports, limit)

    def free_align_log_payload(self, limit: int = 50) -> dict:
        """History of free-quota alignment runs, from freealign.jsonl."""
        return self._jsonl_history_payload(
            "freealign.jsonl", self.free_align_reports, limit)

    def backup_list_payload(self) -> dict:
        """Read-only list of config backups (router.json.<tag>.bak), newest first.

        Backups are written by _backup_config before maintenance/free-align
        rewrite the config; restore_backup (the console's 恢复 button) swaps
        one back in through the validated config pipeline.
        """
        backups: list[dict] = []
        if self.config_path and self.config_path.parent.is_dir():
            prefix = self.config_path.name + "."
            try:
                candidates = list(self.config_path.parent.iterdir())
            except OSError:
                candidates = []
            for path in candidates:
                name = path.name
                if (not name.startswith(prefix) or not name.endswith(".bak")
                        or len(name) <= len(prefix) + len(".bak")):
                    continue
                try:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                backups.append({
                    "file": name,
                    "tag": name[len(prefix):-len(".bak")],
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                })
        backups.sort(key=lambda b: b["mtime"], reverse=True)
        return {"backups": backups,
                "config": self.config_path.name if self.config_path else None}

    def test_maintenance_model(self, payload) -> dict:
        """Manual quiz for one (channel, model) from the console overview."""
        if not isinstance(payload, dict):
            raise ValueError("body 必须是对象")
        return maint.test_single_model(
            self, str(payload.get("channel") or ""),
            str(payload.get("model") or ""))

    def retest_failed_models(self) -> dict:
        """Quiz every model whose last quiz failed, in one batch."""
        return maint.retest_failed_models(self)

    def apply_maintenance_suggestion(self, payload) -> dict:
        """One-click apply for `add_model` suggestions: map the model verbatim."""
        if not isinstance(payload, dict):
            raise ValueError("body 必须是对象")
        channel_name = payload.get("channel")
        models = payload.get("models")
        if isinstance(models, str):
            models = [models]
        if (not isinstance(channel_name, str) or not channel_name.strip()
                or not isinstance(models, list) or not models):
            raise ValueError("需要 channel 与 models（模型名数组）")
        if any(not isinstance(m, str) or not m.strip() for m in models):
            raise ValueError("模型名必须是非空字符串")
        if self.config.channel(channel_name) is None:
            raise ValueError(f"渠道 {channel_name} 不存在")
        raw = copy.deepcopy(self.raw)
        added = []
        for channel in raw.get("channels") or []:
            if isinstance(channel, dict) and channel.get("name") == channel_name:
                mapping = channel.get("models") or {}
                for m in models:
                    key = m.strip()
                    if mapping.get(key) != key:
                        added.append(key)
                    mapping[key] = key
                channel["models"] = mapping
                break
        if added:
            # A real change is about to land: snapshot the config first so a
            # bad adopt is undoable from the console's backup list. An adopt
            # that adds nothing is a no-op and keeps the previous backup.
            self._backup_config("suggestion")
            self.apply_config(raw)
        return {"ok": True, "channel": channel_name,
                "models": [m.strip() for m in models]}

    # ------------------------------------------------------- suggestions
    def _load_ignored_suggestions(self) -> list[str]:
        """Signatures of suggestions the operator rejected (忽略 button)."""
        if not self.config_path:
            return []
        path = self.config_path.with_name("maintenance-ignored.json")
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        sigs = data.get("signatures") if isinstance(data, dict) else None
        return [str(s) for s in (sigs or []) if s]

    def _without_ignored(self, reports: list[dict]) -> list[dict]:
        """Report copies with operator-ignored suggestions dropped.

        The JSONL log keeps every suggestion (it is what actually ran); the
        console hides the ones the operator said no to, in both the
        latest-report card and the run history.
        """
        ignored = set(self._load_ignored_suggestions())
        out = []
        for report in reports:
            copy = dict(report)
            copy["suggestions"] = [
                s for s in (report.get("suggestions") or [])
                if maint.suggestion_signature(s) not in ignored]
            out.append(copy)
        return out

    def ignore_maintenance_suggestion(self, payload) -> dict:
        """Remember one rejected suggestion; the console stops showing it.

        The signature is computed server-side from the suggestion itself, so
        a later run that regenerates the same item (same kind + channel +
        models) is filtered too. Newest 200 signatures are kept.
        """
        if not isinstance(payload, dict) or not payload.get("kind"):
            raise ValueError("body 必须携带建议（kind）")
        if not self.config_path:
            raise ValueError("配置路径未知")
        sig = maint.suggestion_signature(payload)
        path = self.config_path.with_name("maintenance-ignored.json")
        sigs = self._load_ignored_suggestions()
        if sig not in sigs:
            sigs.append(sig)
        sigs = sigs[-200:]
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({"signatures": sigs}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        os.replace(tmp, path)
        return {"ok": True, "signature": sig}

    def restore_backup(self, tag) -> dict:
        """Swap the config back to one console-written backup.

        The current config is snapshotted first (tag "restore") so a bad
        restore is itself revertible, and apply_config revalidates the
        backup before anything touches the live file. The tag is matched
        against a strict name pattern: it only ever selects
        ``<config>.<tag>.bak`` next to the config, never an arbitrary path.
        """
        if not self.config_path:
            raise ValueError("配置路径未知")
        tag = str(tag or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", tag):
            raise ValueError("无效的备份标签")
        source = self.config_path.with_name(
            self.config_path.name + f".{tag}.bak")
        if not source.is_file():
            raise ValueError(f"备份不存在：{tag}")
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"备份文件无法解析：{exc}")
        if not isinstance(raw, dict):
            raise ValueError("备份内容不是配置对象")
        self._backup_config("restore")
        self.apply_config(raw)
        return {"ok": True, "tag": tag, "restored": source.name}

    # ------------------------------------------------------------ migration
    def migration_export_payload(self) -> dict:
        """One zip with every file the gateway persists beside the config.

        A config backup (or the masked config export) was never enough to
        move a deployment: member keys, subscription credentials and usage
        history live in their own files next to router.json. The bundle
        carries them in plaintext — it is a migration artifact to copy to the
        target machine, not something to email around. Import never lets the
        bundled management password replace the local one (see
        migration_import), so leaking it still cannot hijack a live console.
        """
        if not self.config_path or not self.config_path.is_file():
            raise ValueError("未找到配置文件，无法导出迁移包")
        files: list[Path] = [self.config_path]
        for optional in (self.keys.path if self.keys else None,
                         self.subscriptions_path,
                         self.usage.path if self.usage else None):
            if optional is not None and optional.is_file():
                files.append(optional)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                zf.write(path, arcname=path.name)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return {
            "filename": f"facet-migration-{stamp}.zip",
            "data": base64.b64encode(buf.getvalue()).decode("ascii"),
            "contents": [{"name": p.name, "size": p.stat().st_size}
                         for p in files],
            "note": "迁移包含明文密钥（渠道、成员 Key），请妥善保管，不要外发",
        }

    def migration_import(self, payload) -> dict:
        """Restore a bundle produced by migration_export_payload, hot.

        Only the four known file names are extracted — a crafted zip member
        can never write outside the config dir, and junk entries are ignored.
        The config goes through the validated apply_config pipeline (a bad
        bundle is rejected before anything is touched); every replaced file
        is snapshotted as ``<name>.migration.bak`` first so the import is
        itself revertible. The local admin_key always survives: the importing
        operator would otherwise be locked out by the source machine's
        management password.
        """
        if not isinstance(payload, dict):
            raise ValueError("body 必须是对象")
        data = str(payload.get("data") or "")
        if not data:
            raise ValueError("缺少迁移包数据 data")
        try:
            blob = base64.b64decode(data, validate=True)
        except ValueError as exc:
            raise ValueError(f"迁移包数据不是有效的 base64：{exc}")
        members: dict[str, bytes] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                for name in ("router.json", "keys.json",
                             "subscriptions.json", "usage.json"):
                    if name in zf.namelist():
                        members[name] = zf.read(name)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"迁移包不是有效的 zip 文件：{exc}")
        if "router.json" not in members:
            raise ValueError("迁移包缺少 router.json，不是有效的迁移包")
        try:
            raw = json.loads(members["router.json"].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"迁移包内的 router.json 无法解析：{exc}")
        if not isinstance(raw, dict):
            raise ValueError("迁移包内的 router.json 不是配置对象")

        imported: list[str] = []
        skipped: list[str] = []
        warnings: list[str] = []

        def snapshot(path: Path) -> None:
            shutil.copyfile(path, path.with_name(path.name + ".migration.bak"))

        def write_atomic(path: Path, blob_: bytes) -> None:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(blob_)
            os.replace(tmp, path)

        # ---- config: validated pipeline, local management password kept
        if not self.config_path:
            raise ValueError("未找到配置文件，无法导入迁移包")
        if self.config_path.is_file():
            self._backup_config("migration")
        listen = dict(raw.get("listen") or {})
        if self.config.listen.admin_key:
            listen["admin_key"] = self.config.listen.admin_key
            warnings.append("管理密码保持本机不变，迁移包内的密码未生效")
        raw["listen"] = listen
        self.apply_config(raw)
        imported.append("router.json")

        # ---- member keys
        keys_blob = members.get("keys.json")
        if keys_blob is not None and self.keys is not None:
            try:
                json.loads(keys_blob.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError(f"迁移包内的 keys.json 无法解析：{exc}")
            path = self.keys.path
            if path.is_file():
                snapshot(path)
            write_atomic(path, keys_blob)
            fresh = KeyStore(path)
            with self.keys.lock:
                self.keys.records = fresh.records
            imported.append("keys.json")
        elif keys_blob is not None:
            skipped.append("keys.json")

        # ---- subscription accounts
        subs_blob = members.get("subscriptions.json")
        if subs_blob is not None and self.subscriptions_path is not None:
            try:
                document = json.loads(subs_blob.decode("utf-8"))
                records = subs.normalize_all(
                    document.get("subscriptions")
                    if isinstance(document, dict) else document)
                settings = subs.normalize_settings(
                    document.get("settings") if isinstance(document, dict) else None)
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError(f"迁移包内的 subscriptions.json 无法采用：{exc}")
            path = self.subscriptions_path
            if path.is_file():
                snapshot(path)
            write_atomic(path, subs_blob)
            self.subscriptions = records
            self.subscription_settings = settings
            self._migrate_subscription_channels(self.subscriptions)
            self._refresh_billing_ranks()
            imported.append("subscriptions.json")
        elif subs_blob is not None:
            skipped.append("subscriptions.json")

        # ---- usage history
        usage_blob = members.get("usage.json")
        if usage_blob is not None and self.usage is not None:
            try:
                document = json.loads(usage_blob.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError(f"迁移包内的 usage.json 无法解析：{exc}")
            if not (isinstance(document, dict) and isinstance(document.get("days"), dict)):
                raise ValueError("迁移包内的 usage.json 不是用量数据")
            path = self.usage.path
            if path.is_file():
                snapshot(path)
            write_atomic(path, usage_blob)
            with self.usage.lock:
                self.usage.data = document
                self.usage.dirty = True
            imported.append("usage.json")
        elif usage_blob is not None:
            skipped.append("usage.json")

        return {"ok": True, "imported": imported, "skipped": skipped,
                "warnings": warnings}

    def _apply_catalog_filter(self, candidates, decision) -> tuple[list, list]:
        """Soft validation of candidates against recorded upstream catalogs.

        A catalog is a *known list*, not a whitelist: unknown names are still
        passed through, but a channel whose catalog provably lacks the wire
        model is skipped to pre-empt a doomed upstream call. Internal
        scheduling (auto / agent) is exempt — the wire model there is the
        channel's own configured mapping or a catalog-enumerated model, not
        a client-supplied name.
        """
        if decision.mode not in ("exact", "fallback"):
            return candidates, []
        kept, skipped = [], []
        for channel, wire in candidates:
            models = (self.store.catalogs.get(channel.name) or {}).get("models") or []
            if models and wire not in models:
                skipped.append({"channel": channel.name, "wire_model": wire,
                                "reason": "model_not_in_catalog"})
                continue
            kept.append((channel, wire))
        return kept, skipped

    def _apply_dead_filter(self, candidates) -> tuple[list, list]:
        """Skip channel mappings a verification run proved unusable.

        Labels are per channel+wire and expire (maintenance.dead_label_ttl),
        so an upstream recovery re-admits the model without a config edit.
        Applies to every routing mode: in internal scheduling the wire model
        is the channel's own mapping, which is exactly what was verified.
        When everything on the list is labelled dead the filter backs off —
        a stale label must not turn a recoverable outage into a hard 503.
        """
        dead = maint.dead_model_set(self)
        if not dead:
            return candidates, []
        kept, skipped = [], []
        for channel, wire in candidates:
            if (channel.name, wire) in dead:
                skipped.append({"channel": channel.name, "wire_model": wire,
                                "reason": "model_marked_dead"})
                continue
            kept.append((channel, wire))
        if candidates and not kept:
            return candidates, []
        return kept, skipped

    def _free_channel_substitute(self, channel, wire: str) -> str:
        """Another free model of the same channel with headroom left ("" if none).

        Only models the vendor meters in tokens like the free baseline qualify:
        image/3D/video models report a different unit (``total`` 50 or 2000000),
        so they are never silently swapped in for a chat request.
        """
        best, best_ratio = "", None
        for alt in dict.fromkeys(str(w) for w in channel.models.values()):
            if not alt or alt in ("*", wire):
                continue
            status = self.free_quota_status(channel, alt)
            if status is None or status["exhausted"]:
                continue
            spec = self.free_quota_spec(channel, alt)
            vendor_total = status.get("vendor_total")
            if (spec is not None and spec.tokens and vendor_total is not None
                    and int(vendor_total) != int(spec.tokens)):
                continue
            ratio = float(status.get("ratio") or 0.0)
            if best_ratio is None or ratio < best_ratio:
                best, best_ratio = alt, ratio
        return best

    def _spare_after_failure(self, channel, wire: str, decision, swapped: set) -> str:
        """Another model of the same channel after a model-scoped failure.

        A vendor can withdraw one model's free grant at request time (HTTP 403
        "Free quota exhausted") while its siblings still have headroom. The
        attempt loop walks a fixed candidate list, so without a spare the
        whole channel was lost for that request; in auto/agent mode one
        sibling is tried before the router moves on. A client-pinned model
        (exact/fallback) is never swapped, and each channel is swapped at
        most once per request so a channel full of dead ids cannot stall the
        failover.
        """
        if getattr(decision, "mode", "") not in ("auto", "agent"):
            return ""
        if channel.name in swapped:
            return ""
        swapped.add(channel.name)
        return self._free_channel_substitute(channel, wire)

    def _apply_free_quota_filter(self, candidates, decision) -> tuple[list, list]:
        """Hard stop once a free model has used its allowance.

        A spent mapping chosen by internal scheduling (auto/agent) is replaced
        by another model of the same channel that still has free headroom: one
        exhausted model must not freeze the whole free channel. A model the
        client pinned by name (exact/fallback mode) is never swapped. Unlike
        the catalog/dead filters this never backs off without a substitute: the
        whole point is to prevent a free channel from silently switching to
        per-call billing once the vendor's grant is gone.
        """
        if not candidates:
            return candidates, []
        can_swap = getattr(decision, "mode", "") in ("auto", "agent")
        kept, skipped = [], []
        for channel, wire in candidates:
            status = self.free_quota_status(
                channel, wire, getattr(decision, "exposed", "") or "")
            if not (status and status["exhausted"]):
                kept.append((channel, wire))
                continue
            alt = self._free_channel_substitute(channel, wire) if can_swap else ""
            entry = {
                "channel": channel.name,
                "wire_model": wire,
                "reason": "free_quota_exhausted",
                "quota": status,
            }
            if alt:
                entry["substituted"] = alt
                kept.append((channel, alt))
            skipped.append(entry)
        return kept, skipped

    # ---------------------------------------------------- billing policy
    def _billing_label(self, channel) -> str:
        """Free / plan / payg bucket for one channel, for audit + console.

        tier="free" wins outright; a channel linked to a subscription record
        bills against that plan; everything else is per-call pay-as-you-go.
        Expired subscriptions never reach the attempt loop (blocked_channels),
        so "plan" here always means an active one.
        """
        if channel.tier == "free":
            return "free"
        ref = channel_ref(channel)
        for record in self.subscriptions:
            refs = {str(r) for r in (record.get("channels") or [])}
            if ref in refs or channel.name in refs:
                return "plan"
        return "payg"

    def _paid_month_tokens(self) -> int:
        """Tokens billed to plan/payg channels so far this calendar month."""
        month = date.today().strftime("%Y-%m")
        labels = {c.name: self._billing_label(c) for c in self.config.channels}
        total = 0
        with self.usage.lock:
            for day, channels in self.usage.data.get("days", {}).items():
                if not str(day).startswith(month):
                    continue
                for name, bucket in channels.items():
                    if labels.get(name) in ("plan", "payg"):
                        total += int(bucket.get("tokens") or 0)
        return total

    def _apply_paid_budget_filter(self, candidates) -> tuple[list, list]:
        """Skip plan/payg candidates once the monthly paid budget is spent.

        defaults.paid_budget_tokens = 0 disables the budget. Free candidates
        always pass; an all-paid route with a spent budget comes back empty
        and the no-candidate reply carries paid_budget_skipped.
        """
        budget = self.config.defaults.paid_budget_tokens
        if budget <= 0 or not candidates:
            return candidates, []
        if self._paid_month_tokens() < budget:
            return candidates, []
        kept, skipped = [], []
        for channel, wire in candidates:
            if self._billing_label(channel) == "free":
                kept.append((channel, wire))
            else:
                skipped.append({"channel": channel.name, "wire_model": wire,
                                "reason": "paid_budget_exhausted"})
        return kept, skipped

    def _note_billing(self, billing: str) -> None:
        """Remember the last served bucket; flag a free→paid transition.

        In-memory only (a restart resets it): the transition alert fires in
        a short window after the switch, so the runtime notifier reports it
        once instead of nagging.
        """
        prev = self._billing_state.get("billing")
        self._billing_state = {"billing": billing,
                               "prev": prev if prev != billing else None,
                               "at": time.time() if (
                                   prev == "free" and billing != "free") else 0.0}

    def _apply_vision_filter(self, candidates) -> tuple[list, list]:
        """Keep only channels that can take an image/document/audio request.

        Capability is config-declared: `vision: false` excludes a channel,
        `vision: [wire, ...]` restricts it to listed models, `vision_model`
        implies capable (the wire was already re-pointed). Unmarked channels
        stay in — an unconfigured gateway forwards as before, and the
        upstream remains the final judge.
        """
        kept, skipped = [], []
        for channel, wire in candidates:
            if not channel_takes_images(channel, wire):
                skipped.append({"channel": channel.name, "wire_model": wire,
                                "reason": "channel_text_only"})
                continue
            kept.append((channel, wire))
        return kept, skipped

    def compare_channel_rows(self) -> list[tuple[str, str, frozenset, bool]]:
        """Rows for the compare page's channel column, from the live config.

        Only enabled channels with a real API key count: a disabled or keyless
        channel cannot serve anything, and a masked-export ``***`` placeholder
        is exactly the fake-usable trap ui_state already reports honestly.
        Row shape: (name, provider, normalized explicit model bases, wildcard).
        """
        rows = []
        for channel in self.config.channels:
            if not channel.enabled or channel.api_key == "***":
                continue
            if not resolve_secret(channel.api_key):
                continue
            bases = frozenset(bench_mod.norm_base(key)
                              for key in channel.models
                              if key not in ("auto", "*"))
            rows.append((channel.name, channel.provider, bases,
                         "*" in channel.models))
        return rows

    def ui_state(self) -> dict:
        snap = self.store.snapshot()
        blocked = self.blocked_channels()
        channels = []
        for channel in self.config.channels:
            item = dict(snap["channels"].get(channel.name, {}))
            catalog = self.store.catalog_view(channel.name)
            # resolve_secret passes a literal "***" through, and such channels
            # then send Bearer *** upstream and fail 401 while looking "usable"
            # (typical when a masked export is pasted back as router.json).
            # Report them honestly: not configured, plus a placeholder flag the
            # console's config-health banner explains.
            placeholder = channel.api_key == "***"
            has_key = bool(resolve_secret(channel.api_key)) and not placeholder
            item.update({
                "name": channel.name,
                "protocol": channel.protocol,
                "priority": channel.priority,
                "enabled": channel.enabled,
                "cooldown_minutes": channel.cooldown_minutes,
                "blocked": channel.name in blocked,
                "blocked_reason": blocked.get(channel.name, ""),
                "tier": channel_tier(channel, self.subscriptions),
                "tiers": dict(channel.tiers),
                "scores": self.store.scores_for(channel.name),
                "catalog": {
                    "fetched_at": catalog["fetched_at"],
                    "count": catalog["count"],
                    "changes": catalog["changes"],
                },
                "configured": has_key,
                "placeholder_key": placeholder,
            })
            rank = self.store.billing_ranks.get(channel.name)
            item["billing"] = None if not rank else {
                "group": rank[0],
                "expiry": (datetime.fromtimestamp(rank[1]).date().isoformat()
                           if rank[0] == 0 else ""),
                "burn": -rank[2] if rank[0] == 0 else 0.0,
            }
            channels.append(item)
        return {
            "channels": channels,
            "routes": {key: list(route.channels) for key, route in self.config.routes.items()},
            "model_states": self.model_states(),
            "sticky_sessions": snap["sticky_sessions"],
            "listen": {"host": self.config.listen.host, "port": self.config.listen.port,
                       "admin_key_set": bool(self.config.listen.admin_key)},
            "config_path": str(self.config_path) if self.config_path else None,
            "subscriptions": self.subscription_summary(),
            "subscription_settings": self.subscription_settings,
            "blocked_channels": blocked,
            "catalog_refresh_hours": self.config.defaults.catalog_refresh_hours,
            "load_balancing": {
                "strategy": self.config.defaults.lb_strategy,
                "probe_interval_seconds": self.config.defaults.probe_interval_seconds,
                "probe_mode": self.config.defaults.probe_mode,
                "probe_max_cooldown_seconds": self.config.defaults.probe_max_cooldown_seconds,
            },
            "maintenance": {
                "hours": self.config.defaults.maintenance_hours,
                "llm_model": self.config.defaults.maintenance_model,
                "runs": len(self.maintenance_reports),
                "last": self._maintenance_summary(),
            },
        }

    def model_states(self) -> list[dict]:
        """Aggregate per-model availability across channels (runtime-only).

        free_ok      — some enabled channel maps this model at tier "free"
        exhausted    — a tier-free channel's upstream still advertises the
                       model but no enabled free mapping remains (freealign
                       pruned it, or it never carried free quota)
        paid_only    — only paid-tier mappings on enabled channels
        available    — enabled channels provide it but no tier annotation
                       (neither free nor paid, e.g. no linked subscription)
        no_channel   — nothing enabled provides it (disabled-only or unknown)

        Tiers come from per-model overrides > channel.tier > linked
        subscriptions, plus one vendor naming convention: on OpenRouter only
        the ":free" model-id suffix is free (see model_tier). Cooldown is a
        channel-health signal (shown per channel) and deliberately not part
        of the status.
        """
        now = time.time()
        subscriptions = self.subscriptions
        by_model: dict[str, list] = {}
        for channel in self.config.channels:
            for model in explicit_models(channel):
                by_model.setdefault(model, []).append(channel)
        # What tier-free upstreams still advertise in their catalogs.
        free_upstream: dict[str, list[str]] = {}
        for channel in self.config.channels:
            if channel_tier(channel, subscriptions) != "free":
                continue
            for model in (self.store.catalogs.get(channel.name) or {}).get("models") or []:
                # OpenRouter's catalog lists its paid models too; only the
                # ":free" suffix ones advertise free access (see model_tier).
                if (channel.provider == "openrouter"
                        and not is_free_model(model)):
                    continue
                free_upstream.setdefault(str(model), []).append(channel.name)
        labels = {"free_ok": "免费可用", "free_exhausted": "免费已耗尽",
                  "paid_only": "仅付费", "available": "可用（档位未知）",
                  "no_channel": "无渠道"}
        rank = {"free_ok": 0, "free_exhausted": 1, "paid_only": 2,
                "available": 3, "no_channel": 4}
        rows = []
        for model in sorted(set(by_model) | set(free_upstream)):
            free_ok = paid_ok = plain_ok = False
            entries = []
            for channel in by_model.get(model, []):
                tier = model_tier(channel, subscriptions, model)
                if channel.enabled and tier == "free":
                    free_ok = True
                elif channel.enabled and tier == "paid":
                    paid_ok = True
                elif channel.enabled and not tier:
                    plain_ok = True
                state = self.store.states.get(channel.name)
                wire = channel.models.get(model) or model
                entries.append({
                    "name": channel.name,
                    "tier": tier or "",
                    "enabled": channel.enabled,
                    "cooling": bool(state and state.cooldown_until > now),
                    "score": int(round(self.store.score(channel.name, wire))),
                })
            if free_ok:
                status = "free_ok"
            elif model in free_upstream:
                status = "free_exhausted"
            elif paid_ok:
                status = "paid_only"
            elif plain_ok:
                status = "available"
            else:
                status = "no_channel"
            # Exhausted models have no mapping left; still show which free
            # upstreams advertise them.
            seen = {entry["name"] for entry in entries}
            for name in free_upstream.get(model, []):
                if name in seen:
                    continue
                channel = self.config.channel(name)
                if channel is None:
                    continue
                state = self.store.states.get(name)
                entries.append({
                    "name": name,
                    "tier": "free",
                    "enabled": channel.enabled,
                    "cooling": bool(state and state.cooldown_until > now),
                    "score": int(round(self.store.score(
                        name, channel.models.get(AUTO_MODEL) or model))),
                })
            rows.append({"model": model, "status": status,
                         "label": labels[status], "channels": entries})
        rows.sort(key=lambda row: (rank[row["status"]], row["model"]))
        return rows

    def _maintenance_summary(self) -> dict | None:
        if not self.maintenance_reports:
            return None
        report = self._without_ignored([self.maintenance_reports[-1]])[0]
        return {
            "finished_at": report.get("finished_at"),
            "trigger": report.get("trigger"),
            "applied": len(report.get("applied") or []),
            "suggestions": len(report.get("suggestions") or []),
            "alerts": len(report.get("alerts") or []),
            "llm_error": report.get("llm_error") or "",
        }

    # ------------------------------------------------------------------ auth
    def _provided_key(self, h: BaseHTTPRequestHandler) -> str:
        provided = h.headers.get("x-api-key")
        if not provided:
            auth = h.headers.get("Authorization") or ""
            provided = auth[7:] if auth.lower().startswith("bearer ") else ""
        return provided or ""

    def _authorized(self, h: BaseHTTPRequestHandler) -> bool:
        """Gateway auth: the master key, or any active member key.

        Member keys exist so several people can share one gateway without
        passing a single secret around; they attribute usage to a person,
        they do not gate routes or models. With no master key configured the
        gateway stays LAN-open exactly as before (usage of keyless callers
        lands in the anonymous bucket).
        """
        provided = self._provided_key(h)
        member_record = self.keys.lookup(provided) if self.keys else None
        member = member_record["name"] if member_record else None
        # Resolved once here; handlers thread it into usage records as
        # `getattr(h, "user_key", "")` (tests pass h=None, default covers it).
        # The full record rides along so _member_limited sees the caps
        # without a second key scan.
        h.user_key = member or ""
        h.member_record = member_record
        if member:
            return True
        expected = self.config.listen.api_key
        if not expected:
            return True
        # A paused master key stops gateway calls that use it, while the
        # member keys above keep their own independent lifecycle.
        if not self.config.listen.api_key_enabled:
            return False
        # compare_digest raises TypeError on non-ASCII str arguments (a Bearer
        # token is arbitrary client bytes): compare the encoded forms instead.
        return hmac.compare_digest(
            (provided or "").encode("utf-8"), (expected or "").encode("utf-8"))

    def _authorized_admin(self, h: BaseHTTPRequestHandler) -> bool:
        """Management endpoints use listen.admin_key and nothing else.

        A config with no credentials at all keeps the console open (same rule
        as the gateway; the browser then forces the operator to set a
        management password before anything can be used); a gateway api_key
        without an admin_key locks the management API until the operator sets
        one in router.json.
        """
        expected = self.config.listen.admin_key
        if not expected:
            return not self.config.listen.api_key
        provided = h.headers.get("X-Admin-Key")
        if not provided:
            auth = h.headers.get("Authorization") or ""
            provided = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not provided:
            return False
        return hmac.compare_digest(
            expected.encode("utf-8"), provided.strip().encode("utf-8"))

    # -------------------------------------------------------- member quotas
    def _member_limited(self, member: str, record: dict | None) -> tuple[int, str] | None:
        """(status, message) when an inference call would break a member cap.

        Caps are per key: rpm uses an in-memory sliding window, request/token
        quotas read the usage recorder (today / this month). The RPM slot is
        reserved in the same critical section as the cap check, so two
        concurrent calls cannot both pass a full window; a call later rejected
        by a usage quota releases its reservation. No limits -> None.
        """
        limits = (record or {}).get("limits") or {}
        if not member or not limits:
            return None
        now = time.time()
        rpm = int(limits.get("rpm") or 0)
        reserved = None
        if rpm:
            with self._member_rpm_lock:
                stamps = self.member_rpm.setdefault(member, [])
                horizon = now - MEMBER_RPM_WINDOW
                while stamps and stamps[0] <= horizon:
                    stamps.pop(0)
                if len(stamps) >= rpm:
                    return (429, f"成员 {member} 已达到每分钟 {rpm} 次请求上限")
                stamps.append(now)
                reserved = stamps
        if self.usage:
            today = date.today()
            day_requests = int(limits.get("daily_requests") or 0)
            day_tokens = int(limits.get("daily_tokens") or 0)
            month_requests = int(limits.get("monthly_requests") or 0)
            month_tokens = int(limits.get("monthly_tokens") or 0)
            rejected = None
            if day_requests or day_tokens:
                used = self.usage.member_totals(member, today)
                if day_requests and used["requests"] >= day_requests:
                    rejected = (429, f"成员 {member} 今日请求数配额已用完"
                                     f"（已用 {used['requests']} / 上限 {day_requests}）")
                elif day_tokens and used["tokens"] >= day_tokens:
                    rejected = (429, f"成员 {member} 今日 token 配额已用完"
                                     f"（已用 {used['tokens']} / 上限 {day_tokens}）")
            if rejected is None and (month_requests or month_tokens):
                used = self.usage.member_totals(member, today.replace(day=1))
                if month_requests and used["requests"] >= month_requests:
                    rejected = (429, f"成员 {member} 本月请求数配额已用完"
                                     f"（已用 {used['requests']} / 上限 {month_requests}）")
                elif month_tokens and used["tokens"] >= month_tokens:
                    rejected = (429, f"成员 {member} 本月 token 配额已用完"
                                     f"（已用 {used['tokens']} / 上限 {month_tokens}）")
            if rejected is not None:
                # The call never reaches inference: give the RPM slot back so
                # a quota-blocked caller cannot lock itself out of the window.
                if reserved is not None:
                    with self._member_rpm_lock:
                        try:
                            reserved.remove(now)
                        except ValueError:
                            pass
                return rejected
        return None

    def update_key_limits(self, key: str, limits) -> dict:
        """Replace one member key's quota block; ValueError when it is gone."""
        if not self.keys:
            raise ValueError("成员 Key 功能未启用")
        record = self.keys.set_limits(key, limits)
        if record is None:
            raise ValueError("成员 Key 不存在或已被删除")
        return record

    def rotate_member_key(self, key: str) -> dict:
        """Re-issue one member key's secret; ValueError when it is gone."""
        if not self.keys:
            raise ValueError("成员 Key 功能未启用")
        record = self.keys.rotate(key)
        if record is None:
            raise ValueError("成员 Key 不存在或已被删除")
        return record

    def member_usage(self, days: int = 30) -> dict:
        """Member usage stats merged with each key's current limits/state."""
        if not self.usage:
            return {"range": days, "today": date.today().isoformat(), "users": [],
                    "days": [], "note": "usage recorder not enabled"}
        payload = self.usage.user_stats(days)
        by_name = {row["name"]: row for row in (self.keys.list() if self.keys else [])}
        for row in payload.get("users") or []:
            record = by_name.get(row.get("name")) or {}
            row["limits"] = record.get("limits") or {}
            row["enabled"] = bool(record.get("enabled", True))
        return payload

    # --------------------------------------------------------------- dispatch
    def dispatch(self, h: BaseHTTPRequestHandler, method: str, path: str, raw: bytes) -> None:
        if ui.handle(self, h, method, path, raw):
            return
        if not self._authorized(h):
            _json(h, 401, {"error": {"message": "invalid gateway key"}})
            return
        # Per-member caps apply to inference calls only (count_tokens is local
        # and free); keyless callers and keys without limits pass untouched.
        member = getattr(h, "user_key", "")
        if (member and method == "POST" and path.startswith("/v1/")
                and path != "/v1/messages/count_tokens"):
            limited = self._member_limited(member, getattr(h, "member_record", None))
            if limited:
                _json(h, limited[0], {"error": {"message": limited[1]}})
                return
        if method == "GET" and path == "/health":
            now = time.time()
            snap = self.store.snapshot(now)["channels"]
            blocked = self.blocked_channels()
            # Disabled channels cannot serve requests, so they must not
            # count as available; the per-channel detail below still
            # reports their (stale) stats for diagnostics.
            enabled = {c.name for c in self.config.channels if c.enabled}
            available = [n for n, s in snap.items()
                         if not s["cooling"] and n not in blocked and n in enabled]
            _json(h, 200 if available else 503, {
                "status": "ok" if available else "all_channels_cooling",
                "available": available,
                "channels": snap,
                "blocked_channels": blocked,
                "subscriptions": self.subscription_summary(),
            })
            return
        if method == "GET" and path == "/stats":
            snap = self.store.snapshot()
            _json(h, 200, {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "note": "total_tokens includes streamed usage when the upstream reports it",
                **snap,
            })
            return
        if method == "GET" and path == "/v1/models":
            _json(h, 200, self.model_list())
            return
        if method == "POST" and path == "/v1/responses":
            self._completion(h, OPENAI, raw, responses=True)
            return
        if method == "POST" and path == "/v1/chat/completions":
            self._completion(h, OPENAI, raw)
            return
        if method == "POST" and path == "/v1/completions":
            self._completion(h, OPENAI, raw, legacy=True)
            return
        if method == "POST" and path == "/v1/embeddings":
            self._embeddings(h, raw)
            return
        if method == "POST" and path == "/v1/rerank":
            self._rerank(h, raw)
            return
        if method == "POST" and path == "/v1/audio/speech":
            self._audio_speech(h, raw)
            return
        if method == "POST" and path == "/v1/audio/transcriptions":
            self._audio_transcriptions(h, raw, endpoint="transcriptions")
            return
        if method == "POST" and path == "/v1/audio/translations":
            self._audio_transcriptions(h, raw, endpoint="translations")
            return
        if method == "POST" and path == "/v1/messages/count_tokens":
            self._count_tokens(h, raw)
            return
        if method == "POST" and path == "/v1/messages":
            self._completion(h, ANTHROPIC, raw)
            return
        _json(h, 404, {"error": {"message": "not found", "path": path}})

    # ---------------------------------------------------------- count_tokens
    def _count_tokens(self, h: BaseHTTPRequestHandler, raw: bytes) -> None:
        """Local context-budget estimate for Claude Code; costs no upstream quota."""
        try:
            body = json.loads(raw)
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return
        if not isinstance(body, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return
        tokens = _estimate_tokens(body)
        self.log_request(
            protocol=ANTHROPIC, model=str(body.get("model") or ""),
            mode="local", reason="local-token-estimate", status="local",
            http_status=200, tokens=tokens, ms=0, stream=False,
        )
        _json(h, 200, {"input_tokens": tokens}, {
            "X-Token-Estimate": "local-heuristic",
            "X-Routed-Via": "local",
        })

    # ----------------------------------------------------------- embeddings
    def _embeddings(self, h: BaseHTTPRequestHandler, raw: bytes) -> None:
        """Handle POST /v1/embeddings — OpenAI-compatible embeddings endpoint."""
        try:
            body = json.loads(raw)
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return
        if not isinstance(body, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return

        requested_model = str(body.get("model") or "").strip() or AUTO_MODEL
        _log_inbound_shape(body, len(raw))

        blocked = self.blocked_channels()
        decision = resolve_model(
            self.config, OPENAI, requested_model, h.headers.get("X-Route"), blocked,
        )
        audit = _audit_headers(decision)
        log_base = {
            "api": "embeddings",
            "protocol": OPENAI,
            "model": decision.requested,
            "mode": decision.mode,
            "reason": decision.reason,
            "route": decision.hint,
            "stream": False,
            "sticky": False,
            "rid": _rid(),
        }

        if decision.mode == "rejected":
            self.log_request(**log_base, status="rejected", http_status=400, attempt=0)
            _json(h, 400, {
                "error": {
                    "message": "unknown model; internal scheduling is disabled",
                    "model": requested_model,
                    "hint": "use a branded model name or add the model to a channel's models mapping",
                },
                "models": [item["id"] for item in self.model_list()["data"]],
            }, audit)
            return

        candidates, _ = self.store.pick(
            OPENAI, decision.hint, decision.exposed, None,
            decision=decision, blocked=blocked,
            agent_candidates=None,
        )
        candidates = [(channel, wire) for channel, wire in candidates
                      if channel.name not in blocked]

        # Embeddings are text-only; no vision filtering needed.
        candidates, catalog_skipped = self._apply_catalog_filter(candidates, decision)
        candidates, dead_skipped = self._apply_dead_filter(candidates)
        candidates, quota_skipped = self._apply_free_quota_filter(candidates, decision)
        candidates, budget_skipped = self._apply_paid_budget_filter(candidates)
        catalog_skipped = list(catalog_skipped) + dead_skipped

        if not candidates:
            self.log_request(**log_base, status="no_candidate", http_status=503, attempt=0)
            route = self.config.route(OPENAI, decision.hint)
            status_map: dict[str, str] = {}
            for name in _status_channels(decision, route):
                channel = self.config.channel(name)
                if channel is None:
                    status_map[name] = "missing"
                elif not channel.enabled:
                    status_map[name] = "disabled"
                elif name in blocked:
                    status_map[name] = "blocked"
                else:
                    with self.store.lock:
                        ok, why = self.store.states[name].available(time.time(), channel.rate)
                    status_map[name] = "ok" if ok else why
            message = "no healthy channel for this route/model"
            if status_map and all(
                    s in ("disabled", "missing") for s in status_map.values()):
                message = ("no enabled channel for this route — enable a channel in "
                           "the console or add one that speaks this protocol")
            if quota_skipped:
                message = ("free quota exhausted for this model — routing stopped "
                           "to avoid per-call billing")
            error = {
                "message": message,
                "route": f"{OPENAI}:{decision.hint}",
                "model": requested_model,
                "blocked_channels": blocked,
                "channel_status": status_map,
            }
            if catalog_skipped:
                error["catalog_skipped"] = catalog_skipped
            if quota_skipped:
                error["free_quota_skipped"] = quota_skipped
            _json(h, 503, {
                "error": error,
                "channels": self.store.snapshot()["channels"],
            }, {**audit, "X-Routed-Via": "none", "X-Failover-Count": "0"})
            return

        attempt = 0
        failures: list[dict] = []
        queue = list(candidates[: self.config.defaults.max_failover + 1])
        swapped: set[str] = set()
        while queue:
            channel, wire = queue.pop(0)
            for try_no in range(self.config.defaults.network_retries + 1):
                now = time.time()
                with self.store.lock:
                    ok, why = self.store.states[channel.name].available(now, channel.rate)
                    if not ok:
                        failures.append({"channel": channel.name, "reason": why})
                        break
                    self.store.states[channel.name].note_attempt(now)

                started = time.perf_counter()
                error = self._forward_embeddings(h, channel, wire, body, attempt, audit)
                entry = {
                    **log_base, "channel": channel.name, "wire_model": wire,
                    "attempt": attempt, "ms": round((time.perf_counter() - started) * 1000, 1),
                    "billing": self._billing_label(channel),
                }
                if error is None:
                    self.log_request(**entry, status="ok", http_status=200,
                                     tokens=0)  # embeddings usage not tracked yet
                    return

                reason = error[1]
                entry["reason"] = reason
                self.log_request(
                    **entry,
                    status="network_error" if reason.startswith("transient_") else "upstream_error",
                    http_status=error[0], cooldown_seconds=round(error[2], 1),
                )
                attempt += 1
                failures.append({
                    "channel": channel.name,
                    "status": error[0],
                    "reason": reason,
                    "cooldown_seconds": round(error[2], 1),
                })
                if reason != "transient_network":
                    spare = self._spare_after_failure(channel, wire, decision, swapped)
                    if spare:
                        queue.insert(0, (channel, spare))
                    break

        _json(h, 503, {
            "error": {"message": "all channels failed for this request", "failures": failures},
            "channels": self.store.snapshot()["channels"],
        }, {**audit, "X-Routed-Via": "none", "X-Failover-Count": str(attempt)})

    def _forward_embeddings(self, h, channel, wire, body, attempt, audit):
        """Relay one embeddings attempt. Return None on success, else (status, reason, seconds)."""
        payload = {
            "model": wire,
            "input": body.get("input"),
        }
        # Optional parameters passed through to upstream
        if "encoding_format" in body:
            payload["encoding_format"] = body["encoding_format"]
        if "dimensions" in body:
            payload["dimensions"] = body["dimensions"]
        if "user" in body:
            payload["user"] = body["user"]

        upstream_path = upstream_path_for(channel.base_url, channel.upstream_path, OPENAI, endpoint="embeddings")
        headers = {"Content-Type": "application/json"}
        if channel.api_key:
            headers["Authorization"] = f"Bearer {channel.api_key}"
        apply_upstream_headers(headers, channel.base_url,
            body=payload, api_key=channel.api_key)

        req = Request(
            channel.base_url + upstream_path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        now = time.time()
        try:
            resp = open_with_proxy(req, channel.timeout, channel.proxy)
        except HTTPError as exc:
            raw_resp = exc.read()
            quota, seconds, reason = parse_cooldown(
                exc.code, exc.headers, raw_resp,
                channel.cooldown_minutes, self.config.defaults.max_cooldown_hours, now,
                not_found_hours=self.config.defaults.not_found_cooldown_hours,
                daily_hours=self.config.defaults.daily_429_cooldown_hours,
            )
            excerpt = raw_resp.decode("utf-8", "replace")[:300]
            low = excerpt.lower()
            account_scoped = _account_scoped_failure(quota, low, channel)
            if account_scoped and any(m in low for m in ACCOUNT_BALANCE_MARKERS):
                # The vendor is pointing at the account's money, not this
                # model's grant: nothing resets on its own, so cooling for
                # hours only replays the rejection. Retire the channel
                # (persisted) instead; re-enabling is the operator's call.
                self._disable_account_channel(channel, excerpt)
                self.record_usage(channel.name, False, model=wire, h=h)
                return (exc.code, "account_disabled", 0.0)
            per_model = quota and not account_scoped
            if quota:
                plan_seconds, plan_reason = self._plan_reset_cooldown(channel, now)
                if plan_seconds > seconds:
                    seconds, reason = plan_seconds, plan_reason
                    per_model = False
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = f"HTTP {exc.code}: {excerpt}"
                state.mark_cooldown(
                    seconds, reason, now, per_model=per_model, wire=wire,
                    half_open=not quota,
                )
            self.record_usage(channel.name, False, model=wire, h=h)
            if not quota:
                self.store.record_score(channel.name, wire, -5)
            return (exc.code, reason, seconds)
        except (URLError, TimeoutError, OSError) as exc:
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = str(exc)[:300]
                state.mark_cooldown(30.0, "transient_network", now, half_open=True)
            self.record_usage(channel.name, False, model=wire, h=h)
            self.store.record_score(channel.name, wire, -5)
            return (502, "transient_network", 30.0)
        except Exception as exc:
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = str(exc)[:300]
                state.mark_cooldown(30.0, "transient_internal", now, half_open=True)
            self.record_usage(channel.name, False, model=wire, h=h)
            self.store.record_score(channel.name, wire, -5)
            return (502, "transient_internal", 30.0)
        else:
            try:
                with resp:
                    return self._relay_embeddings(h, resp, channel, wire, attempt, audit)
            except (BrokenPipeError, ConnectionResetError):
                with self.store.lock:
                    self.store.states[channel.name].mark_success(0, time.time(), wire)
                self.store.record_score(channel.name, wire, 1)
                return None
            except Exception as exc:
                with self.store.lock:
                    self.store.states[channel.name].last_error = str(exc)[:300]
                return None

    def _relay_embeddings(self, h, resp, channel, wire, attempt, audit):
        """Relay successful embeddings response to client."""
        extra = {
            **dict(audit or {}),
            "X-Routed-Via": _header_safe(f"{channel.name}/{wire}"),
            "X-Failover-Count": str(attempt),
        }
        data = resp.read()
        h.send_response(200)
        h.send_header("Content-Type", resp.headers.get("Content-Type") or "application/json")
        h.send_header("Content-Length", str(len(data)))
        for key, value in extra.items():
            h.send_header(key, value)
        h.end_headers()
        h.wfile.write(data)
        with self.store.lock:
            self.store.states[channel.name].mark_success(0, time.time(), wire)
        self.store.record_score(channel.name, wire, 1)
        self.record_usage(channel.name, True, 0, model=wire, h=h)
        self.apply_quota(channel.name, resp.headers)
        return None

    # ---------------------------------------------------------------- rerank
    def _rerank(self, h: BaseHTTPRequestHandler, raw: bytes) -> None:
        """Handle POST /v1/rerank — OpenAI-compatible rerank endpoint.

        Request shape (OpenAI rerank API):
        {
            "model": "rerank-model-name",
            "query": "text to rank against",
            "documents": ["doc1", "doc2", ...],
            "top_n": 3,
            "return_documents": true
        }

        Only routes to channels that expose a rerank-capable model.
        """
        try:
            body = json.loads(raw)
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return
        if not isinstance(body, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return

        requested_model = str(body.get("model") or "").strip() or AUTO_MODEL
        _log_inbound_shape(body, len(raw))

        blocked = self.blocked_channels()
        decision = resolve_model(
            self.config, OPENAI, requested_model, h.headers.get("X-Route"), blocked,
        )
        audit = _audit_headers(decision)
        log_base = {
            "api": "rerank",
            "protocol": OPENAI,
            "model": decision.requested,
            "mode": decision.mode,
            "reason": decision.reason,
            "route": decision.hint,
            "stream": False,
            "sticky": False,
            "rid": _rid(),
        }

        if decision.mode == "rejected":
            self.log_request(**log_base, status="rejected", http_status=400, attempt=0)
            _json(h, 400, {
                "error": {
                    "message": "unknown model; internal scheduling is disabled",
                    "model": requested_model,
                    "hint": "use a branded model name or add the model to a channel's models mapping",
                },
                "models": [item["id"] for item in self.model_list()["data"]],
            }, audit)
            return

        # Filter candidates: only channels that have a rerank model
        candidates, _ = self.store.pick(
            OPENAI, decision.hint, decision.exposed, None,
            decision=decision, blocked=blocked,
            agent_candidates=None,
        )
        # Rerank is text-only; no vision filtering needed.
        candidates = [(channel, wire) for channel, wire in candidates
                      if channel.name not in blocked]
        # Apply catalog filter to find channels that actually have rerank models
        candidates, catalog_skipped = self._apply_catalog_filter(candidates, decision)
        candidates, dead_skipped = self._apply_dead_filter(candidates)
        candidates, quota_skipped = self._apply_free_quota_filter(candidates, decision)
        candidates, budget_skipped = self._apply_paid_budget_filter(candidates)
        catalog_skipped = list(catalog_skipped) + dead_skipped

        if not candidates:
            self.log_request(**log_base, status="no_candidate", http_status=503, attempt=0)
            route = self.config.route(OPENAI, decision.hint)
            status_map: dict[str, str] = {}
            for name in _status_channels(decision, route):
                channel = self.config.channel(name)
                if channel is None:
                    status_map[name] = "missing"
                elif not channel.enabled:
                    status_map[name] = "disabled"
                elif name in blocked:
                    status_map[name] = "blocked"
                else:
                    with self.store.lock:
                        ok, why = self.store.states[name].available(time.time(), channel.rate)
                    status_map[name] = "ok" if ok else why
            message = "no healthy channel with rerank capability for this route/model"
            if status_map and all(
                    s in ("disabled", "missing") for s in status_map.values()):
                message = ("no enabled channel for this route — enable a channel in "
                           "the console or add one that speaks this protocol")
            if quota_skipped:
                message = ("free quota exhausted for this model — routing stopped "
                           "to avoid per-call billing")
            error = {
                "message": message,
                "route": f"{OPENAI}:{decision.hint}",
                "model": requested_model,
                "blocked_channels": blocked,
                "channel_status": status_map,
            }
            if catalog_skipped:
                error["catalog_skipped"] = catalog_skipped
            if quota_skipped:
                error["free_quota_skipped"] = quota_skipped
            _json(h, 503, {
                "error": error,
                "channels": self.store.snapshot()["channels"],
            }, {**audit, "X-Routed-Via": "none", "X-Failover-Count": "0"})
            return

        attempt = 0
        failures: list[dict] = []
        queue = list(candidates[: self.config.defaults.max_failover + 1])
        swapped: set[str] = set()
        while queue:
            channel, wire = queue.pop(0)
            for try_no in range(self.config.defaults.network_retries + 1):
                now = time.time()
                with self.store.lock:
                    ok, why = self.store.states[channel.name].available(now, channel.rate)
                    if not ok:
                        failures.append({"channel": channel.name, "reason": why})
                        break
                    self.store.states[channel.name].note_attempt(now)

                started = time.perf_counter()
                error = self._forward_rerank(h, channel, wire, body, attempt, audit)
                entry = {
                    **log_base, "channel": channel.name, "wire_model": wire,
                    "attempt": attempt, "ms": round((time.perf_counter() - started) * 1000, 1),
                    "billing": self._billing_label(channel),
                }
                if error is None:
                    self.log_request(**entry, status="ok", http_status=200,
                                     tokens=0)  # rerank usage not tracked in tokens
                    return

                reason = error[1]
                entry["reason"] = reason
                self.log_request(
                    **entry,
                    status="network_error" if reason.startswith("transient_") else "upstream_error",
                    http_status=error[0], cooldown_seconds=round(error[2], 1),
                )
                attempt += 1
                failures.append({
                    "channel": channel.name,
                    "status": error[0],
                    "reason": reason,
                    "cooldown_seconds": round(error[2], 1),
                })
                if reason != "transient_network":
                    spare = self._spare_after_failure(channel, wire, decision, swapped)
                    if spare:
                        queue.insert(0, (channel, spare))
                    break

        _json(h, 503, {
            "error": {"message": "all channels failed for this request", "failures": failures},
            "channels": self.store.snapshot()["channels"],
        }, {**audit, "X-Routed-Via": "none", "X-Failover-Count": str(attempt)})

    def _forward_rerank(self, h, channel, wire, body, attempt, audit):
        """Relay one rerank attempt. Return None on success, else (status, reason, seconds)."""
        payload = {
            "model": wire,
            "query": body.get("query"),
            "documents": body.get("documents"),
        }
        # Optional parameters passed through to upstream
        if "top_n" in body:
            payload["top_n"] = body["top_n"]
        if "return_documents" in body:
            payload["return_documents"] = body["return_documents"]
        if "user" in body:
            payload["user"] = body["user"]

        upstream_path = upstream_path_for(channel.base_url, channel.upstream_path, OPENAI, endpoint="rerank")
        headers = {"Content-Type": "application/json"}
        if channel.api_key:
            headers["Authorization"] = f"Bearer {channel.api_key}"
        apply_upstream_headers(headers, channel.base_url,
            body=payload, api_key=channel.api_key)

        req = Request(
            channel.base_url + upstream_path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        now = time.time()
        try:
            resp = open_with_proxy(req, channel.timeout, channel.proxy)
        except HTTPError as exc:
            raw_resp = exc.read()
            quota, seconds, reason = parse_cooldown(
                exc.code, exc.headers, raw_resp,
                channel.cooldown_minutes, self.config.defaults.max_cooldown_hours, now,
                not_found_hours=self.config.defaults.not_found_cooldown_hours,
                daily_hours=self.config.defaults.daily_429_cooldown_hours,
            )
            excerpt = raw_resp.decode("utf-8", "replace")[:300]
            low = excerpt.lower()
            account_scoped = _account_scoped_failure(quota, low, channel)
            if account_scoped and any(m in low for m in ACCOUNT_BALANCE_MARKERS):
                # The vendor is pointing at the account's money, not this
                # model's grant: nothing resets on its own, so cooling for
                # hours only replays the rejection. Retire the channel
                # (persisted) instead; re-enabling is the operator's call.
                self._disable_account_channel(channel, excerpt)
                self.record_usage(channel.name, False, model=wire, h=h)
                return (exc.code, "account_disabled", 0.0)
            per_model = quota and not account_scoped
            if quota:
                plan_seconds, plan_reason = self._plan_reset_cooldown(channel, now)
                if plan_seconds > seconds:
                    seconds, reason = plan_seconds, plan_reason
                    per_model = False
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = f"HTTP {exc.code}: {excerpt}"
                state.mark_cooldown(
                    seconds, reason, now, per_model=per_model, wire=wire,
                    half_open=not quota,
                )
            self.record_usage(channel.name, False, model=wire, h=h)
            if not quota:
                self.store.record_score(channel.name, wire, -5)
            return (exc.code, reason, seconds)
        except (URLError, TimeoutError, OSError) as exc:
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = str(exc)[:300]
                state.mark_cooldown(30.0, "transient_network", now, half_open=True)
            self.record_usage(channel.name, False, model=wire, h=h)
            self.store.record_score(channel.name, wire, -5)
            return (502, "transient_network", 30.0)
        except Exception as exc:
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = str(exc)[:300]
                state.mark_cooldown(30.0, "transient_internal", now, half_open=True)
            self.record_usage(channel.name, False, model=wire, h=h)
            self.store.record_score(channel.name, wire, -5)
            return (502, "transient_internal", 30.0)
        else:
            try:
                with resp:
                    return self._relay_rerank(h, resp, channel, wire, attempt, audit)
            except HTTPError as exc:
                # Should not happen for 2xx responses, but handle just in case
                raw_resp = exc.read()
                quota, seconds, reason = parse_cooldown(
                    exc.code, exc.headers, raw_resp,
                    channel.cooldown_minutes, self.config.defaults.max_cooldown_hours, now,
                    not_found_hours=self.config.defaults.not_found_cooldown_hours,
                    daily_hours=self.config.defaults.daily_429_cooldown_hours,
                )
                excerpt = raw_resp.decode("utf-8", "replace")[:300]
                low = excerpt.lower()
                account_scoped = _account_scoped_failure(quota, low, channel)
                if account_scoped and any(m in low for m in ACCOUNT_BALANCE_MARKERS):
                    # Same verdict as the chat paths: an out-of-credit account
                    # will not heal, so retire the channel instead of cooling.
                    self._disable_account_channel(channel, excerpt)
                    self.record_usage(channel.name, False, model=wire, h=h)
                    return (exc.code, "account_disabled", 0.0)
                per_model = quota and not account_scoped
                if quota:
                    plan_seconds, plan_reason = self._plan_reset_cooldown(channel, now)
                    if plan_seconds > seconds:
                        seconds, reason = plan_seconds, plan_reason
                        per_model = False
                with self.store.lock:
                    state = self.store.states[channel.name]
                    state.last_error = f"HTTP {exc.code}: {excerpt}"
                    state.mark_cooldown(
                        seconds, reason, now, per_model=per_model, wire=wire,
                        half_open=not quota,
                    )
                self.record_usage(channel.name, False, model=wire, h=h)
                if not quota:
                    self.store.record_score(channel.name, wire, -5)
                return (exc.code, reason, seconds)
            except (BrokenPipeError, ConnectionResetError):
                with self.store.lock:
                    self.store.states[channel.name].mark_success(0, time.time(), wire)
                self.store.record_score(channel.name, wire, 1)
                return None
            except Exception as exc:
                with self.store.lock:
                    self.store.states[channel.name].last_error = str(exc)[:300]
                return None

    def _relay_rerank(self, h, resp, channel, wire, attempt, audit):
        """Relay successful rerank response to client."""
        extra = {
            **dict(audit or {}),
            "X-Routed-Via": _header_safe(f"{channel.name}/{wire}"),
            "X-Failover-Count": str(attempt),
        }
        data = resp.read()
        h.send_response(200)
        h.send_header("Content-Type", resp.headers.get("Content-Type") or "application/json")
        h.send_header("Content-Length", str(len(data)))
        for key, value in extra.items():
            h.send_header(key, value)
        h.end_headers()
        h.wfile.write(data)
        with self.store.lock:
            self.store.states[channel.name].mark_success(0, time.time(), wire)
        self.store.record_score(channel.name, wire, 1)
        self.record_usage(channel.name, True, 0, model=wire, h=h)

    # --------------------------------------------------------------- audio endpoints
    def _audio_speech(self, h: BaseHTTPRequestHandler, raw: bytes) -> None:
        """Handle POST /v1/audio/speech — OpenAI-compatible TTS endpoint.

        Request shape (OpenAI audio speech API):
        {
            "model": "tts-model-name",
            "input": "text to synthesize",
            "voice": "alloy",
            "response_format": "mp3",
            "speed": 1.0
        }

        Returns binary audio data (not JSON). Routes to channels with TTS capability.
        """
        try:
            body = json.loads(raw)
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return
        if not isinstance(body, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return

        requested_model = str(body.get("model") or "").strip() or AUTO_MODEL
        _log_inbound_shape(body, len(raw))

        blocked = self.blocked_channels()
        decision = resolve_model(
            self.config, OPENAI, requested_model, h.headers.get("X-Route"), blocked,
        )
        audit = _audit_headers(decision)
        log_base = {
            "api": "audio_speech",
            "protocol": OPENAI,
            "model": decision.requested,
            "mode": decision.mode,
            "reason": decision.reason,
            "route": decision.hint,
            "stream": False,
            "sticky": False,
            "rid": _rid(),
        }

        if decision.mode == "rejected":
            self.log_request(**log_base, status="rejected", http_status=400, attempt=0)
            _json(h, 400, {
                "error": {
                    "message": "unknown model; internal scheduling is disabled",
                    "model": requested_model,
                    "hint": "use a branded model name or add the model to a channel's models mapping",
                },
                "models": [item["id"] for item in self.model_list()["data"]],
            }, audit)
            return

        # Filter candidates for TTS capability
        candidates, _ = self.store.pick(
            OPENAI, decision.hint, decision.exposed, None,
            decision=decision, blocked=blocked,
            agent_candidates=None,
        )
        candidates = [(channel, wire) for channel, wire in candidates
                      if channel.name not in blocked]
        # Apply catalog filter
        candidates, catalog_skipped = self._apply_catalog_filter(candidates, decision)
        candidates, dead_skipped = self._apply_dead_filter(candidates)
        candidates, quota_skipped = self._apply_free_quota_filter(candidates, decision)
        candidates, budget_skipped = self._apply_paid_budget_filter(candidates)
        catalog_skipped = list(catalog_skipped) + dead_skipped

        if not candidates:
            self.log_request(**log_base, status="no_candidate", http_status=503, attempt=0)
            route = self.config.route(OPENAI, decision.hint)
            status_map: dict[str, str] = {}
            for name in _status_channels(decision, route):
                channel = self.config.channel(name)
                if channel is None:
                    status_map[name] = "missing"
                elif not channel.enabled:
                    status_map[name] = "disabled"
                elif name in blocked:
                    status_map[name] = "blocked"
                else:
                    with self.store.lock:
                        ok, why = self.store.states[name].available(time.time(), channel.rate)
                    status_map[name] = "ok" if ok else why
            message = "no healthy channel with TTS capability for this route/model"
            if status_map and all(
                    s in ("disabled", "missing") for s in status_map.values()):
                message = ("no enabled channel for this route — enable a channel in "
                           "the console or add one that speaks this protocol")
            if quota_skipped:
                message = ("free quota exhausted for this model — routing stopped "
                           "to avoid per-call billing")
            error = {
                "message": message,
                "route": f"{OPENAI}:{decision.hint}",
                "model": requested_model,
                "blocked_channels": blocked,
                "channel_status": status_map,
            }
            if catalog_skipped:
                error["catalog_skipped"] = catalog_skipped
            if quota_skipped:
                error["free_quota_skipped"] = quota_skipped
            _json(h, 503, {
                "error": error,
                "channels": self.store.snapshot()["channels"],
            }, {**audit, "X-Routed-Via": "none", "X-Failover-Count": "0"})
            return

        attempt = 0
        failures: list[dict] = []
        queue = list(candidates[: self.config.defaults.max_failover + 1])
        swapped: set[str] = set()
        while queue:
            channel, wire = queue.pop(0)
            for try_no in range(self.config.defaults.network_retries + 1):
                now = time.time()
                with self.store.lock:
                    ok, why = self.store.states[channel.name].available(now, channel.rate)
                    if not ok:
                        failures.append({"channel": channel.name, "reason": why})
                        break
                    self.store.states[channel.name].note_attempt(now)

                started = time.perf_counter()
                error = self._forward_audio_speech(h, channel, wire, body, attempt, audit)
                entry = {
                    **log_base, "channel": channel.name, "wire_model": wire,
                    "attempt": attempt, "ms": round((time.perf_counter() - started) * 1000, 1),
                    "billing": self._billing_label(channel),
                }
                if error is None:
                    self.log_request(**entry, status="ok", http_status=200, tokens=0)
                    return

                reason = error[1]
                entry["reason"] = reason
                self.log_request(
                    **entry,
                    status="network_error" if reason.startswith("transient_") else "upstream_error",
                    http_status=error[0], cooldown_seconds=round(error[2], 1),
                )
                attempt += 1
                failures.append({
                    "channel": channel.name,
                    "status": error[0],
                    "reason": reason,
                    "cooldown_seconds": round(error[2], 1),
                })
                if reason != "transient_network":
                    spare = self._spare_after_failure(channel, wire, decision, swapped)
                    if spare:
                        queue.insert(0, (channel, spare))
                    break

        _json(h, 503, {
            "error": {"message": "all channels failed for this request", "failures": failures},
            "channels": self.store.snapshot()["channels"],
        }, {**audit, "X-Routed-Via": "none", "X-Failover-Count": str(attempt)})

    def _forward_audio_speech(self, h, channel, wire, body, attempt, audit):
        """Relay one TTS attempt. Return None on success, else (status, reason, seconds)."""
        payload = {
            "model": wire,
            "input": body.get("input"),
        }
        # Optional parameters passed through to upstream
        if "voice" in body:
            payload["voice"] = body["voice"]
        if "response_format" in body:
            payload["response_format"] = body["response_format"]
        if "speed" in body:
            payload["speed"] = body["speed"]
        if "user" in body:
            payload["user"] = body["user"]

        upstream_path = upstream_path_for(channel.base_url, channel.upstream_path, OPENAI, endpoint="speech")
        headers = {"Content-Type": "application/json"}
        if channel.api_key:
            headers["Authorization"] = f"Bearer {channel.api_key}"
        apply_upstream_headers(headers, channel.base_url,
            body=payload, api_key=channel.api_key)

        req = Request(
            channel.base_url + upstream_path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        now = time.time()
        try:
            resp = open_with_proxy(req, channel.timeout, channel.proxy)
        except HTTPError as exc:
            raw_resp = exc.read()
            quota, seconds, reason = parse_cooldown(
                exc.code, exc.headers, raw_resp,
                channel.cooldown_minutes, self.config.defaults.max_cooldown_hours, now,
                not_found_hours=self.config.defaults.not_found_cooldown_hours,
                daily_hours=self.config.defaults.daily_429_cooldown_hours,
            )
            excerpt = raw_resp.decode("utf-8", "replace")[:300]
            low = excerpt.lower()
            account_scoped = _account_scoped_failure(quota, low, channel)
            if account_scoped and any(m in low for m in ACCOUNT_BALANCE_MARKERS):
                # The vendor is pointing at the account's money, not this
                # model's grant: nothing resets on its own, so cooling for
                # hours only replays the rejection. Retire the channel
                # (persisted) instead; re-enabling is the operator's call.
                self._disable_account_channel(channel, excerpt)
                self.record_usage(channel.name, False, model=wire, h=h)
                return (exc.code, "account_disabled", 0.0)
            per_model = quota and not account_scoped
            if quota:
                plan_seconds, plan_reason = self._plan_reset_cooldown(channel, now)
                if plan_seconds > seconds:
                    seconds, reason = plan_seconds, plan_reason
                    per_model = False
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = f"HTTP {exc.code}: {excerpt}"
                state.mark_cooldown(
                    seconds, reason, now, per_model=per_model, wire=wire,
                    half_open=not quota,
                )
            self.record_usage(channel.name, False, model=wire, h=h)
            if not quota:
                self.store.record_score(channel.name, wire, -5)
            return (exc.code, reason, seconds)
        except (URLError, TimeoutError, OSError) as exc:
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = str(exc)[:300]
                state.mark_cooldown(30.0, "transient_network", now, half_open=True)
            self.record_usage(channel.name, False, model=wire, h=h)
            self.store.record_score(channel.name, wire, -5)
            return (502, "transient_network", 30.0)
        except Exception as exc:
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = str(exc)[:300]
                state.mark_cooldown(30.0, "transient_internal", now, half_open=True)
            self.record_usage(channel.name, False, model=wire, h=h)
            self.store.record_score(channel.name, wire, -5)
            return (502, "transient_internal", 30.0)
        else:
            try:
                with resp:
                    return self._relay_audio_binary(h, resp, channel, wire, attempt, audit)
            except HTTPError as exc:
                raw_resp = exc.read()
                quota, seconds, reason = parse_cooldown(
                    exc.code, exc.headers, raw_resp,
                    channel.cooldown_minutes, self.config.defaults.max_cooldown_hours, now,
                    not_found_hours=self.config.defaults.not_found_cooldown_hours,
                    daily_hours=self.config.defaults.daily_429_cooldown_hours,
                )
                excerpt = raw_resp.decode("utf-8", "replace")[:300]
                low = excerpt.lower()
                account_scoped = _account_scoped_failure(quota, low, channel)
                if account_scoped and any(m in low for m in ACCOUNT_BALANCE_MARKERS):
                    # Same verdict as the chat paths: an out-of-credit account
                    # will not heal, so retire the channel instead of cooling.
                    self._disable_account_channel(channel, excerpt)
                    self.record_usage(channel.name, False, model=wire, h=h)
                    return (exc.code, "account_disabled", 0.0)
                per_model = quota and not account_scoped
                if quota:
                    plan_seconds, plan_reason = self._plan_reset_cooldown(channel, now)
                    if plan_seconds > seconds:
                        seconds, reason = plan_seconds, plan_reason
                        per_model = False
                with self.store.lock:
                    state = self.store.states[channel.name]
                    state.last_error = f"HTTP {exc.code}: {excerpt}"
                    state.mark_cooldown(
                        seconds, reason, now, per_model=per_model, wire=wire,
                        half_open=not quota,
                    )
                self.record_usage(channel.name, False, model=wire, h=h)
                if not quota:
                    self.store.record_score(channel.name, wire, -5)
                return (exc.code, reason, seconds)
            except (BrokenPipeError, ConnectionResetError):
                with self.store.lock:
                    self.store.states[channel.name].mark_success(0, time.time(), wire)
                self.store.record_score(channel.name, wire, 1)
                return None
            except Exception as exc:
                with self.store.lock:
                    self.store.states[channel.name].last_error = str(exc)[:300]
                return None

    def _relay_audio_binary(self, h, resp, channel, wire, attempt, audit):
        """Relay successful binary audio response to client."""
        extra = {
            **dict(audit or {}),
            "X-Routed-Via": _header_safe(f"{channel.name}/{wire}"),
            "X-Failover-Count": str(attempt),
        }
        # For binary responses, stream directly without loading all into memory
        content_type = resp.headers.get("Content-Type") or "audio/mpeg"
        h.send_response(200)
        h.send_header("Content-Type", content_type)
        # Don't set Content-Length for streaming
        for key, value in extra.items():
            h.send_header(key, value)
        h.end_headers()
        
        total_bytes = 0
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                h.wfile.write(chunk)
                total_bytes += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected - still mark success for upstream
            pass
        
        with self.store.lock:
            self.store.states[channel.name].mark_success(0, time.time(), wire)
        self.store.record_score(channel.name, wire, 1)
        self.record_usage(channel.name, True, 0, model=wire, h=h)

    def _audio_transcriptions(self, h: BaseHTTPRequestHandler, raw: bytes, endpoint: str = "transcriptions") -> None:
        """Handle POST /v1/audio/transcriptions and /v1/audio/translations — OpenAI-compatible STT endpoints.

        Request shape (multipart/form-data):
        - file: (required) audio file
        - model: (required) model name
        - language: (optional) ISO-639-1 language code
        - prompt: (optional) text to guide the model
        - response_format: (optional) json, text, srt, verbose_json, vtt
        - temperature: (optional) sampling temperature
        - timestamp_granularities: (optional) ["word", "segment"]

        Returns JSON transcription result. Routes to channels with STT capability.
        """
        # Parse multipart form data
        try:
            parsed = _parse_multipart(dict(h.headers), raw)
        except ValueError as exc:
            _json(h, 400, {"error": {"message": f"invalid multipart body: {exc}"}})
            return
        except Exception as exc:
            _json(h, 400, {"error": {"message": f"multipart parse error: {exc}"}})
            return

        fields = parsed["fields"]
        files = parsed["files"]

        # Extract required fields
        model = fields.get("model") or ""
        if not model:
            _json(h, 400, {"error": {"message": "model is required"}})
            return

        file_info = files.get("file")
        if not file_info:
            _json(h, 400, {"error": {"message": "file is required"}})
            return

        filename, file_content, file_content_type = file_info
        requested_model = str(model).strip() or AUTO_MODEL
        _log_inbound_shape({"model": requested_model, "file": filename}, len(raw))

        blocked = self.blocked_channels()
        decision = resolve_model(
            self.config, OPENAI, requested_model, h.headers.get("X-Route"), blocked,
        )
        audit = _audit_headers(decision)
        log_base = {
            "api": f"audio_{endpoint}",
            "protocol": OPENAI,
            "model": decision.requested,
            "mode": decision.mode,
            "reason": decision.reason,
            "route": decision.hint,
            "stream": False,
            "sticky": False,
            "rid": _rid(),
        }

        if decision.mode == "rejected":
            self.log_request(**log_base, status="rejected", http_status=400, attempt=0)
            _json(h, 400, {
                "error": {
                    "message": "unknown model; internal scheduling is disabled",
                    "model": requested_model,
                    "hint": "use a branded model name or add the model to a channel's models mapping",
                },
                "models": [item["id"] for item in self.model_list()["data"]],
            }, audit)
            return

        # Filter candidates for STT capability
        candidates, _ = self.store.pick(
            OPENAI, decision.hint, decision.exposed, None,
            decision=decision, blocked=blocked,
            agent_candidates=None,
        )
        candidates = [(channel, wire) for channel, wire in candidates
                      if channel.name not in blocked]
        # Apply catalog filter
        candidates, catalog_skipped = self._apply_catalog_filter(candidates, decision)
        candidates, dead_skipped = self._apply_dead_filter(candidates)
        candidates, quota_skipped = self._apply_free_quota_filter(candidates, decision)
        candidates, budget_skipped = self._apply_paid_budget_filter(candidates)
        catalog_skipped = list(catalog_skipped) + dead_skipped

        if not candidates:
            self.log_request(**log_base, status="no_candidate", http_status=503, attempt=0)
            route = self.config.route(OPENAI, decision.hint)
            status_map: dict[str, str] = {}
            for name in _status_channels(decision, route):
                channel = self.config.channel(name)
                if channel is None:
                    status_map[name] = "missing"
                elif not channel.enabled:
                    status_map[name] = "disabled"
                elif name in blocked:
                    status_map[name] = "blocked"
                else:
                    with self.store.lock:
                        ok, why = self.store.states[name].available(time.time(), channel.rate)
                    status_map[name] = "ok" if ok else why
            message = f"no healthy channel with {endpoint} capability for this route/model"
            if status_map and all(
                    s in ("disabled", "missing") for s in status_map.values()):
                message = ("no enabled channel for this route — enable a channel in "
                           "the console or add one that speaks this protocol")
            if quota_skipped:
                message = ("free quota exhausted for this model — routing stopped "
                           "to avoid per-call billing")
            error = {
                "message": message,
                "route": f"{OPENAI}:{decision.hint}",
                "model": requested_model,
                "blocked_channels": blocked,
                "channel_status": status_map,
            }
            if catalog_skipped:
                error["catalog_skipped"] = catalog_skipped
            if quota_skipped:
                error["free_quota_skipped"] = quota_skipped
            _json(h, 503, {
                "error": error,
                "channels": self.store.snapshot()["channels"],
            }, {**audit, "X-Routed-Via": "none", "X-Failover-Count": "0"})
            return

        attempt = 0
        failures: list[dict] = []
        queue = list(candidates[: self.config.defaults.max_failover + 1])
        swapped: set[str] = set()
        while queue:
            channel, wire = queue.pop(0)
            for try_no in range(self.config.defaults.network_retries + 1):
                now = time.time()
                with self.store.lock:
                    ok, why = self.store.states[channel.name].available(now, channel.rate)
                    if not ok:
                        failures.append({"channel": channel.name, "reason": why})
                        break
                    self.store.states[channel.name].note_attempt(now)

                started = time.perf_counter()
                error = self._forward_audio_transcriptions(h, channel, wire, fields, file_info, endpoint, attempt, audit)
                entry = {
                    **log_base, "channel": channel.name, "wire_model": wire,
                    "attempt": attempt, "ms": round((time.perf_counter() - started) * 1000, 1),
                    "billing": self._billing_label(channel),
                }
                if error is None:
                    self.log_request(**entry, status="ok", http_status=200, tokens=0)
                    return

                reason = error[1]
                entry["reason"] = reason
                self.log_request(
                    **entry,
                    status="network_error" if reason.startswith("transient_") else "upstream_error",
                    http_status=error[0], cooldown_seconds=round(error[2], 1),
                )
                attempt += 1
                failures.append({
                    "channel": channel.name,
                    "status": error[0],
                    "reason": reason,
                    "cooldown_seconds": round(error[2], 1),
                })
                if reason != "transient_network":
                    spare = self._spare_after_failure(channel, wire, decision, swapped)
                    if spare:
                        queue.insert(0, (channel, spare))
                    break

        _json(h, 503, {
            "error": {"message": "all channels failed for this request", "failures": failures},
            "channels": self.store.snapshot()["channels"],
        }, {**audit, "X-Routed-Via": "none", "X-Failover-Count": str(attempt)})

    def _forward_audio_transcriptions(self, h, channel, wire, fields, file_info, endpoint, attempt, audit):
        """Relay one STT/translation attempt. Return None on success, else (status, reason, seconds)."""
        filename, file_content, file_content_type = file_info

        # Build multipart request for upstream
        import uuid
        boundary = uuid.uuid4().hex
        body_parts = []

        def add_field(name, value):
            body_parts.append(f'--{boundary}')
            body_parts.append(f'Content-Disposition: form-data; name="{name}"')
            body_parts.append('')
            body_parts.append(str(value))

        # Add model
        add_field("model", wire)

        # Add optional fields
        for key in ("language", "prompt", "response_format", "temperature", "timestamp_granularities"):
            if key in fields and fields[key] is not None:
                add_field(key, fields[key])

        # Add file
        body_parts.append(f'--{boundary}')
        body_parts.append(f'Content-Disposition: form-data; name="file"; filename="{filename}"')
        body_parts.append(f'Content-Type: {file_content_type}')
        body_parts.append('')
        body_parts.append('')  # placeholder for file content

        # Build the body with file content inserted
        prefix = "\r\n".join(body_parts[:-1]) + "\r\n"  # everything before file content
        suffix = "\r\n" + f"--{boundary}--\r\n"
        
        body_bytes = prefix.encode("utf-8") + file_content + suffix.encode("utf-8")

        upstream_path = upstream_path_for(channel.base_url, channel.upstream_path, OPENAI, endpoint=endpoint)
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        if channel.api_key:
            headers["Authorization"] = f"Bearer {channel.api_key}"
        apply_upstream_headers(headers, channel.base_url,
            body=fields, api_key=channel.api_key)

        req = Request(
            channel.base_url + upstream_path,
            data=body_bytes,
            headers=headers,
            method="POST",
        )
        now = time.time()
        try:
            resp = open_with_proxy(req, channel.timeout, channel.proxy)
        except HTTPError as exc:
            raw_resp = exc.read()
            quota, seconds, reason = parse_cooldown(
                exc.code, exc.headers, raw_resp,
                channel.cooldown_minutes, self.config.defaults.max_cooldown_hours, now,
                not_found_hours=self.config.defaults.not_found_cooldown_hours,
                daily_hours=self.config.defaults.daily_429_cooldown_hours,
            )
            excerpt = raw_resp.decode("utf-8", "replace")[:300]
            low = excerpt.lower()
            account_scoped = _account_scoped_failure(quota, low, channel)
            if account_scoped and any(m in low for m in ACCOUNT_BALANCE_MARKERS):
                # The vendor is pointing at the account's money, not this
                # model's grant: nothing resets on its own, so cooling for
                # hours only replays the rejection. Retire the channel
                # (persisted) instead; re-enabling is the operator's call.
                self._disable_account_channel(channel, excerpt)
                self.record_usage(channel.name, False, model=wire, h=h)
                return (exc.code, "account_disabled", 0.0)
            per_model = quota and not account_scoped
            if quota:
                plan_seconds, plan_reason = self._plan_reset_cooldown(channel, now)
                if plan_seconds > seconds:
                    seconds, reason = plan_seconds, plan_reason
                    per_model = False
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = f"HTTP {exc.code}: {excerpt}"
                state.mark_cooldown(
                    seconds, reason, now, per_model=per_model, wire=wire,
                    half_open=not quota,
                )
            self.record_usage(channel.name, False, model=wire, h=h)
            if not quota:
                self.store.record_score(channel.name, wire, -5)
            return (exc.code, reason, seconds)
        except (URLError, TimeoutError, OSError) as exc:
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = str(exc)[:300]
                state.mark_cooldown(30.0, "transient_network", now, half_open=True)
            self.record_usage(channel.name, False, model=wire, h=h)
            self.store.record_score(channel.name, wire, -5)
            return (502, "transient_network", 30.0)
        except Exception as exc:
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = str(exc)[:300]
                state.mark_cooldown(30.0, "transient_internal", now, half_open=True)
            self.record_usage(channel.name, False, model=wire, h=h)
            self.store.record_score(channel.name, wire, -5)
            return (502, "transient_internal", 30.0)
        else:
            try:
                with resp:
                    return self._relay_audio_json(h, resp, channel, wire, attempt, audit)
            except HTTPError as exc:
                raw_resp = exc.read()
                quota, seconds, reason = parse_cooldown(
                    exc.code, exc.headers, raw_resp,
                    channel.cooldown_minutes, self.config.defaults.max_cooldown_hours, now,
                    not_found_hours=self.config.defaults.not_found_cooldown_hours,
                    daily_hours=self.config.defaults.daily_429_cooldown_hours,
                )
                excerpt = raw_resp.decode("utf-8", "replace")[:300]
                low = excerpt.lower()
                account_scoped = _account_scoped_failure(quota, low, channel)
                if account_scoped and any(m in low for m in ACCOUNT_BALANCE_MARKERS):
                    # Same verdict as the chat paths: an out-of-credit account
                    # will not heal, so retire the channel instead of cooling.
                    self._disable_account_channel(channel, excerpt)
                    self.record_usage(channel.name, False, model=wire, h=h)
                    return (exc.code, "account_disabled", 0.0)
                per_model = quota and not account_scoped
                if quota:
                    plan_seconds, plan_reason = self._plan_reset_cooldown(channel, now)
                    if plan_seconds > seconds:
                        seconds, reason = plan_seconds, plan_reason
                        per_model = False
                with self.store.lock:
                    state = self.store.states[channel.name]
                    state.last_error = f"HTTP {exc.code}: {excerpt}"
                    state.mark_cooldown(
                        seconds, reason, now, per_model=per_model, wire=wire,
                        half_open=not quota,
                    )
                self.record_usage(channel.name, False, model=wire, h=h)
                if not quota:
                    self.store.record_score(channel.name, wire, -5)
                return (exc.code, reason, seconds)
            except (BrokenPipeError, ConnectionResetError):
                with self.store.lock:
                    self.store.states[channel.name].mark_success(0, time.time(), wire)
                self.store.record_score(channel.name, wire, 1)
                return None
            except Exception as exc:
                with self.store.lock:
                    self.store.states[channel.name].last_error = str(exc)[:300]
                return None

    def _relay_audio_json(self, h, resp, channel, wire, attempt, audit):
        """Relay successful JSON response (transcription/translation) to client."""
        extra = {
            **dict(audit or {}),
            "X-Routed-Via": _header_safe(f"{channel.name}/{wire}"),
            "X-Failover-Count": str(attempt),
        }
        data = resp.read()
        h.send_response(200)
        h.send_header("Content-Type", resp.headers.get("Content-Type") or "application/json")
        h.send_header("Content-Length", str(len(data)))
        for key, value in extra.items():
            h.send_header(key, value)
        h.end_headers()
        h.wfile.write(data)
        with self.store.lock:
            self.store.states[channel.name].mark_success(0, time.time(), wire)
        self.store.record_score(channel.name, wire, 1)
        self.record_usage(channel.name, True, 0, model=wire, h=h)

    # --------------------------------------------------------------- models
    def model_list(self) -> dict:
        """Advertise our own ids first, then every explicitly declared model."""
        blocked = self.blocked_channels()
        policy = self.config.models
        data: list[dict] = []
        seen: set[str] = set()

        # Capability sets per protocol (router-level endpoints the protocol can serve).
        # OpenAI protocol channels can serve all router endpoints; Anthropic serves
        # chat/messages and embeddings via conversion. This is a conservative baseline;
        # actual upstream capability may vary and is discovered via catalog/maintenance.
        PROTOCOL_CAPABILITIES: dict[str, list[str]] = {
            OPENAI: [
                "chat", "completions", "embeddings", "rerank",
                "speech", "transcriptions", "translations", "responses",
            ],
            ANTHROPIC: ["chat", "embeddings"],
        }

        def route_capabilities(route_name: str) -> list[str]:
            """Aggregate capabilities from all enabled channels in a route."""
            route_key = f"openai:{route_name}"  # internal models use openai protocol
            route = self.config.routes.get(route_key)
            if not route:
                return []
            caps: set[str] = set()
            for ch_name in route.channels:
                ch = self.config.channel(ch_name)
                if ch and ch.enabled and ch.name not in blocked:
                    caps.update(PROTOCOL_CAPABILITIES.get(ch.protocol, []))
            return sorted(caps)

        def add(
            name: str,
            owned_by: str,
            mode: str,
            route: str | None = None,
            capabilities: list[str] | None = None,
        ) -> None:
            if not name or name in seen:
                return
            seen.add(name)
            item: dict = {
                "id": name,
                "object": "model",
                "owned_by": owned_by,
                "mode": mode,
            }
            if route:
                item["route"] = route
            if capabilities:
                item["capabilities"] = capabilities
            data.append(item)

        # Branded models (facet-*) inherit capabilities from their target route
        for name, hint in policy.branded.items():
            add(name, "facet", "auto", hint, route_capabilities(hint))
        # Internal auto models
        add(AUTO_MODEL, "planrouter", "auto", "default", route_capabilities("default"))
        for key in sorted(self.config.routes):
            name = key.split(":", 1)[1]
            if name == "default":
                continue
            add(f"auto:{name}", "planrouter", "auto", name, route_capabilities(name))
            add(f"facet:{name}", "facet", "auto", name, route_capabilities(name))
        # Explicit channel models: capabilities from the channel's protocol
        for channel in self.config.channels:
            if not channel.enabled or channel.name in blocked:
                continue
            proto_caps = PROTOCOL_CAPABILITIES.get(channel.protocol, [])
            for name in sorted(explicit_models(channel)):
                add(name, channel.name, "exact", channel.protocol, proto_caps)
        return {
            "object": "list",
            "data": data,
            "policy": {
                "fallback_unknown": policy.fallback_unknown,
                "fallback_route": policy.fallback_route,
                "note": "unknown model names are scheduled internally when fallback_unknown is true",
            },
        }

    # ------------------------------------------------------------- completion
    def _completion(self, h: BaseHTTPRequestHandler, protocol: str, raw: bytes,
                    responses: bool = False, legacy: bool = False) -> None:
        try:
            body = json.loads(raw)
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return
        if not isinstance(body, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return
        requested_model = str(body.get("model") or "").strip() or AUTO_MODEL
        adapter = None
        if responses:
            body = to_chat_request(body)
            adapter = ResponsesAdapter(requested_model, bool(body.get("stream")))
        elif legacy:
            body = legacy_to_chat_request(body)
            adapter = CompletionsAdapter(requested_model, bool(body.get("stream")))
        model = str(body.get("model") or "").strip() or AUTO_MODEL
        _log_inbound_shape(body, len(raw))
        metadata = body.get("metadata")
        session = (
            h.headers.get("X-Session-Id")
            or body.get("user")
            or (metadata.get("user_id") if isinstance(metadata, dict) else None)
        )
        if not isinstance(session, str):
            session = None

        blocked = self.blocked_channels()
        decision = resolve_model(
            self.config, protocol, model, h.headers.get("X-Route"), blocked,
        )
        route = self.config.route(protocol, decision.hint)
        if route is not None and route.system_prompt:
            body = inject_system(body, route.system_prompt)
        audit = _audit_headers(decision)
        log_base = {
            "api": "responses" if responses else ("completions" if legacy else
                                                 ("chat" if protocol == OPENAI else "messages")),
            "protocol": protocol,
            "model": decision.requested,
            "mode": decision.mode,
            "reason": decision.reason,
            "route": decision.hint,
            "stream": bool(body.get("stream")),
            "sticky": bool(session),
            "rid": _rid(),
        }
        if decision.mode == "rejected":
            self.log_request(**log_base, status="rejected", http_status=400, attempt=0)
            _json(h, 400, {
                "error": {
                    "message": "unknown model; internal scheduling is disabled",
                    "model": model,
                    "hint": "use a branded model name or add the model to a channel's models mapping",
                },
                "models": [item["id"] for item in self.model_list()["data"]],
            }, audit)
            return

        if decision.mode == "agent":
            agent_candidates = self._agent_candidates()
        else:
            agent_candidates = None
        candidates, _ = self.store.pick(
            protocol, decision.hint, decision.exposed, session,
            decision=decision, blocked=blocked,
            agent_candidates=agent_candidates,
        )
        candidates = [(channel, wire) for channel, wire in candidates
                      if channel.name not in blocked]
        # Multimodal requests only go to channels that can take them. A
        # channel with `vision_model` gets its wire re-pointed before the
        # capability filters run, so every filter sees the final model;
        # exact requests keep the client's model and are only filtered.
        needs_vision = body_has_vision(body)
        if needs_vision:
            audit["X-Routed-Vision"] = "1"
            log_base["vision"] = True
            if decision.mode != "exact":
                candidates = [(channel, vision_wire(channel, wire))
                              for channel, wire in candidates]
        candidates, catalog_skipped = self._apply_catalog_filter(candidates, decision)
        candidates, dead_skipped = self._apply_dead_filter(candidates)
        candidates, quota_skipped = self._apply_free_quota_filter(candidates, decision)
        candidates, budget_skipped = self._apply_paid_budget_filter(candidates)
        vision_skipped: list = []
        if needs_vision:
            candidates, vision_skipped = self._apply_vision_filter(candidates)
        catalog_skipped = list(catalog_skipped) + dead_skipped
        if decision.mode == "fallback":
            log_base["model_unverified"] = not self.store.catalog_knows(decision.requested)
            if log_base["model_unverified"]:
                audit["X-Routed-Model-Unverified"] = "1"
        if not candidates:
            self.log_request(**log_base, status="no_candidate", http_status=503, attempt=0)
            # Say *why* each route channel is out, so a fresh install (seeded
            # templates stay disabled until keys are filled) gets an actionable
            # hint instead of a bare "no healthy channel".
            route = self.config.route(protocol, decision.hint)
            status_map: dict[str, str] = {}
            for name in _status_channels(decision, route):
                channel = self.config.channel(name)
                if channel is None:
                    status_map[name] = "missing"
                elif not channel.enabled:
                    status_map[name] = "disabled"
                elif name in blocked:
                    status_map[name] = "blocked"
                else:
                    with self.store.lock:
                        ok, why = self.store.states[name].available(time.time(), channel.rate)
                    status_map[name] = "ok" if ok else why
            message = "no healthy channel for this route/model"
            if status_map and all(
                    s in ("disabled", "missing") for s in status_map.values()):
                message = ("no enabled channel for this route — enable a channel in "
                           "the console or add one that speaks this protocol")
            if quota_skipped:
                message = ("free quota exhausted for this model — routing stopped "
                           "to avoid per-call billing")
            if vision_skipped:
                message = ("no image-capable channel for this route — every "
                           "candidate is text-only; give one channel a "
                           "'vision_model' or drop its 'vision': false")
            if budget_skipped:
                message = ("monthly paid budget exhausted — paid channels are "
                           "skipped until the budget is raised or the month "
                           "resets (free channels keep serving)")
            error = {
                "message": message,
                "route": f"{protocol}:{decision.hint}",
                "model": model,
                "blocked_channels": blocked,
                "channel_status": status_map,
            }
            if catalog_skipped:
                error["catalog_skipped"] = catalog_skipped
            if quota_skipped:
                error["free_quota_skipped"] = quota_skipped
            if vision_skipped:
                error["vision_skipped"] = vision_skipped
            if budget_skipped:
                error["paid_budget_skipped"] = budget_skipped
            _json(h, 503, {
                "error": error,
                "channels": self.store.snapshot()["channels"],
            }, {**audit, "X-Routed-Via": "none", "X-Failover-Count": "0"})
            return

        with self.store.lock:
            sticky = self.store.session_channel(session, time.time())
        failures: list[dict] = []
        attempt = 0
        queue = list(candidates[: self.config.defaults.max_failover + 1])
        swapped: set[str] = set()
        while queue:
            channel, wire = queue.pop(0)
            # Transient network errors (URLError/timeout/OSError) happen before
            # any byte reaches the client, so the same channel is retried
            # immediately (half_open lets the re-check pass). Any other failure
            # moves on to the next candidate.
            for try_no in range(self.config.defaults.network_retries + 1):
                now = time.time()
                with self.store.lock:
                    ok, why = self.store.states[channel.name].available(now, channel.rate)
                    if not ok:
                        failures.append({"channel": channel.name, "reason": why})
                        break
                    self.store.states[channel.name].note_attempt(now)

                handoff = sticky is not None and sticky != channel.name
                delivery: dict = {"adapter": adapter} if adapter else {}
                started = time.perf_counter()
                error = self._forward(h, protocol, channel, wire, body, attempt,
                                      handoff, audit, delivery)
                entry = {
                    **log_base, "channel": channel.name, "wire_model": wire,
                    "attempt": attempt, "ms": round((time.perf_counter() - started) * 1000, 1),
                    "billing": self._billing_label(channel),
                }
                if error is None:
                    if handoff:
                        with self.store.lock:
                            self.store.states[channel.name].handoffs += 1
                    # A mid-response relay failure already sent headers to the
                    # client: audit it as relay_error instead of a clean ok.
                    relay_error = delivery.get("relay_error")
                    if relay_error:
                        entry["reason"] = relay_error
                    self.log_request(**entry,
                                     status="relay_error" if relay_error else "ok",
                                     http_status=200,
                                     tokens=delivery.get("tokens", 0),
                                     input_tokens=delivery.get("input_tokens", 0),
                                     cached_tokens=delivery.get("cached_tokens", 0))
                    self._note_billing(entry["billing"])
                    if not relay_error:
                        # A channel whose stream just died must not become the
                        # session's sticky pick for the next half hour.
                        self.store.bind_session(session, channel.name, time.time())
                    return

                if error[1] == "bad_request":
                    # The request never left the gateway (conversion failed on
                    # a malformed payload): no failover, no cooldown — just the
                    # client's own mistake spelled out.
                    message = (delivery.get("bad_request")
                               or "request could not be prepared for the upstream")
                    entry["reason"] = "bad_request"
                    self.log_request(**entry, status="bad_request", http_status=400)
                    _json(h, 400, {
                        "error": {
                            "message": (f"gateway cannot prepare this request for "
                                        f"channel {channel.name}: {message}"),
                            "channel": channel.name,
                        },
                    }, audit)
                    return

                reason = error[1]
                entry["reason"] = reason
                self.log_request(
                    **entry,
                    status="network_error" if reason.startswith("transient_") else "upstream_error",
                    http_status=error[0], cooldown_seconds=round(error[2], 1),
                )
                attempt += 1
                failures.append({
                    "channel": channel.name,
                    "status": error[0],
                    "reason": reason,
                    "cooldown_seconds": round(error[2], 1),
                })
                if reason != "transient_network":
                    spare = self._spare_after_failure(channel, wire, decision, swapped)
                    if spare:
                        queue.insert(0, (channel, spare))
                    break

        _json(h, 503, {
            "error": {"message": "all channels failed for this request", "failures": failures},
            "channels": self.store.snapshot()["channels"],
        }, {**audit, "X-Routed-Via": "none", "X-Failover-Count": str(attempt)})

    # --------------------------------------------------------------- upstream
    def _forward(self, h, protocol, channel, wire, body, attempt, handoff,
                 audit=None, delivery=None):
        """Relay one attempt. Return None on success, else (status, reason, seconds)."""
        audit = audit or {}
        if delivery is None:
            delivery = {}
        now = time.time()
        try:
            payload = dict(body)
            payload["model"] = wire
            normalize_kimi_temperature(payload)
            stream = bool(payload.get("stream"))
            # Scheduling guarantees wire_proto == protocol (Router.pick skips
            # other-protocol channels); the conversion branch below remains a
            # defensive pass-through, not a cross-protocol dispatch path.
            wire_proto = channel.protocol
            if wire_proto != protocol:
                payload = convert_request(payload, protocol, wire_proto)
            if (stream and wire_proto == OPENAI and self.config.defaults.track_stream_usage
                    and "stream_options" not in payload):
                payload["stream_options"] = {"include_usage": True}
            upstream_path = upstream_path_for(channel.base_url, channel.upstream_path, wire_proto)
            headers = {"Content-Type": "application/json"}
            if channel.api_key:
                if wire_proto == OPENAI:
                    headers["Authorization"] = f"Bearer {channel.api_key}"
                else:
                    headers["x-api-key"] = channel.api_key
            if wire_proto == ANTHROPIC:
                headers["anthropic-version"] = (
                    h.headers.get("anthropic-version") if h is not None else None
                ) or "2023-06-01"
            client_session = ""
            if h is not None:
                client_session = (h.headers.get("x-opencode-session")
                                  or h.headers.get("x-session-id") or "")
            apply_upstream_headers(headers, channel.base_url,
                body=body, api_key=channel.api_key or "",
                client_session=client_session)
            req = Request(
                channel.base_url + upstream_path,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
        except Exception as exc:
            # A malformed-but-JSON client payload (e.g. max_tokens:"1000"
            # rejected by the protocol conversion) is the caller's fault, not
            # the channel's: answer 400 without cooling down a healthy channel
            # or burning failover attempts on every sibling.
            if delivery is not None:
                delivery["bad_request"] = str(exc)[:300]
            return (400, "bad_request", 0.0)
        try:
            resp = open_with_proxy(req, channel.timeout, channel.proxy)
        except HTTPError as exc:
            raw = exc.read()
            quota, seconds, reason = parse_cooldown(
                exc.code, exc.headers, raw,
                channel.cooldown_minutes, self.config.defaults.max_cooldown_hours, now,
                not_found_hours=self.config.defaults.not_found_cooldown_hours,
                daily_hours=self.config.defaults.daily_429_cooldown_hours,
            )
            excerpt = raw.decode("utf-8", "replace")[:300]
            low = excerpt.lower()
            account_scoped = _account_scoped_failure(quota, low, channel)
            if account_scoped and any(m in low for m in ACCOUNT_BALANCE_MARKERS):
                # The vendor is pointing at the account's money, not this
                # model's grant: nothing resets on its own, so cooling for
                # hours only replays the rejection. Retire the channel
                # (persisted) instead; re-enabling is the operator's call.
                self._disable_account_channel(channel, excerpt)
                self.record_usage(channel.name, False, model=wire, h=h)
                return (exc.code, "account_disabled", 0.0)
            per_model = quota and not account_scoped
            if quota:
                # A vendor plan window that already reads exhausted means the
                # 429 is the monthly/weekly allowance being out, not rate
                # limiting: cool the channel until the window resets (falls
                # back to the parse_cooldown verdict when nothing matches).
                plan_seconds, plan_reason = self._plan_reset_cooldown(channel, now)
                if plan_seconds > seconds:
                    seconds, reason = plan_seconds, plan_reason
                    per_model = False  # a plan window binds every model on it
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = f"HTTP {exc.code}: {excerpt}"
                state.mark_cooldown(
                    seconds, reason, now, per_model=per_model, wire=wire,
                    half_open=not quota,
                )
            self.record_usage(channel.name, False, model=wire, h=h)
            # Quota exhaustion is the plan running out, not instability:
            # it cools the channel but never touches the stability score.
            if not quota:
                self.store.record_score(channel.name, wire, -5)
            return (exc.code, reason, seconds)
        except (URLError, TimeoutError, OSError) as exc:
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = str(exc)[:300]
                state.mark_cooldown(30.0, "transient_network", now, half_open=True)
            self.record_usage(channel.name, False, model=wire, h=h)
            self.store.record_score(channel.name, wire, -5)
            return (502, "transient_network", 30.0)
        except Exception as exc:  # keep the gateway alive on unexpected upstream shapes
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = str(exc)[:300]
                state.mark_cooldown(30.0, "transient_internal", now, half_open=True)
            self.record_usage(channel.name, False, model=wire, h=h)
            self.store.record_score(channel.name, wire, -5)
            return (502, "transient_internal", 30.0)
        else:
            # Time-to-first-byte feeds the weighted strategy's latency signal;
            # the full duration would measure generation length, not the channel.
            with self.store.lock:
                self.store.states[channel.name].record_latency(time.time() - now)
            try:
                with resp:
                    return self._relay(h, resp, protocol, channel, wire, attempt,
                                       handoff, stream, audit, delivery)
            except (BrokenPipeError, ConnectionResetError):
                with self.store.lock:
                    self.store.states[channel.name].mark_success(0, time.time(), wire)
                self.store.record_score(channel.name, wire, 1)
                return None
            except UpstreamStreamError as exc:
                # The vendor answered and then dropped the body: an upstream
                # transport failure, the same class as a 502 before the headers.
                # It cools the channel down and costs stability score so the
                # router stops preferring a channel whose streams keep dying.
                detail = str(exc)
                with self.store.lock:
                    state = self.store.states[channel.name]
                    state.last_error = detail
                    state.mark_cooldown(30.0, "transient_network", now, half_open=True)
                self.record_usage(channel.name, False, model=wire, h=h)
                self.store.record_score(channel.name, wire, -5)
                if delivery.get("headers_sent"):
                    # Mid-response: failover is no longer possible. Tell the
                    # client the stream was cut instead of hanging up on it.
                    delivery["relay_error"] = detail
                    if h is not None:
                        try:
                            h.wfile.write(_stream_error_event(
                                protocol, isinstance(delivery.get("adapter"),
                                                     ResponsesAdapter),
                                f"upstream stream ended early on channel "
                                f"{channel.name} ({detail}); "
                                f"the response above may be truncated"))
                            h.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            pass
                        # The client already holds a truncated body on a
                        # keep-alive socket. Close it so the client sees EOF
                        # instead of hanging on a dead stream.
                        h.close_connection = True
                    return None
                # Not one byte has reached the client: an honest failover.
                return (502, "transient_network", 30.0)
            except Exception as exc:
                # A relay failure after the upstream answered is the gateway's
                # own post-processing (usage parse, adapter), not channel
                # health: no cooldown, no score. While no byte has reached the
                # client an honest failover is still possible; mid-response
                # there is nothing left to send but the audit trail.
                detail = str(exc)[:300]
                with self.store.lock:
                    self.store.states[channel.name].last_error = detail
                if delivery is not None:
                    delivery["relay_error"] = detail
                    if not delivery.get("headers_sent"):
                        return (502, "relay_error", 0.0)
                if h is not None:
                    # Mid-response failure: the client already holds truncated
                    # headers/body on a keep-alive socket. Close it so the
                    # client sees EOF instead of hanging on a dead stream.
                    h.close_connection = True
                return None

    def _relay(self, h, resp, protocol, channel, wire, attempt, handoff, stream,
               audit=None, delivery=None):
        adapter = (delivery or {}).get("adapter")
        extra = {
            **dict(audit or {}),
            "X-Routed-Via": _header_safe(f"{channel.name}/{wire}"),
            "X-Failover-Count": str(attempt),
        }
        if handoff:
            extra["X-Session-Handoff"] = "1"
        # urlopen raises HTTPError for non-2xx, so a bare response object with
        # an error status only appears via mocks/unusual openers: relay the
        # status and body honestly instead of masking it as 200.
        if (getattr(resp, "status", 0) or 200) >= 400:
            data = resp.read()
            self.record_usage(channel.name, False, model=wire, h=h)
            if delivery is not None:
                delivery["headers_sent"] = True
            h.send_response(resp.status)
            ctype = resp.headers.get("Content-Type")
            if ctype:
                h.send_header("Content-Type", ctype)
            retry = resp.headers.get("Retry-After")
            if retry:
                h.send_header("Retry-After", retry)
            h.send_header("Content-Length", str(len(data)))
            for key, value in extra.items():
                h.send_header(key, value)
            h.end_headers()
            h.wfile.write(data)
            return None
        if stream:
            # Cross-protocol streaming (P1-3): the upstream speaks the
            # channel's protocol, so the SSE is converted incrementally into
            # the client's before (optionally) the responses adapter runs.
            converter = stream_converter(channel.protocol, protocol, model=wire)
            stream_tokens = stream_input = stream_cached = 0
            buffer = b""
            stopped = False
            try:
                # Commit to this channel only once the vendor has produced a
                # first byte: a 200 whose body never starts can still fail over.
                chunk = _read_upstream(resp, 4096)
                if delivery is not None:
                    delivery["headers_sent"] = True
                h.send_response(200)
                h.send_header("Content-Type", resp.headers.get("Content-Type") or "text/event-stream")
                h.send_header("Cache-Control", "no-cache")
                h.send_header("Connection", "close")
                for key, value in extra.items():
                    h.send_header(key, value)
                h.end_headers()
                while chunk:
                    outgoing = converter.feed(chunk) if converter is not None else chunk
                    if adapter is not None:
                        outgoing = adapter.feed(outgoing)
                    if outgoing:
                        h.wfile.write(outgoing)
                        h.wfile.flush()
                    buffer += chunk
                    if b"\n" in buffer:
                        complete, buffer = buffer.rsplit(b"\n", 1)
                        add_total, add_input, add_cached = _stream_tokens(complete, protocol)
                        stream_tokens += add_total
                        stream_input += add_input
                        stream_cached += add_cached
                    if len(buffer) > 65536:  # never parse an unbounded non-SSE line
                        buffer = b""
                    chunk = _read_upstream(resp, 4096)
            except (BrokenPipeError, ConnectionResetError):
                # The client hung up mid-stream: drop the upstream connection
                # right away so the vendor stops generating instead of billing
                # a stream nobody reads. Tokens already relayed are still
                # recorded below; only the tail write is skipped.
                stopped = True
                try:
                    resp.close()
                except Exception:
                    pass
            add_total, add_input, add_cached = _stream_tokens(buffer, protocol)
            stream_tokens += add_total
            stream_input += add_input
            stream_cached += add_cached
            if not stopped:
                tail = b""
                if converter is not None:
                    tail = converter.finish()
                if adapter is not None:
                    tail = adapter.feed(tail) + adapter.finish()
                if tail:
                    try:
                        h.wfile.write(tail)
                        h.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            with self.store.lock:
                self.store.states[channel.name].mark_success(stream_tokens, time.time(), wire)
            self.store.record_score(channel.name, wire, 1)
            self.record_usage(channel.name, True, stream_tokens, model=wire, h=h,
                              input_tokens=stream_input, cached_tokens=stream_cached)
            self.apply_quota(channel.name, resp.headers)
            if delivery is not None:
                delivery["tokens"] = stream_tokens
                delivery["input_tokens"] = stream_input
                delivery["cached_tokens"] = stream_cached
            return None

        data = resp.read()
        if channel.protocol != protocol:
            # Answer came back in the channel's protocol: convert it back to
            # the client's before relaying (P1-3).
            try:
                data = json.dumps(
                    convert_response(json.loads(data), channel.protocol, protocol),
                    ensure_ascii=False).encode("utf-8")
            except ValueError:
                pass  # a non-JSON body is relayed untouched
        tokens, input_tokens, cached_tokens = _usage(data, protocol)
        if delivery is not None:
            delivery["tokens"] = tokens
            delivery["input_tokens"] = input_tokens
            delivery["cached_tokens"] = cached_tokens
        with self.store.lock:
            self.store.states[channel.name].mark_success(tokens, time.time(), wire)
        self.store.record_score(channel.name, wire, 1)
        self.record_usage(channel.name, True, tokens, model=wire, h=h,
                          input_tokens=input_tokens, cached_tokens=cached_tokens)
        self.apply_quota(channel.name, resp.headers)
        # Hand the cache economics back to the caller as well: for non-stream
        # responses headers are the only place a downstream service can read
        # them without parsing the body (streams carry it in the final usage
        # chunk, where headers are already gone).
        if input_tokens:
            extra["X-Input-Tokens"] = str(input_tokens)
            extra["X-Cached-Tokens"] = str(cached_tokens)
        if adapter is not None:
            data = adapter.finish(data)
        if delivery is not None:
            delivery["headers_sent"] = True
        h.send_response(200)
        h.send_header("Content-Type", resp.headers.get("Content-Type") or "application/json")
        h.send_header("Content-Length", str(len(data)))
        for key, value in extra.items():
            h.send_header(key, value)
        h.end_headers()
        h.wfile.write(data)
        return None


class _BodyTooLarge(Exception):
    """Chunked body exceeded MAX_BODY_BYTES mid-stream."""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PlanRouter/0.1"
    # StreamRequestHandler socket timeout: bounds rfile reads (a half-open
    # client stalling mid-body used to pin a thread forever) and reaps idle
    # keep-alive sockets. Relays write small chunks as upstream data arrives,
    # so a healthy stream never blocks a single write for this long.
    timeout = 120

    def do_GET(self):  # noqa: N802
        self._run("GET")

    def do_POST(self):  # noqa: N802
        self._run("POST")

    def do_DELETE(self):  # noqa: N802
        self._run("DELETE")

    def _read_chunked_body(self) -> bytes:
        """Read a Transfer-Encoding: chunked body (RFC 9112 §7.1).

        Dart's HttpClientRequest sends chunked whenever contentLength is left
        unset, and HTTP/1.1 allows any client to do the same — so a chunked
        request must be assembled here instead of reading a zero-length
        body and failing with "invalid JSON body". Trailer lines after the
        final 0-chunk are consumed and discarded.
        """
        chunks: list[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline(1024)
            if not line:
                raise ValueError("truncated chunk header")
            if not line.endswith(b"\n"):
                raise ValueError("chunk header too long")
            try:
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
            except ValueError:
                raise ValueError("invalid chunk size") from None
            if size == 0:
                break  # final chunk; trailers follow
            if total + size > MAX_BODY_BYTES:
                raise _BodyTooLarge()
            chunk = self.rfile.read(size)
            if len(chunk) < size:
                raise ValueError("truncated chunk")
            total += size
            chunks.append(chunk)
            if self.rfile.read(2) != b"\r\n":
                raise ValueError("missing chunk terminator")
        # Trailers may follow the final chunk; consume them up to the blank
        # line so those bytes don't leak into the next keep-alive request.
        budget = 16384
        while True:
            line = self.rfile.readline(budget)
            if not line:
                raise ValueError("truncated chunk trailers")
            if line in (b"\r\n", b"\n"):
                break
            if not line.endswith(b"\n"):
                raise ValueError("chunk trailer too long")
            budget -= len(line)
            if budget <= 0:
                raise ValueError("chunk trailers too large")
        return b"".join(chunks)

    def _run(self, method: str):
        # RFC 9110 §8.6: a chunked request must be assembled even when a
        # Content-Length is also present; TE takes precedence.
        chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()
        try:
            if chunked:
                raw = self._read_chunked_body()
            else:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    # The body is left unread on the socket: under keep-alive those
                    # bytes would be parsed as the next request, so close instead.
                    _json(self, 400, {"error": {"message": "invalid Content-Length"}}, close=True)
                    return
                if length > MAX_BODY_BYTES:
                    # Same as above: the oversize body is never consumed, so the
                    # connection must end here (send_header also sets close_connection).
                    _json(self, 413, {"error": {"message": "request body too large"}}, close=True)
                    return
                raw = self.rfile.read(length) if length else b""
        except _BodyTooLarge:
            # Unread chunks remain on the socket; keep-alive would misparse
            # them as the next request, so close instead of answering 413.
            self.close_connection = True
            _json(self, 413, {"error": {"message": "request body too large"}}, close=True)
            return
        except TimeoutError:
            # The socket timeout (Handler.timeout) fired mid-body: answer and
            # drop the connection instead of leaving the client hanging.
            self.close_connection = True
            _json(self, 408, {"error": {"message": "request body read timed out"}},
                  close=True)
            return
        except ValueError:
            # Malformed chunk stream; the same unread-body-on-socket rule
            # applies, so close rather than try keep-alive.
            self.close_connection = True
            _json(self, 400, {"error": {"message": "invalid chunked body"}}, close=True)
            return
        self.server.app.dispatch(self, method, urlsplit(self.path).path, raw)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[planrouter] " + (fmt % args) + "\n")


def make_server(
    config: Config,
    host: str | None = None,
    port: int | None = None,
    config_path: str | Path | None = None,
    state_file: str | Path | None = None,
    subscriptions_file: str | Path | None = None,
    usage_file: str | Path | None = None,
    notifications_file: str | Path | None = None,
    audit_file: str | Path | None = None,
    keys_file: str | Path | None = None,
) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(
        (host or config.listen.host, port or config.listen.port), Handler,
    )
    httpd.daemon_threads = True
    app = App(
        config,
        config_path=config_path,
        state_file=state_file,
        subscriptions_file=subscriptions_file,
        usage_file=usage_file,
        notifications_file=notifications_file,
        audit_file=audit_file,
        keys_file=keys_file,
    )
    app.start_background()
    httpd.app = app  # type: ignore[attr-defined]
    return httpd
