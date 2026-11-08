"""Local HTTP gateway: one compatible endpoint, automatic channel failover."""
from __future__ import annotations

import copy
import hmac
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import subscriptions as subs
from . import ui
from . import maintenance as maint
from . import freealign as freealign_mod
from . import providers as providers_mod
from .audit import RequestLog
from .core import (
    ANTHROPIC, AUTO_MODEL, OPENAI, Config, StateStore, explicit_models,
    open_with_proxy, parse_cooldown, parse_config, resolve_model,
    upstream_path_for,
)
from .notifier import ReminderService
from .persistence import StatePersistence
from .probe import MAX_CHANNELS, probe_channel, probe_models
from .quota import parse_quota, quota_exhaustion_wait
from .report import build_report
from .responses import ResponsesAdapter, to_chat_request
from .usage import UsageRecorder

MAX_BODY_BYTES = 8 * 1024 * 1024
UNSAFE_HEADER = re.compile(r"[^A-Za-z0-9._:/@+ -]")


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


def _json(h: BaseHTTPRequestHandler, status: int, payload: dict, extra: dict | None = None) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    for key, value in (extra or {}).items():
        h.send_header(key, str(value))
    h.end_headers()
    h.wfile.write(data)


def _usage(data: bytes, protocol: str) -> int:
    try:
        usage = (json.loads(data).get("usage") or {})
    except (ValueError, AttributeError):
        return 0
    try:
        if protocol == OPENAI:
            return int(usage.get("total_tokens") or 0)
        return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def _stream_tokens(data: bytes, protocol: str) -> int:
    """Sum `usage` blocks from SSE data lines (OpenAI final chunk, Anthropic deltas)."""
    total = 0
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
        try:
            if protocol == OPENAI:
                total += int(usage.get("total_tokens") or 0)
            else:
                total += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            continue
    return total


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


def channel_tier(channel, subscriptions: list[dict]) -> str:
    """Explicit channel.tier wins; otherwise infer from linked subscriptions."""
    if channel.tier:
        return channel.tier
    for record in subscriptions:
        if channel.name in (record.get("channels") or []):
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
    previous = {
        c.get("name"): c for c in (old_raw.get("channels") or [])
        if isinstance(c, dict)
    }
    for channel in (merged.get("channels") or []):
        if not isinstance(channel, dict):
            continue
        if channel.get("api_key") in (None, "", "***"):
            key = previous.get(channel.get("name"), {}).get("api_key")
            if key:
                channel["api_key"] = key
            else:
                channel.pop("api_key", None)
        # Vendor bookkeeping is newer than some open console tabs: a stale
        # editor omits provider/mode entirely and would otherwise erase them
        # one channel per save. Absent keys inherit; explicit values win.
        prev = previous.get(channel.get("name"), {})
        for field in ("provider", "mode"):
            if field not in channel and prev.get(field):
                channel[field] = prev[field]
    listen = merged.setdefault("listen", {})
    for field in ("api_key", "admin_key"):
        if listen.get(field) in (None, "", "***"):
            key = (old_raw.get("listen") or {}).get(field)
            if key:
                listen[field] = key
            else:
                listen.pop(field, None)
    return merged


class CatalogRefresher:
    """Background sweep: re-fetch every channel's upstream model catalog.

    Runs once shortly after startup, then every
    defaults.catalog_refresh_hours (re-read each cycle so hot-reloaded
    config takes effect; 0 still means enabled — the sweep simply uses the
    default interval until restart disables it).
    """

    FIRST_SWEEP_DELAY = 30.0

    def __init__(self, app: "App"):
        self.app = app
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="planrouter-catalog")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _interval(self) -> float:
        hours = self.app.config.defaults.catalog_refresh_hours
        return max(1.0, float(hours or 24)) * 3600.0

    def _loop(self) -> None:
        if self._stop.wait(self.FIRST_SWEEP_DELAY):
            return
        self.app.refresh_all_catalogs()
        while not self._stop.wait(self._interval()):
            self.app.refresh_all_catalogs()


class MaintenanceRunner:
    """Daily self-maintenance: catalog-confirmed auto-fixes + free-quota LLM analysis.

    Runs once shortly after startup (after the first catalog sweep), then
    every defaults.maintenance_hours. One run = refresh catalogs, apply
    catalog-confirmed changes (with a config backup), optionally ask the
    maintenance LLM for advisory notes, and record a report.
    """

    FIRST_RUN_DELAY = 120.0
    MAX_REPORTS = 20

    def __init__(self, app: "App"):
        self.app = app
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="planrouter-maintenance")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _interval(self) -> float:
        hours = self.app.config.defaults.maintenance_hours
        return max(1.0, float(hours or 24)) * 3600.0

    def _loop(self) -> None:
        if self._stop.wait(self.FIRST_RUN_DELAY):
            return
        try:
            self.app.run_maintenance("scheduled")
        except Exception:  # maintenance must never kill the gateway
            pass
        while not self._stop.wait(self._interval()):
            try:
                self.app.run_maintenance("scheduled")
            except Exception:
                pass


class FreeAlignRunner:
    """Scheduled free-quota alignment for free-tier channels (freealign.py).

    Vendors like Bailian expose their free-quota state only through a local
    CLI, and the set of models carrying free quota moves over time (models
    expire, new ones join). This sweep re-runs the vendor command and rewrites
    the affected channels' mappings so the router never routes to a model
    whose free quota has run out. Runs once shortly after the first
    maintenance sweep, then every defaults.free_align_hours.
    """

    FIRST_RUN_DELAY = 150.0

    def __init__(self, app: "App"):
        self.app = app
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="planrouter-freealign")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _interval(self) -> float:
        hours = self.app.config.defaults.free_align_hours
        return max(1.0, float(hours or 24)) * 3600.0

    def _loop(self) -> None:
        if self._stop.wait(self.FIRST_RUN_DELAY):
            return
        try:
            self.app.run_free_align("scheduled")
        except Exception:  # alignment must never kill the gateway
            pass
        while not self._stop.wait(self._interval()):
            try:
                self.app.run_free_align("scheduled")
            except Exception:
                pass


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
    ):
        self.config = config
        self.store = StateStore(config)
        self.config_path = Path(config_path) if config_path else None
        self.raw = self._read_raw()
        self.subscriptions_path = Path(subscriptions_file) if subscriptions_file else None
        try:
            self.subscriptions = subs.load_file(self.subscriptions_path)
            self.subscription_settings = subs.load_settings(self.subscriptions_path)
        except (OSError, ValueError):
            self.subscriptions = []
            self.subscription_settings = dict(subs.DEFAULT_SETTINGS)
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
            lambda: self.subscriptions, lambda: self.subscription_settings,
        ) if notifications_file else None
        self.audit = RequestLog(audit_file) if audit_file else None
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
        if self.config.defaults.catalog_refresh_hours:
            self.catalog_refresher = CatalogRefresher(self)
            self.catalog_refresher.start()
        if self.config.defaults.maintenance_hours:
            self.maintenance_runner = MaintenanceRunner(self)
            self.maintenance_runner.start()
        if self.config.defaults.free_align_hours:
            self.free_align_runner = FreeAlignRunner(self)
            self.free_align_runner.start()

    def stop_background(self) -> None:
        if self.maintenance_runner:
            self.maintenance_runner.stop()
        if self.free_align_runner:
            self.free_align_runner.stop()
        if self.catalog_refresher:
            self.catalog_refresher.stop()
        if self.notifier:
            self.notifier.stop()
        if self.usage:
            self.usage.stop()
        if self.persistence:
            self.persistence.stop()

    def record_usage(self, channel: str, success: bool, tokens: int = 0) -> None:
        if self.usage:
            self.usage.record(channel, success, tokens)

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
        return build_report(self.subscriptions, days, month)

    def blocked_channels(self) -> dict[str, str]:
        """Channels whose every linked subscription has expired."""
        if not self.subscription_settings.get("block_expired_channels", True):
            return {}
        expired: dict[str, str] = {}
        usable: set[str] = set()
        for record in self.subscriptions:
            status = subs.enrich(record)["status"]
            for name in record.get("channels") or []:
                if status == "expired":
                    expired[name] = (
                        f"订阅已过期：{record['vendor']} / "
                        f"{record['account'] or '—'} / {record['plan']}"
                    )
                else:
                    usable.add(name)
        return {name: reason for name, reason in expired.items() if name not in usable}

    def _read_raw(self) -> dict:
        if self.config_path and self.config_path.is_file():
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
        return {}

    # -------------------------------------------------- subscription management
    def subscription_summary(self) -> dict:
        return subs.summarize(self.subscriptions)

    def subscriptions_payload(self) -> dict:
        return {
            "subscriptions": [subs.enrich(record) for record in self.subscriptions],
            "summary": self.subscription_summary(),
            "settings": self.subscription_settings,
            "path": str(self.subscriptions_path) if self.subscriptions_path else None,
        }

    def save_subscriptions(self, raw) -> list[dict]:
        settings = self.subscription_settings
        if isinstance(raw, dict) and "settings" in raw:
            settings = subs.normalize_settings(raw["settings"])
        records = subs.normalize_all(raw)
        if self.subscriptions_path:
            subs.save_file(self.subscriptions_path, records, settings)
        self.subscriptions = records
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
    def public_config(self) -> dict:
        visible = copy.deepcopy(self.raw)
        for channel in (visible.get("channels") or []):
            key = channel.get("api_key")
            if key and not str(key).startswith("env:"):
                channel["api_key"] = "***"
        listen = visible.get("listen") or {}
        for field in ("api_key", "admin_key"):
            if listen.get(field) and not str(listen[field]).startswith("env:"):
                listen[field] = "***"
        policy = self.config.models
        visible["models"] = {
            "branded": dict(policy.branded),
            "fallback_unknown": policy.fallback_unknown,
            "fallback_route": policy.fallback_route,
        }
        return visible

    def apply_config(self, raw: dict, persist: bool = True) -> Config:
        merged = merge_secrets(raw, self.raw)
        config = parse_config(merged)  # validate before touching the file
        if persist and self.config_path:
            tmp = self.config_path.with_name(self.config_path.name + ".tmp")
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.config_path)
        self.raw = merged
        self._swap(config)
        return config

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

    def probe_channel_models(self, channel) -> dict:
        """Fetch one channel's upstream model list for the console.

        `channel` may be a full draft (unsaved form) or a bare name. Secrets
        marked *** are merged from the saved config; a successful fetch for a
        saved channel name is recorded in the channel's catalog.
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
                if c.enabled and model in c.models.values()]
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
        protocol = channel.protocol
        if protocol == ANTHROPIC:
            payload = {"model": model, "max_tokens": 6000,
                       "messages": [{"role": "user", "content": prompt}]}
        else:
            payload = {"model": model, "max_tokens": 6000, "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}
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
        report["finished_at"] = time.time()
        self._record_free_align(report)
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
            "free_models": maint.free_models_payload(self),
            "reports": list(reversed(reports)),
            "free_align": {
                "enabled": bool(self.config.defaults.free_align_hours),
                "hours": self.config.defaults.free_align_hours,
                "reports": list(reversed(free_reports)),
            },
        }

    def maintenance_log_payload(self, limit: int = 50) -> dict:
        """History of maintenance runs, newest first, from maintenance.jsonl.

        Reads the file tail so a long history cannot grow the response; falls
        back to the in-memory reports when no log file exists (e.g. tests).
        """
        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit = 50
        entries: list[dict] = []
        source = "memory"
        path = (self.config_path.parent / "maintenance.jsonl"
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
                    for report in self.maintenance_reports
                ]
            has_more = len(reports) > limit
            entries = list(reversed(reports[-limit:]))
        return {"logs": entries, "source": source, "has_more": has_more,
                "limit": limit}

    def test_maintenance_model(self, payload) -> dict:
        """Manual quiz for one (channel, model) from the console overview."""
        if not isinstance(payload, dict):
            raise ValueError("body 必须是对象")
        return maint.test_single_model(
            self, str(payload.get("channel") or ""),
            str(payload.get("model") or ""))

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
        scheduling (auto) is exempt — the wire model there is the channel's
        own configured mapping, not a client-supplied name.
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

    def ui_state(self) -> dict:
        snap = self.store.snapshot()
        blocked = self.blocked_channels()
        channels = []
        for channel in self.config.channels:
            item = dict(snap["channels"].get(channel.name, {}))
            catalog = self.store.catalog_view(channel.name)
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
                "catalog": {
                    "fetched_at": catalog["fetched_at"],
                    "count": catalog["count"],
                    "changes": catalog["changes"],
                },
            })
            channels.append(item)
        return {
            "channels": channels,
            "routes": {key: list(route.channels) for key, route in self.config.routes.items()},
            "sticky_sessions": snap["sticky_sessions"],
            "listen": {"host": self.config.listen.host, "port": self.config.listen.port},
            "config_path": str(self.config_path) if self.config_path else None,
            "subscriptions": self.subscription_summary(),
            "subscription_settings": self.subscription_settings,
            "blocked_channels": blocked,
            "catalog_refresh_hours": self.config.defaults.catalog_refresh_hours,
            "maintenance": {
                "hours": self.config.defaults.maintenance_hours,
                "llm_model": self.config.defaults.maintenance_model,
                "runs": len(self.maintenance_reports),
                "last": self._maintenance_summary(),
            },
        }

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
    def _authorized(self, h: BaseHTTPRequestHandler) -> bool:
        expected = self.config.listen.api_key
        if not expected:
            return True
        provided = h.headers.get("x-api-key")
        if not provided:
            auth = h.headers.get("Authorization") or ""
            provided = auth[7:] if auth.lower().startswith("bearer ") else ""
        return hmac.compare_digest(provided, expected)

    def _authorized_admin(self, h: BaseHTTPRequestHandler) -> bool:
        """Management endpoints can require a key distinct from the tool's key."""
        expected = self.config.listen.admin_key
        if not expected:
            return self._authorized(h)
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
            available = [n for n, s in snap.items() if not s["cooling"] and n not in blocked]
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

    # --------------------------------------------------------------- models
    def model_list(self) -> dict:
        """Advertise our own ids first, then every explicitly declared model."""
        blocked = self.blocked_channels()
        policy = self.config.models
        data: list[dict] = []
        seen: set[str] = set()

        def add(name: str, owned_by: str, mode: str, route: str | None = None) -> None:
            if not name or name in seen:
                return
            seen.add(name)
            item = {"id": name, "object": "model", "owned_by": owned_by, "mode": mode}
            if route:
                item["route"] = route
            data.append(item)

        for name, hint in policy.branded.items():
            add(name, "facet", "auto", hint)
        add(AUTO_MODEL, "planrouter", "auto", "default")
        for key in sorted(self.config.routes):
            name = key.split(":", 1)[1]
            if name == "default":
                continue
            add(f"auto:{name}", "planrouter", "auto", name)
            add(f"facet:{name}", "facet", "auto", name)
        for channel in self.config.channels:
            if not channel.enabled or channel.name in blocked:
                continue
            for name in sorted(explicit_models(channel)):
                add(name, channel.name, "exact", channel.protocol)
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
                    responses: bool = False) -> None:
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
        model = str(body.get("model") or "").strip() or AUTO_MODEL
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
        audit = _audit_headers(decision)
        log_base = {
            "api": "responses" if responses else ("chat" if protocol == OPENAI else "messages"),
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

        candidates, _ = self.store.pick(
            protocol, decision.hint, decision.exposed, session,
            decision=decision, blocked=blocked,
        )
        candidates = [(channel, wire) for channel, wire in candidates
                      if channel.name not in blocked]
        candidates, catalog_skipped = self._apply_catalog_filter(candidates, decision)
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
            error = {
                "message": message,
                "route": f"{protocol}:{decision.hint}",
                "model": model,
                "blocked_channels": blocked,
                "channel_status": status_map,
            }
            if catalog_skipped:
                error["catalog_skipped"] = catalog_skipped
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
        stream = bool(payload.get("stream"))
        if (stream and protocol == OPENAI and self.config.defaults.track_stream_usage
                and "stream_options" not in payload):
            payload["stream_options"] = {"include_usage": True}
        upstream_path = upstream_path_for(channel.base_url, channel.upstream_path, protocol)
        headers = {"Content-Type": "application/json"}
        if channel.api_key:
            if protocol == OPENAI:
                headers["Authorization"] = f"Bearer {channel.api_key}"
            else:
                headers["x-api-key"] = channel.api_key
        if protocol == ANTHROPIC:
            headers["anthropic-version"] = h.headers.get("anthropic-version") or "2023-06-01"
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
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = f"HTTP {exc.code}: {excerpt}"
                state.mark_cooldown(
                    seconds, reason, now, half_open=not quota,
                )
            self.record_usage(channel.name, False)
            return (exc.code, reason, seconds)
        except (URLError, TimeoutError, OSError) as exc:
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = str(exc)[:300]
                state.mark_cooldown(30.0, "transient_network", now, half_open=True)
            self.record_usage(channel.name, False)
            return (502, "transient_network", 30.0)
        except Exception as exc:  # keep the gateway alive on unexpected upstream shapes
            with self.store.lock:
                state = self.store.states[channel.name]
                state.last_error = repr(exc)[:300]
                state.mark_cooldown(30.0, "transient_internal", now, half_open=True)
            self.record_usage(channel.name, False)
            return (502, "transient_internal", 30.0)
        else:
            # Headers may already be on the wire here: never fail over again.
            try:
                with resp:
                    return self._relay(h, resp, protocol, channel, wire, attempt,
                                       handoff, stream, audit, delivery)
            except (BrokenPipeError, ConnectionResetError):
                with self.store.lock:
                    self.store.states[channel.name].mark_success(0, time.time())
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
        if stream:
            h.send_response(200)
            h.send_header("Content-Type", resp.headers.get("Content-Type") or "text/event-stream")
            h.send_header("Cache-Control", "no-cache")
            h.send_header("Connection", "close")
            for key, value in extra.items():
                h.send_header(key, value)
            h.end_headers()
            stream_tokens = 0
            buffer = b""
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    outgoing = adapter.feed(chunk) if adapter is not None else chunk
                    if outgoing:
                        h.wfile.write(outgoing)
                        h.wfile.flush()
                    buffer += chunk
                    if b"\n" in buffer:
                        complete, buffer = buffer.rsplit(b"\n", 1)
                        stream_tokens += _stream_tokens(complete, protocol)
                    if len(buffer) > 65536:  # never parse an unbounded non-SSE line
                        buffer = b""
            except (BrokenPipeError, ConnectionResetError):
                pass
            stream_tokens += _stream_tokens(buffer, protocol)
            if adapter is not None:
                tail = adapter.finish()
                if tail:
                    h.wfile.write(tail)
                    h.wfile.flush()
            with self.store.lock:
                self.store.states[channel.name].mark_success(stream_tokens, time.time())
            self.record_usage(channel.name, True, stream_tokens)
            self.apply_quota(channel.name, resp.headers)
            if delivery is not None:
                delivery["tokens"] = stream_tokens
            return None

        data = resp.read()
        tokens = _usage(data, protocol)
        if delivery is not None:
            delivery["tokens"] = tokens
        with self.store.lock:
            self.store.states[channel.name].mark_success(tokens, time.time())
        self.record_usage(channel.name, True, tokens)
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
    )
    app.start_background()
    httpd.app = app  # type: ignore[attr-defined]
    return httpd
