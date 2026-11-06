"""Local HTTP gateway: one compatible endpoint, automatic channel failover."""
from __future__ import annotations

import copy
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import subscriptions as subs
from . import ui
from .core import (
    ANTHROPIC, OPENAI, Config, StateStore, parse_cooldown, parse_config,
)
from .notifier import ReminderService
from .persistence import StatePersistence
from .usage import UsageRecorder

MAX_BODY_BYTES = 8 * 1024 * 1024


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


def merge_secrets(new_raw: dict, old_raw: dict) -> dict:
    """Keep stored secrets when the editor sends '***' or an empty value."""
    merged = copy.deepcopy(new_raw)
    previous = {
        c.get("name"): c for c in (old_raw.get("channels") or [])
        if isinstance(c, dict)
    }
    for channel in (merged.get("channels") or []):
        if isinstance(channel, dict) and channel.get("api_key") in (None, "", "***"):
            key = previous.get(channel.get("name"), {}).get("api_key")
            if key:
                channel["api_key"] = key
            else:
                channel.pop("api_key", None)
    listen = merged.setdefault("listen", {})
    if listen.get("api_key") in (None, "", "***"):
        key = (old_raw.get("listen") or {}).get("api_key")
        if key:
            listen["api_key"] = key
        else:
            listen.pop("api_key", None)
    return merged


class App:
    def __init__(
        self,
        config: Config,
        config_path: str | Path | None = None,
        state_file: str | Path | None = None,
        subscriptions_file: str | Path | None = None,
        usage_file: str | Path | None = None,
        notifications_file: str | Path | None = None,
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
        self.persistence = StatePersistence(state_file, lambda: self.store) if state_file else None
        if self.persistence:
            self.persistence.restore_from_disk()
        self.usage = UsageRecorder(usage_file) if usage_file else None
        self.notifier = ReminderService(
            self.subscriptions_path, notifications_file,
            lambda: self.subscriptions, lambda: self.subscription_settings,
        ) if notifications_file else None

    def start_background(self) -> None:
        if self.persistence:
            self.persistence.start()
        if self.usage:
            self.usage.start()
        if self.notifier:
            self.notifier.start()

    def stop_background(self) -> None:
        if self.notifier:
            self.notifier.stop()
        if self.usage:
            self.usage.stop()
        if self.persistence:
            self.persistence.stop()

    def record_usage(self, channel: str, success: bool, tokens: int = 0) -> None:
        if self.usage:
            self.usage.record(channel, success, tokens)

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

    # ------------------------------------------------------- config management
    def public_config(self) -> dict:
        visible = copy.deepcopy(self.raw)
        for channel in (visible.get("channels") or []):
            key = channel.get("api_key")
            if key and not str(key).startswith("env:"):
                channel["api_key"] = "***"
        listen = visible.get("listen") or {}
        if listen.get("api_key") and not str(listen["api_key"]).startswith("env:"):
            listen["api_key"] = "***"
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
            return True

    def ui_state(self) -> dict:
        snap = self.store.snapshot()
        blocked = self.blocked_channels()
        channels = []
        for channel in self.config.channels:
            item = dict(snap["channels"].get(channel.name, {}))
            item.update({
                "name": channel.name,
                "protocol": channel.protocol,
                "priority": channel.priority,
                "enabled": channel.enabled,
                "cooldown_minutes": channel.cooldown_minutes,
                "blocked": channel.name in blocked,
                "blocked_reason": blocked.get(channel.name, ""),
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
                "note": "total_tokens covers non-streaming responses only",
                **snap,
            })
            return
        if method == "GET" and path == "/v1/models":
            blocked = self.blocked_channels()
            names = {"auto", "auto:cheap"}
            for channel in self.config.channels:
                if channel.enabled and channel.name not in blocked:
                    names.update(m for m in channel.models if m not in ("*", "auto"))
            _json(h, 200, {
                "object": "list",
                "data": [{"id": n, "object": "model", "owned_by": "planrouter"} for n in sorted(names)],
            })
            return
        if method == "POST" and path == "/v1/chat/completions":
            self._completion(h, OPENAI, raw)
            return
        if method == "POST" and path == "/v1/messages":
            self._completion(h, ANTHROPIC, raw)
            return
        _json(h, 404, {"error": {"message": "not found", "path": path}})

    # ------------------------------------------------------------- completion
    def _completion(self, h: BaseHTTPRequestHandler, protocol: str, raw: bytes) -> None:
        try:
            body = json.loads(raw)
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return
        if not isinstance(body, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return
        model = str(body.get("model") or "auto")
        hint = h.headers.get("X-Route")
        if not hint and model.startswith("auto:"):
            hint = model.split(":", 1)[1]
        hint = hint or "default"
        metadata = body.get("metadata")
        session = (
            h.headers.get("X-Session-Id")
            or body.get("user")
            or (metadata.get("user_id") if isinstance(metadata, dict) else None)
        )
        if not isinstance(session, str):
            session = None

        blocked = self.blocked_channels()
        candidates, _ = self.store.pick(protocol, hint, model, session)
        candidates = [(channel, wire) for channel, wire in candidates
                      if channel.name not in blocked]
        if not candidates:
            _json(h, 503, {
                "error": {
                    "message": "no healthy channel for this route/model",
                    "route": f"{protocol}:{hint}",
                    "model": model,
                    "blocked_channels": blocked,
                },
                "channels": self.store.snapshot()["channels"],
            })
            return

        sticky = self.store.session_channel(session, time.time())
        failures: list[dict] = []
        attempt = 0
        for channel, wire in candidates[: self.config.defaults.max_failover + 1]:
            now = time.time()
            with self.store.lock:
                ok, why = self.store.states[channel.name].available(now, channel.rate)
                if not ok:
                    failures.append({"channel": channel.name, "reason": why})
                    continue
                self.store.states[channel.name].note_attempt(now)

            handoff = sticky is not None and sticky != channel.name
            error = self._forward(h, protocol, channel, wire, body, attempt, handoff)
            if error is None:
                if handoff:
                    with self.store.lock:
                        self.store.states[channel.name].handoffs += 1
                self.store.bind_session(session, channel.name, time.time())
                return

            attempt += 1
            failures.append({
                "channel": channel.name,
                "status": error[0],
                "reason": error[1],
                "cooldown_seconds": round(error[2], 1),
            })

        _json(h, 503, {
            "error": {"message": "all channels failed for this request", "failures": failures},
            "channels": self.store.snapshot()["channels"],
        }, {"X-Routed-Via": "none", "X-Failover-Count": str(attempt)})

    # --------------------------------------------------------------- upstream
    def _forward(self, h, protocol, channel, wire, body, attempt, handoff):
        """Relay one attempt. Return None on success, else (status, reason, seconds)."""
        payload = dict(body)
        payload["model"] = wire
        upstream_path = "/v1/chat/completions" if protocol == OPENAI else "/v1/messages"
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
        stream = bool(payload.get("stream"))
        now = time.time()
        try:
            resp = urlopen(req, timeout=channel.timeout)
        except HTTPError as exc:
            raw = exc.read()
            quota, seconds, reason = parse_cooldown(
                exc.code, exc.headers, raw,
                channel.cooldown_minutes, self.config.defaults.max_cooldown_hours, now,
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
                    return self._relay(h, resp, protocol, channel, wire, attempt, handoff, stream)
            except (BrokenPipeError, ConnectionResetError):
                with self.store.lock:
                    self.store.states[channel.name].mark_success(0, time.time())
                return None
            except Exception as exc:
                with self.store.lock:
                    self.store.states[channel.name].last_error = repr(exc)[:300]
                return None

    def _relay(self, h, resp, protocol, channel, wire, attempt, handoff, stream):
        extra = {
            "X-Routed-Via": f"{channel.name}/{wire}",
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
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    h.wfile.write(chunk)
                    h.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            with self.store.lock:
                self.store.states[channel.name].mark_success(0, time.time())
            self.record_usage(channel.name, True, 0)
            return None

        data = resp.read()
        tokens = _usage(data, protocol)
        with self.store.lock:
            self.store.states[channel.name].mark_success(tokens, time.time())
        self.record_usage(channel.name, True, tokens)
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
    )
    app.start_background()
    httpd.app = app  # type: ignore[attr-defined]
    return httpd
