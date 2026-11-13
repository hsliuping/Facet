"""Local HTTP gateway: one compatible endpoint, automatic channel failover."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from datetime import datetime

from . import subscriptions as subs
from . import ui
from . import maintenance as maint
from . import dsh as dsh_mod
from . import freealign as freealign_mod
from . import providers as providers_mod
from . import facts as facts_mod
from . import ark_quota
from .audit import RequestLog
from .completions import CompletionsAdapter
from .completions import to_chat_request as legacy_to_chat_request
from .convert import convert_request, convert_response, inject_system, stream_converter
from .core import (
    ANTHROPIC, AUTO_MODEL, EMBEDDING_MODEL, OPENAI, QUOTA_MARKERS, Config,
    StateStore, channel_ref,
    body_has_vision, channel_takes_images, ensure_channel_ids, explicit_models,
    open_with_proxy, parse_cooldown, parse_config, parse_free_quota,
    resolve_model, resolve_secret, upstream_path_for, vision_wire,
)
from .keys import KeyStore
from .notifier import ReminderService
from .persistence import StatePersistence
from .probe import MAX_CHANNELS, pick_model, probe_channel, probe_models
from .quota import parse_quota, quota_exhaustion_wait
from .report import build_report
from .responses import ResponsesAdapter, to_chat_request
from .usage import UsageRecorder

MAX_BODY_BYTES = 8 * 1024 * 1024
UNSAFE_HEADER = re.compile(r"[^A-Za-z0-9._:/@+ -]")
# First-run management credential: simple to type, replaced by the console's
# rotate action (or by listen.admin_key) as soon as the deployment is set up.
DEFAULT_ADMIN_KEY = "admin"
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
ACCOUNT_SCOPED_MARKERS = (
    "insufficient_quota", "billing", "credit", "balance", "arrearage",
    "payment", "余额不足", "欠费", "账户", "账号",
)
# A channel failing this many times in a row (no success in between) deserves
# a runtime alert: something is broken even while cooldowns route around it.
RUNTIME_ALERT_ERROR_THRESHOLD = 3


def _header_safe(value) -> str:
    """Client-controlled values must never be able to inject a response header."""
    return UNSAFE_HEADER.sub("-", str(value))[:120]


def _audit_headers(decision) -> dict:
    return {
        "X-Routed-Model-Mode": decision.mode,
        "X-Routed-Model-Reason": decision.reason,
        "X-Routed-Requested-Model": _header_safe(decision.requested),
        "X-Route": _header_safe(decision.hint),
    }


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
    actually reached the gateway and how large it was.
    """
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


def _json(h: BaseHTTPRequestHandler, status: int, payload: dict, extra: dict | None = None) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
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
        raise ValueError("not multipart/form-data")

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
    return (f"应用 {len(report.get('applied') or [])} 项"
            f" · 建议 {len(report.get('suggestions') or [])} 条"
            f" · 注意 {len(report.get('alerts') or [])} 条")


def _free_align_summary(report: dict) -> str:
    text = f"{len(report.get('aligned') or [])} 个免费渠道已核对"
    changed = len(report.get("patch") or {})
    if changed:
        text += f" · 改写 {changed} 个映射"
    alerts = len(report.get("alerts") or [])
    if alerts:
        text += f" · {alerts} 个失败"
    return text


def _row_better(old: dict, new: dict) -> bool:
    """Pick the best channel entry for one shared wire model: free beats paid,
    known tool-call beats unknown beats none, then the higher stability score."""
    def rank(row: dict) -> tuple:
        tool = (0 if row["tool_call"] is True
                else 1 if row["tool_call"] is None else 2)
        tier = 0 if row["tier"] == "free" else 1
        return (tier, tool, -row["score"])

    return rank(new) < rank(old)


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
        self.running = False

    def hours(self) -> float:
        return float(getattr(self.app.config.defaults, self.hours_key) or 24)

    def interval(self) -> float:
        return max(1.0, self.hours()) * 3600.0

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
            self._run()
            self.next_run_at = time.time() + self.interval()
            if self._stop.wait(self.interval()):
                return

    def _run(self) -> None:
        self.running = True
        self.last_run_at = time.time()
        try:
            self.last_summary = self.run_once() or ""
            self.last_error = ""
        except Exception as exc:  # a broken sweep must never kill the gateway
            self.last_error = str(exc) or exc.__class__.__name__
        finally:
            self.running = False

    def run_once(self) -> str:
        raise NotImplementedError

    def status(self) -> dict:
        enabled = bool(getattr(self.app.config.defaults, self.hours_key))
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "enabled": enabled,
            "hours": self.hours() if enabled else 0,
            "state": "running" if self.running else ("idle" if enabled else "disabled"),
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at if enabled else 0,
            "last_summary": self.last_summary,
            "last_error": self.last_error,
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

    def run_once(self) -> str:
        return _free_align_summary(self.app.run_free_align("scheduled"))


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
            self._cycle()

    def _cycle(self) -> None:
        now = time.time()
        targets = [c for c in self._targets(now)
                   if now >= self._deferred.get(c.name, 0.0)]
        if not targets:
            return
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
        # Subscriptions written before channel ids existed link by name; point
        # them at the ids now, while those names still resolve.
        if self._migrate_subscription_channels(self.subscriptions) \
                and self.subscriptions_path:
            try:
                subs.save_file(self.subscriptions_path, self.subscriptions,
                               self.subscription_settings)
            except OSError:
                pass
        # Vendor registry (builtin providers.json + optional user overlay in
        # the config dir); a broken overlay must not keep the gateway down.
        try:
            self.providers = providers_mod.load_providers(
                self.config_path.parent if self.config_path else None)
        except (OSError, ValueError):
            self.providers = providers_mod.load_providers(None)
        self.persistence = StatePersistence(state_file, lambda: self.store) if state_file else None
        if self.persistence:
            self.persistence.restore_from_disk()
        self.usage = UsageRecorder(usage_file) if usage_file else None
        self.notifier = ReminderService(
            self.subscriptions_path, notifications_file,
            lambda: self._sub_records(), lambda: self.subscription_settings,
            get_health=lambda: self.runtime_alerts(),
        ) if notifications_file else None
        self.audit = RequestLog(audit_file) if audit_file else None
        # Member keys for small-team attribution; absent file -> no members,
        # and the gateway behaves exactly as before.
        self.keys = KeyStore(keys_file) if keys_file else None
        # Vendor-reported free quota from the last alignment sweep. Kept next
        # to the config so a restart keeps showing real numbers, and so the
        # free-quota gate can see consumption the gateway itself did not make.
        self.vendor_quota: dict = {}
        self.vendor_quota_at = ""
        # Vendor-side plan usage for the subscriptions page (Volcengine
        # Coding/Agent Plan via GetPersonalPlan + per-plan usage APIs).
        # Cached per credential pair with a TTL and persisted like
        # vendor_quota: a restart shows the snapshot and refreshes on the
        # first page load. A channel-bound AK/SK and the vendor-level
        # fallback therefore each get their own cache slot.
        self.plan_entries: dict[str, dict] = {}
        self._plan_lock = threading.Lock()
        self._load_vendor_quota()
        self.catalog_refresher: CatalogRefresher | None = None
        self.maintenance_runner: "MaintenanceRunner | None" = None
        self.free_align_runner: "FreeAlignRunner | None" = None
        self.maintenance_reports: list[dict] = []
        self.free_align_reports: list[dict] = []
        self.maintenance_lock = threading.Lock()
        # Verified maintenance-model queue (see maintenance.probe_maintenance_models).
        self.maintenance_pool = maint.load_maintenance_pool(self)
        self.maintenance_pool_lock = threading.Lock()

    def log_request(self, **fields) -> None:
        if self.audit:
            self.audit.record(**fields)

    def request_log_payload(self, limit: int = 50) -> dict:
        if not self.audit:
            return {"entries": [], "path": None, "size": 0,
                    "note": "请求审计未启用（启动时未指定 --audit-file）"}
        return {
            "entries": self.audit.recent(limit),
            "path": str(self.audit.path),
            "size": self.audit.size(),
            "note": "仅记录选路元数据，不含 prompt、密钥与模型输出",
        }

    def start_background(self) -> None:
        if self.persistence:
            self.persistence.start()
        if self.usage:
            self.usage.start()
        if self.notifier:
            self.notifier.start()
        # The task objects exist even when a sweep is switched off, so the
        # console can still list it (and show the last manual run).
        self.catalog_refresher = CatalogRefresher(self)
        self.maintenance_runner = MaintenanceRunner(self)
        self.free_align_runner = FreeAlignRunner(self)
        if self.config.defaults.catalog_refresh_hours:
            self.catalog_refresher.start()
        if self.config.defaults.maintenance_hours:
            self.maintenance_runner.start()
        if self.config.defaults.free_align_hours:
            self.free_align_runner.start()
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
        if self.catalog_refresher:
            self.catalog_refresher.stop()
        if self.health_prober:
            self.health_prober.stop()
        if self.notifier:
            self.notifier.stop()
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
                 "plans": plans},
                ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # quota display must never break the sweep

    # -------------------------------------------------- vendor plan usage
    def _plan_creds_for(self, record: dict) -> tuple[str, str, str]:
        """(kind, ak, sk) for a subscription row: bound channel first.

        Both the vendor gate and the plan kind come from the linked channel
        (its provider plus business line / billing mode), never from free text
        on the subscription: a subscription has no vendor of its own.

        Plan endpoints are Volcengine-only, so a Bailian/OpenRouter row must
        not get decorated with (or fail on) an Ark management-plane call.

        If the record has an explicit `account_id`, we look up the account
        by id from config.accounts and use its credentials; otherwise the
        channel credential chain applies.
        """
        linked = self._linked_channels(record)
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

    def plan_for(self, record: dict) -> dict | None:
        """Vendor plan usage for one subscription row, TTL-cached per creds.

        Returns None when the row has no usable AK/SK; a fetch failure
        returns ``{"error": ...}`` (plus the last good snapshot marked
        stale when one exists). Snapshots persist through vendorquota.json.
        """
        kind, ak, sk = self._plan_creds_for(record)
        if not ak or not sk:
            return None
        key = f"{kind}:{hashlib.sha1(f'{ak}:{sk}'.encode()).hexdigest()[:12]}"
        with self._plan_lock:
            now = time.time()
            entry = self.plan_entries.get(key) or {}
            snapshot, error, ts = (entry.get("snapshot"), entry.get("error", ""),
                                   entry.get("ts", 0.0))
            if now - ts < (PLAN_TTL if snapshot else PLAN_ERROR_TTL):
                if error and not snapshot:
                    return {"error": error}
                if error and snapshot:
                    return {**snapshot, "stale": True, "error": error}
                return snapshot
            fetch = ark_quota.fetch_agent_plan if kind == "agent" \
                else ark_quota.fetch_coding_plan
            try:
                plan = fetch(ak, sk)
            except Exception as exc:
                error = str(exc)[:160]
                self.plan_entries[key] = {"snapshot": snapshot, "error": error,
                                          "ts": now}
                if snapshot:
                    return {**snapshot, "stale": True, "error": error}
                return {"error": error}
            snapshot = {**plan, "fetched_at": datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S")}
            self.plan_entries[key] = {"snapshot": snapshot, "error": "",
                                      "ts": now}
            self._save_vendor_quota()
            return snapshot

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

    def free_quota_spec(self, channel, wire: str = "", exposed: str = ""):
        """Resolved free-tier allowance for one model on this channel.

        Only models that actually count as free are capped: a paid channel
        sharing the same vendor must never be throttled by the free baseline.
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
        if not eligible:
            return None
        if channel.free_quota is not None:
            return channel.free_quota
        raw = self.provider_mode(channel.provider, channel.mode).get("free_quota")
        try:
            return parse_free_quota(raw)
        except ValueError:
            return None

    def free_quota_status(self, channel, wire: str, exposed: str = "") -> dict | None:
        """Local usage vs the free allowance; exhausted means "never route it".

        The vendor's own numbers (last alignment sweep) are carried alongside
        the gateway counter: they are the only truth for account-level quotas
        that other clients also draw from, so zero vendor remaining blocks the
        model exactly like a locally reached cap does.
        """
        spec = self.free_quota_spec(channel, wire, exposed)
        vendor = (self.vendor_quota.get(channel.name) or {}).get(wire) or {}
        if spec is None and not vendor:
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
        exhausted = bool(
            (spec is not None and spec.tokens is not None
             and tokens_used >= spec.tokens)
            or (spec is not None and spec.requests is not None
                and requests_used >= spec.requests)
            or (vendor and (vendor.get("remaining") or 0) <= 0)
        )
        ratios = []
        if spec is not None and spec.tokens:
            ratios.append(tokens_used / spec.tokens)
        if spec is not None and spec.requests:
            ratios.append(requests_used / spec.requests)
        if vendor and vendor.get("total"):
            ratios.append(1 - (vendor.get("remaining") or 0) / vendor["total"])
        ratio = max(ratios) if ratios else 0.0
        return {
            "source": ("vendor" if spec is None else
                       "channel" if channel.free_quota is not None
                       else "provider_baseline"),
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
            "vendor_remaining": vendor.get("remaining"),
            "vendor_total": vendor.get("total"),
            "vendor_consumed": vendor.get("consumed"),
            "vendor_at": self.vendor_quota_at or None,
        }

    def free_quota_exhausted(self, channel, wire: str, exposed: str = "") -> bool:
        status = self.free_quota_status(channel, wire, exposed)
        return bool(status and status["exhausted"])

    def runtime_alerts(self) -> list[dict]:
        """Live conditions worth alarming about (P1-2 runtime alerts).

        Three families: a route chain whose every usable channel is cooling or
        blocked, a free-tier model whose allowance is exhausted, and a channel
        failing repeatedly. Config gaps (no key, everything disabled) stay
        silent — those are console hints, not runtime alarms.
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
        and POST fails with 409 if the server version has changed.
        """
        import hashlib
        visible = copy.deepcopy(raw)
        # Mask secrets before hashing
        for channel in (visible.get("channels") or []):
            key = channel.get("api_key")
            if key and not str(key).startswith("env:"):
                channel["api_key"] = "***"
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
        for account in (visible.get("accounts") or []):
            if not isinstance(account, dict):
                continue
            acc_creds = account.get("credentials")
            if isinstance(acc_creds, dict):
                for env_name, value in acc_creds.items():
                    if value and not str(value).startswith("env:"):
                        acc_creds[env_name] = "***"
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
        ensure_channel_ids(merged)
        config = parse_config(merged)  # validate before touching the file
        self._check_vendor_binding(config)
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
            except OSError:
                pass
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
        expected = (self.config.listen.admin_key
                    or (DEFAULT_ADMIN_KEY if self.config.listen.api_key else ""))
        if not expected:
            return True
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
        """Replace the master gateway key after confirming the password."""
        if not self._verify_admin(admin_key):
            raise ValueError("管理密码不正确")
        value = (key or "").strip() or "sk-router-" + secrets.token_hex(20)
        if len(value) < 8:
            raise ValueError("主密钥至少 8 个字符")
        raw = dict(self.raw)
        listen = dict(raw.get("listen") or {})
        listen["api_key"] = value
        listen["api_key_enabled"] = True
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
            raise ValueError("config file not found or unreadable")
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

    def test_channels(self, channels, model: str | None = None) -> dict:
        """Probe console drafts without touching cooldown, usage or audit state.

        Items may be full channel objects (an unsaved channel draft) or bare
        names (a subscription's linked channels, resolved from saved config).
        """
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
            results = list(pool.map(lambda channel: probe_channel(channel, wanted), merged))
        self._append_probe_log(results)
        return {
            "ok": all(result.get("ok") for result in results),
            "results": results,
            "note": "已向每个渠道发送一次最小非流式请求（max_tokens=16）；"
                    "不计入冷却、用量与审计，可能产生极少量费用",
        }

    def _append_probe_log(self, results: list[dict], kind: str = "probe") -> None:
        """Persist one JSON line per probe/catalog batch; secrets are masked."""
        if not self.config_path:
            return
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                 "kind": kind, "results": results}
        try:
            path = self.config_path.parent / "probe.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging must never break the console

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

    def probe_channel_models(self, channel, allow_ark_quota: bool = False) -> dict:
        """Fetch one channel's upstream model list for the console.

        `channel` may be a full draft (unsaved form) or a bare name. Secrets
        marked *** are merged from the saved config; a successful fetch for a
        saved channel name is recorded in the channel's catalog.

        allow_ark_quota=True (console-initiated pulls) lets volcengine payg
        channels take their catalog from the IAM-signed management plane when
        credentials are available; the background catalog sweep keeps using
        the data-plane route.
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
        else:
            result = probe_models(merged)
        if result.get("ok") and isinstance(name, str) and self.config.channel(name):
            self.store.record_catalog(name, result["models"], result["endpoint"])
            view = self.store.catalog_view(name)
            result["catalog"] = view
            result["changes"] = view["changes"]
        self._append_probe_log([result], kind="catalog")
        return result

    def refresh_all_catalogs(self) -> list[dict]:
        """Re-fetch every enabled channel's upstream model catalog."""
        results = []
        for channel in list(self.config.channels):
            if not channel.enabled or not channel.api_key:
                continue
            payload = {"name": channel.name}
            if channel.provider:
                payload["provider"] = channel.provider
            if channel.mode:
                payload["mode"] = channel.mode
            try:
                results.append(self.probe_channel_models(payload))
            except Exception as exc:  # one broken channel must not stop the sweep
                results.append({"ok": False, "name": channel.name,
                                "error": repr(exc)[:200]})
        return results

    # ---------------------------------------------------------- maintenance
    def run_maintenance(self, trigger: str = "manual") -> dict:
        """One maintenance pass: refresh catalogs, auto-fix, analyse, report.

        Only catalog-confirmed changes are applied automatically (after a
        config backup); LLM output and quota heuristics become suggestions
        for the console. Never raises: failures land in the report.
        """
        refresh = self.refresh_all_catalogs()
        with self.store.lock:
            catalogs = {name: dict(entry) for name, entry in self.store.catalogs.items()}
            states = self.store.snapshot()["channels"]
        report = maint.analyze(self.config, catalogs, states, refresh)

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

        maint.refresh_plan_models(self, report)
        maint.refresh_providers(self, report)

        usage = None
        if self.usage is not None:
            usage = {c["name"]: c for c in self.usage.series(1)["channels"]}
        reply, model = self._maintenance_llm_call_any(
            maint.build_prompt(self.config, catalogs, states, report, usage))
        if reply is not None:
            try:
                parsed = maint.parse_llm_output(reply)
                report["llm_model"] = model
                report["llm_notes"] = parsed["notes"]
                report["suggestions"].extend(parsed["suggestions"])
            except Exception:  # LLM analysis is best-effort
                pass

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

    def _maintenance_direct_call(self, channel, model: str, prompt: str) -> str:
        """One non-streaming chat turn straight against the owning channel."""
        if self.free_quota_exhausted(channel, model):
            raise RuntimeError(
                f"free quota exhausted for {model} on {channel.name}; "
                "maintenance call blocked to avoid billed usage")
        protocol = channel.protocol
        if protocol == ANTHROPIC:
            payload = {"model": model, "max_tokens": 6000,
                       "messages": [{"role": "user", "content": prompt}]}
        else:
            payload = {"model": model, "max_tokens": 6000, "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}
        normalize_kimi_temperature(payload)
        headers = {"Content-Type": "application/json"}
        if channel.api_key:
            if protocol == ANTHROPIC:
                headers["x-api-key"] = channel.api_key
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {channel.api_key}"
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
                self._maintenance_direct_call(channel, model, "ping")
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
                                  validate=None) -> tuple[str | None, str | None]:
        """Run a maintenance prompt through the first working model.

        Candidates come from defaults.maintenance_model when it lists explicit
        ids, otherwise from the auto-discovered verified queue (see
        maintenance.probe_maintenance_models). A queue model that raises or
        fails validation is dropped on the spot so a dead free model cannot
        break the next run; returns (None, None) when every candidate failed.
        """
        models, mode = maint.pick_maintenance_models(self)
        for model in models:
            try:
                reply = self._maintenance_llm_call(model, prompt)
            except Exception as exc:
                if mode == "pool":
                    maint.drop_maintenance_model(self, model, str(exc))
                continue
            reply = str(reply or "")
            if reply.strip() and (validate is None or validate(reply)):
                return reply, model
            if mode == "pool":
                maint.drop_maintenance_model(
                    self, model, "空回复" if not reply.strip() else "输出未通过校验")
        return None, None

    def probe_maintenance_models(self) -> dict:
        """Quiz candidate maintenance models now and rebuild the queue."""
        return maint.probe_maintenance_models(self)

    def verify_free_models(self, channels=None) -> dict:
        """Ping every free-channel mapping; dead ids get an expiring label
        that routing then skips (see maint.dead_model_set).

        channels narrows the sweep to the named free channels, so checking
        one tier does not wait on every other tier's mappings.
        """
        pairs = None
        if channels:
            only = set(channels)
            pairs = [pair for pair in maint.free_mapping_pairs(self)
                     if pair[0].name in only]
        return maint.verify_free_models(self, pairs)

    def dsh_status(self) -> dict:
        """Report whether the DeepSeek Harness client shares this host."""
        return dsh_mod.status()

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
        operator copying a secret into its config. The profile's model list is
        refreshed first: auto modes and real backend models stay selectable
        together, and channels enabled since the last launch show up.
        """
        dsh_mod.sync_models(self.dsh_models())
        return dsh_mod.start_web(api_key=self.config.listen.api_key, port=port)

    def _backup_config(self, tag: str) -> None:
        if not self.config_path or not self.config_path.is_file():
            return
        try:
            backup = self.config_path.with_name(self.config_path.name + f".{tag}.bak")
            backup.write_bytes(self.config_path.read_bytes())
        except OSError:
            pass  # a failed backup must not block maintenance

    def _record_maintenance(self, report: dict) -> None:
        """Keep the last reports in memory and append one JSON line to disk."""
        with self.maintenance_lock:
            self.maintenance_reports.append(report)
            del self.maintenance_reports[:-MaintenanceRunner.MAX_REPORTS]
        if not self.config_path:
            return
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                 **{k: v for k, v in report.items() if k != "patch"}}
        try:
            path = self.config_path.parent / "maintenance.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging must never break maintenance

    # ----------------------------------------------------------- free align
    def run_free_align(self, trigger: str = "manual") -> dict:
        """Align free-tier channel mappings with vendor free-quota tools.

        Applies the sweep's patch through the normal config pipeline (after a
        config backup); never raises — failures land in the report alerts and
        the channels keep their current mapping until the next sweep.
        """
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
        try:
            path = self.config_path.parent / "freealign.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging must never break the sweep

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
            "dead_ttl_hours": maint.DEAD_LABEL_TTL / 3600,
            "free_scan_at": float(pool.get("free_scan_at") or 0),
            "free_models": maint.free_models_payload(self),
            "reports": list(reversed(reports)),
            "free_align": {
                "enabled": bool(self.config.defaults.free_align_hours),
                "hours": self.config.defaults.free_align_hours,
                "reports": list(reversed(free_reports)),
            },
        }

    def tasks_payload(self) -> dict:
        """The console's task list: cycle, latest outcome and next run."""
        runners = (self.catalog_refresher, self.maintenance_runner,
                   self.free_align_runner)
        return {"tasks": [runner.status() for runner in runners if runner]}

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
        agent cannot drive them. Dead (probe-verified) models and disabled
        or blocked channels are skipped too.
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
                rows.append((
                    (0 if tool is True else 1,
                     0 if tier == "free" else 1,
                     tier,
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
        return {"logs": entries, "source": source, "has_more": has_more,
                "limit": limit}

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
        rewrite the config. Restoring stays a manual file operation on purpose.
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
        for channel in raw.get("channels") or []:
            if isinstance(channel, dict) and channel.get("name") == channel_name:
                mapping = channel.get("models") or {}
                for m in models:
                    mapping[m.strip()] = m.strip()
                channel["models"] = mapping
                break
        self.apply_config(raw)
        return {"ok": True, "channel": channel_name,
                "models": [m.strip() for m in models]}

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

        Labels are per channel+wire and expire (maintenance.DEAD_LABEL_TTL),
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

    def _apply_free_quota_filter(self, candidates, decision) -> tuple[list, list]:
        """Hard stop once a free model has used its allowance.

        Unlike the catalog/dead filters this never backs off: the whole point
        is to prevent a free channel from silently switching to per-call
        billing once the vendor's grant is gone.
        """
        if not candidates:
            return candidates, []
        kept, skipped = [], []
        for channel, wire in candidates:
            status = self.free_quota_status(
                channel, wire, getattr(decision, "exposed", "") or "")
            if status and status["exhausted"]:
                skipped.append({
                    "channel": channel.name,
                    "wire_model": wire,
                    "reason": "free_quota_exhausted",
                    "quota": status,
                })
                continue
            kept.append((channel, wire))
        return kept, skipped

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

    def ui_state(self) -> dict:
        snap = self.store.snapshot()
        blocked = self.blocked_channels()
        channels = []
        for channel in self.config.channels:
            item = dict(snap["channels"].get(channel.name, {}))
            catalog = self.store.catalog_view(channel.name)
            has_key = bool(resolve_secret(channel.api_key))
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
            })
            channels.append(item)
        return {
            "channels": channels,
            "routes": {key: list(route.channels) for key, route in self.config.routes.items()},
            "model_states": self.model_states(),
            "sticky_sessions": snap["sticky_sessions"],
            "listen": {"host": self.config.listen.host, "port": self.config.listen.port},
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
        subscriptions — nothing is hardcoded. Cooldown is a channel-health
        signal (shown per channel) and deliberately not part of the status.
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
        report = self.maintenance_reports[-1]
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
        member = self.keys.match(provided) if self.keys else None
        # Resolved once here; handlers thread it into usage records as
        # `getattr(h, "user_key", "")` (tests pass h=None, default covers it).
        h.user_key = member or ""
        if member:
            return True
        expected = self.config.listen.api_key
        if not expected:
            return True
        # A paused master key stops gateway calls that use it, while the
        # member keys above keep their own independent lifecycle.
        if not self.config.listen.api_key_enabled:
            return False
        return hmac.compare_digest(provided, expected)

    def _authorized_admin(self, h: BaseHTTPRequestHandler) -> bool:
        """Management endpoints use admin_key, which starts out as "admin".

        A config with no credentials at all keeps the console open (same rule
        as the gateway); once any key is configured, "admin" must be rotated
        through the console's 管理密钥 dialog.
        """
        if self.config.listen.admin_key:
            expected = self.config.listen.admin_key
        elif not self.config.listen.api_key:
            return True
        else:
            expected = DEFAULT_ADMIN_KEY
        provided = h.headers.get("X-Admin-Key")
        if not provided:
            auth = h.headers.get("Authorization") or ""
            provided = auth[7:] if auth.lower().startswith("bearer ") else ""
        return hmac.compare_digest(provided, expected)

    # --------------------------------------------------------------- dispatch
    def dispatch(self, h: BaseHTTPRequestHandler, method: str, path: str, raw: bytes) -> None:
        if ui.handle(self, h, method, path, raw):
            return
        if not self._authorized(h):
            _json(h, 401, {"error": {"message": "invalid gateway key"}})
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
        catalog_skipped = list(catalog_skipped) + dead_skipped

        if not candidates:
            self.log_request(**log_base, status="no_candidate", http_status=503, attempt=0)
            route = self.config.route(OPENAI, decision.hint)
            status_map: dict[str, str] = {}
            for name in (route.channels if route else []):
                channel = self.config.channel(name)
                if channel is None:
                    status_map[name] = "missing"
                elif not channel.enabled:
                    status_map[name] = "disabled"
                elif name in blocked:
                    status_map[name] = "blocked"
                else:
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
        for channel, wire in candidates[: self.config.defaults.max_failover + 1]:
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
            account_scoped = any(m in low for m in ACCOUNT_SCOPED_MARKERS)
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
                state.last_error = repr(exc)[:300]
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
                    self.store.states[channel.name].last_error = repr(exc)[:300]
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
        catalog_skipped = list(catalog_skipped) + dead_skipped

        if not candidates:
            self.log_request(**log_base, status="no_candidate", http_status=503, attempt=0)
            route = self.config.route(OPENAI, decision.hint)
            status_map: dict[str, str] = {}
            for name in (route.channels if route else []):
                channel = self.config.channel(name)
                if channel is None:
                    status_map[name] = "missing"
                elif not channel.enabled:
                    status_map[name] = "disabled"
                elif name in blocked:
                    status_map[name] = "blocked"
                else:
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
        for channel, wire in candidates[: self.config.defaults.max_failover + 1]:
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
            account_scoped = any(m in low for m in ACCOUNT_SCOPED_MARKERS)
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
                state.last_error = repr(exc)[:300]
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
                account_scoped = any(m in low for m in ACCOUNT_SCOPED_MARKERS)
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
                    self.store.states[channel.name].last_error = repr(exc)[:300]
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
        catalog_skipped = list(catalog_skipped) + dead_skipped

        if not candidates:
            self.log_request(**log_base, status="no_candidate", http_status=503, attempt=0)
            route = self.config.route(OPENAI, decision.hint)
            status_map: dict[str, str] = {}
            for name in (route.channels if route else []):
                channel = self.config.channel(name)
                if channel is None:
                    status_map[name] = "missing"
                elif not channel.enabled:
                    status_map[name] = "disabled"
                elif name in blocked:
                    status_map[name] = "blocked"
                else:
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
        for channel, wire in candidates[: self.config.defaults.max_failover + 1]:
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
            account_scoped = any(m in low for m in ACCOUNT_SCOPED_MARKERS)
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
                state.last_error = repr(exc)[:300]
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
                account_scoped = any(m in low for m in ACCOUNT_SCOPED_MARKERS)
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
                    self.store.states[channel.name].last_error = repr(exc)[:300]
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
        catalog_skipped = list(catalog_skipped) + dead_skipped

        if not candidates:
            self.log_request(**log_base, status="no_candidate", http_status=503, attempt=0)
            route = self.config.route(OPENAI, decision.hint)
            status_map: dict[str, str] = {}
            for name in (route.channels if route else []):
                channel = self.config.channel(name)
                if channel is None:
                    status_map[name] = "missing"
                elif not channel.enabled:
                    status_map[name] = "disabled"
                elif name in blocked:
                    status_map[name] = "blocked"
                else:
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
        for channel, wire in candidates[: self.config.defaults.max_failover + 1]:
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
            account_scoped = any(m in low for m in ACCOUNT_SCOPED_MARKERS)
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
                state.last_error = repr(exc)[:300]
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
                account_scoped = any(m in low for m in ACCOUNT_SCOPED_MARKERS)
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
                    self.store.states[channel.name].last_error = repr(exc)[:300]
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
        vision_skipped: list = []
        if needs_vision:
            candidates, vision_skipped = self._apply_vision_filter(candidates)
        catalog_skipped = list(catalog_skipped) + dead_skipped
        if decision.mode == "fallback":
            log_base["model_unverified"] = not self.store.catalog_knows(decision.requested)
            if log_base["model_unverified"]:
                audit["X-Routed-Model-Unverified"] = "1"
        if decision.mode == "exact" and candidates and not any(
                channel.name in decision.exact_channels for channel, _ in candidates):
            audit["X-Routed-Model-Mode"] = "fallback"
            audit["X-Routed-Model-Reason"] = "exact-channels-unavailable"
            log_base["mode"] = "fallback"
            log_base["reason"] = "exact-channels-unavailable"
        if not candidates:
            self.log_request(**log_base, status="no_candidate", http_status=503, attempt=0)
            # Say *why* each route channel is out, so a fresh install (seeded
            # templates stay disabled until keys are filled) gets an actionable
            # hint instead of a bare "no healthy channel".
            route = self.config.route(protocol, decision.hint)
            status_map: dict[str, str] = {}
            for name in (route.channels if route else []):
                channel = self.config.channel(name)
                if channel is None:
                    status_map[name] = "missing"
                elif not channel.enabled:
                    status_map[name] = "disabled"
                elif name in blocked:
                    status_map[name] = "blocked"
                else:
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
            _json(h, 503, {
                "error": error,
                "channels": self.store.snapshot()["channels"],
            }, {**audit, "X-Routed-Via": "none", "X-Failover-Count": "0"})
            return

        sticky = self.store.session_channel(session, time.time())
        failures: list[dict] = []
        attempt = 0
        for channel, wire in candidates[: self.config.defaults.max_failover + 1]:
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
                }
                if error is None:
                    if handoff:
                        with self.store.lock:
                            self.store.states[channel.name].handoffs += 1
                    self.log_request(**entry, status="ok", http_status=200,
                                     tokens=delivery.get("tokens", 0))
                    self.store.bind_session(session, channel.name, time.time())
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
        payload = dict(body)
        payload["model"] = wire
        normalize_kimi_temperature(payload)
        stream = bool(payload.get("stream"))
        # The channel speaks its own wire protocol; when that differs from the
        # client's, the payload is converted right here at the boundary (P1-3).
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
            raw = exc.read()
            quota, seconds, reason = parse_cooldown(
                exc.code, exc.headers, raw,
                channel.cooldown_minutes, self.config.defaults.max_cooldown_hours, now,
                not_found_hours=self.config.defaults.not_found_cooldown_hours,
                daily_hours=self.config.defaults.daily_429_cooldown_hours,
            )
            excerpt = raw.decode("utf-8", "replace")[:300]
            low = excerpt.lower()
            account_scoped = any(m in low for m in ACCOUNT_SCOPED_MARKERS)
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
            self.record_usage(channel.name, False, model=wire)
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
                state.last_error = repr(exc)[:300]
                state.mark_cooldown(30.0, "transient_internal", now, half_open=True)
            self.record_usage(channel.name, False, model=wire, h=h)
            self.store.record_score(channel.name, wire, -5)
            return (502, "transient_internal", 30.0)
        else:
            # Headers may already be on the wire here: never fail over again.
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
            except Exception as exc:
                with self.store.lock:
                    self.store.states[channel.name].last_error = repr(exc)[:300]
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
                h.send_response(200)
                h.send_header("Content-Type", resp.headers.get("Content-Type") or "text/event-stream")
                h.send_header("Cache-Control", "no-cache")
                h.send_header("Connection", "close")
                for key, value in extra.items():
                    h.send_header(key, value)
                h.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
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
        with self.store.lock:
            self.store.states[channel.name].mark_success(tokens, time.time(), wire)
        self.store.record_score(channel.name, wire, 1)
        self.record_usage(channel.name, True, tokens, model=wire, h=h,
                          input_tokens=input_tokens, cached_tokens=cached_tokens)
        self.apply_quota(channel.name, resp.headers)
        if adapter is not None:
            data = adapter.finish(data)
        h.send_response(200)
        h.send_header("Content-Type", resp.headers.get("Content-Type") or "application/json")
        h.send_header("Content-Length", str(len(data)))
        for key, value in extra.items():
            h.send_header(key, value)
        h.end_headers()
        h.wfile.write(data)
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PlanRouter/0.1"

    def do_GET(self):  # noqa: N802
        self._run("GET")

    def do_POST(self):  # noqa: N802
        self._run("POST")

    def do_DELETE(self):  # noqa: N802
        self._run("DELETE")

    def _run(self, method: str):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            _json(self, 400, {"error": {"message": "invalid Content-Length"}})
            return
        if length > MAX_BODY_BYTES:
            _json(self, 413, {"error": {"message": "request body too large"}})
            return
        raw = self.rfile.read(length) if length else b""
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
