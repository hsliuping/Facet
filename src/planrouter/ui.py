"""Browser UI and configuration API (no frontend build, no external assets)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

STATIC_DIR = Path(__file__).parent / "static"
STATIC_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _send(h, status: int, data: bytes, content_type: str) -> None:
    h.send_response(status)
    h.send_header("Content-Type", content_type)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(data)


def _json(h, status: int, payload) -> None:
    _send(h, status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
          "application/json; charset=utf-8")


def _static(name: str) -> tuple[bytes, str] | None:
    if not STATIC_NAME.fullmatch(name):
        return None
    path = STATIC_DIR / name
    if not path.is_file():
        return None
    return path.read_bytes(), CONTENT_TYPES.get(path.suffix, "application/octet-stream")


def _same_site(h) -> bool:
    """Reject browser cross-site writes to the management API."""
    site = (h.headers.get("Sec-Fetch-Site") or "").lower()
    if site and site not in ("same-origin", "none"):
        return False
    origin = h.headers.get("Origin")
    if not origin:
        return True
    try:
        netloc = urlsplit(origin).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc == (h.headers.get("Host") or "")


def handle(app, h, method: str, path: str, raw: bytes) -> bool:
    """Return True when this request belongs to the UI/API surface."""
    if method == "GET" and path in ("/", "/ui", "/ui/"):
        asset = _static("index.html")
        if asset:
            _send(h, 200, *asset)
        else:
            _json(h, 500, {"error": {"message": "UI assets missing"}})
        return True

    if method == "GET" and path.startswith("/static/"):
        asset = _static(path[len("/static/"):])
        if asset:
            _send(h, 200, *asset)
        else:
            _json(h, 404, {"error": {"message": "asset not found"}})
        return True

    if not path.startswith("/api/"):
        return False

    if not app._authorized_admin(h):
        _json(h, 401, {"error": {"message": "invalid admin key"}})
        return True

    if method == "POST" and not _same_site(h):
        _json(h, 403, {"error": {"message": "cross-site writes are not allowed"}})
        return True

    if (path == "/api/requests" or path.startswith("/api/requests/")) and method == "GET":
        parts = path.split("/")
        try:
            limit = int(parts[3]) if len(parts) > 3 and parts[3] else 50
        except ValueError:
            limit = 50
        _json(h, 200, app.request_log_payload(limit))
        return True

    if path == "/api/config" and method == "GET":
        _json(h, 200, app.public_config())
        return True

    if path == "/api/config" and method == "POST":
        try:
            incoming = json.loads(raw)
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        if not isinstance(incoming, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return True
        try:
            config = app.apply_config(incoming)
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        except OSError as exc:
            _json(h, 500, {"error": {"message": f"cannot write config: {exc}"}})
            return True
        _json(h, 200, {
            "ok": True,
            "channels": len(config.channels),
            "routes": sorted(config.routes),
        })
        return True

    if path == "/api/state" and method == "GET":
        _json(h, 200, app.ui_state())
        return True

    if (path == "/api/report" or path.startswith("/api/report/")) and method == "GET":
        parts = path.split("/")
        month = parts[3] if len(parts) > 3 and parts[3] else None
        try:
            _json(h, 200, app.report_payload(month))
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
        return True

    if path.startswith("/api/usage") and method == "GET":
        parts = path.split("/")
        try:
            days = int(parts[3]) if len(parts) > 3 and parts[3] else 30
        except ValueError:
            days = 30
        if app.usage:
            _json(h, 200, app.usage.series(days))
        else:
            _json(h, 200, {"range": days, "days": [], "channels": [],
                           "note": "usage recorder not enabled"})
        return True

    if path == "/api/notifications" and method == "GET":
        if app.notifier:
            _json(h, 200, app.notifier.status())
        else:
            _json(h, 200, {"enabled": False, "settings": {}, "pending": [], "history": []})
        return True

    if path == "/api/notifications" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
            if not isinstance(incoming, dict):
                raise ValueError("body must be an object")
            if "settings" in incoming:
                app.save_subscription_settings(incoming["settings"])
            if incoming.get("test"):
                if not app.notifier:
                    raise ValueError("通知服务未启用")
                app.notifier.send_test()
        except Exception as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        _json(h, 200, app.notifier.status() if app.notifier else {"ok": True})
        return True

    if path == "/api/notifications/check" and method == "POST":
        if not app.notifier:
            _json(h, 400, {"error": {"message": "通知服务未启用"}})
            return True
        _json(h, 200, {"ok": True, **app.notifier.check()})
        return True

    if path == "/api/subscriptions" and method == "GET":
        _json(h, 200, app.subscriptions_payload())
        return True

    if path == "/api/subscriptions" and method == "POST":
        try:
            incoming = json.loads(raw)
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        try:
            app.save_subscriptions(incoming)
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        except OSError as exc:
            _json(h, 500, {"error": {"message": f"cannot write subscriptions: {exc}"}})
            return True
        _json(h, 200, app.subscriptions_payload())
        return True

    if path.startswith("/api/subscriptions/") and path.endswith("/renew") and method == "POST":
        subscription_id = unquote(path[len("/api/subscriptions/"):-len("/renew")])
        if not subscription_id or "/" in subscription_id:
            _json(h, 400, {"error": {"message": "invalid subscription id"}})
            return True
        try:
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("body must be an object")
            record = app.renew_subscription(subscription_id, payload)
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        except OSError as exc:
            _json(h, 500, {"error": {"message": f"cannot write subscriptions: {exc}"}})
            return True
        _json(h, 200, {"ok": True, "subscription": record})
        return True

    if path == "/api/test/channels" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        if not isinstance(incoming, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return True
        try:
            payload = app.test_channels(incoming.get("channels"), incoming.get("model"))
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        _json(h, 200, payload)
        return True

    if path == "/api/probe/models" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        if not isinstance(incoming, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return True
        try:
            result = app.probe_channel_models(incoming.get("channel"))
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        _json(h, 200, result)
        return True

    if path == "/api/reload" and method == "POST":
        try:
            config = app.reload_from_disk()
        except (ValueError, OSError) as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        _json(h, 200, {"ok": True, "channels": len(config.channels)})
        return True

    if path == "/api/maintenance" and method == "GET":
        _json(h, 200, app.maintenance_payload())
        return True

    if path == "/api/maintenance/run" and method == "POST":
        _json(h, 200, app.run_maintenance("manual"))
        return True

    if path == "/api/maintenance/apply" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        try:
            result = app.apply_maintenance_suggestion(incoming)
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        except OSError as exc:
            _json(h, 500, {"error": {"message": f"cannot write config: {exc}"}})
            return True
        _json(h, 200, result)
        return True

    if path.startswith("/api/channels/") and path.endswith("/reset") and method == "POST":
        name = unquote(path[len("/api/channels/"):-len("/reset")])
        if not name or "/" in name:
            _json(h, 400, {"error": {"message": "invalid channel name"}})
            return True
        ok = app.reset_channel(name)
        _json(h, 200 if ok else 404, {"ok": ok, "channel": name})
        return True

    _json(h, 404, {"error": {"message": "not found", "path": path}})
    return True
