"""Browser UI and configuration API (no frontend build, no external assets)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import facts as facts_mod
from . import providers as providers_mod
from . import dsh as dsh_mod

STATIC_DIR = Path(__file__).parent / "static"
STATIC_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
# Bundled usage manual, rendered by the console in a floating panel. It lives
# in the repository's docs/ during development and is copied to /opt/planrouter
# by the Docker image; when neither is present (a bare `pip install`) the UI
# degrades to a hint pointing at the vendor's own documentation.
_GUIDE_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "docs" / "api-key-guide.md",  # checkout
    Path("/opt/planrouter/docs/api-key-guide.md"),                      # container
    Path(__file__).parent / "docs" / "api-key-guide.md",                 # wheel
)


def _guide_path() -> Path:
    for candidate in _GUIDE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return _GUIDE_CANDIDATES[0]


GUIDE_PATH = _guide_path()
GUIDE_IMG_NAME = re.compile(r"^[A-Za-z0-9._/-]+$")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _send(h, status: int, data: bytes, content_type: str) -> None:
    h.send_response(status)
    h.send_header("Content-Type", content_type)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(data)


_CU_CREDS_FILE = "cu-credentials.json"


def _cu_creds_path(app) -> Path | None:
    """Where the browser agent keeps its per-host logins (same data directory)."""
    return app.config_path.parent / _CU_CREDS_FILE if app.config_path else None


def _cu_creds_load(app) -> dict:
    path = _cu_creds_path(app)
    if not path or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _cu_creds_public(app) -> dict:
    """Never echo a password or a cookie: the console only shows what exists."""
    hosts = []
    for host, entry in sorted(_cu_creds_load(app).items()):
        if not isinstance(entry, dict):
            continue
        hosts.append({
            "host": host,
            "username": str(entry.get("username") or ""),
            "has_password": bool(entry.get("password")),
            "has_cookie": bool(entry.get("cookie")),
        })
    path = _cu_creds_path(app)
    return {"hosts": hosts, "path": str(path) if path else ""}


def _cu_creds_write(app, data: dict) -> Path:
    path = _cu_creds_path(app)
    if path is None:
        raise ValueError("no config path; the data directory is unknown")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _cu_creds_update(app, incoming: dict) -> dict:
    host = re.sub(r"^https?://", "", str(incoming.get("host") or "").strip().lower())
    host = host.split("/")[0].split(":")[0]
    if not host or not re.match(r"^[a-z0-9.-]+$", host):
        raise ValueError("host must look like www.modelscope.cn")
    data = _cu_creds_load(app)
    if incoming.get("remove"):
        data.pop(host, None)
        _cu_creds_write(app, data)
        return {"ok": True, "removed": host}

    cookie = str(incoming.get("cookie") or "").strip()
    if cookie and not re.match(r"^[^=\s]+\s*=", cookie):
        raise ValueError("cookie must look like NAME=VALUE")
    entry = dict(data.get(host) or {})
    # Empty fields keep the stored value: the console renders masked hints and
    # submits only what the user actually typed.
    if str(incoming.get("username") or "").strip():
        entry["username"] = str(incoming["username"]).strip()
    if str(incoming.get("password") or ""):
        entry["password"] = str(incoming["password"])
    if cookie:
        entry["cookie"] = cookie
    data[host] = entry
    _cu_creds_write(app, data)
    return {"ok": True, "host": host}


def _cu_credentials(app, h, method: str, raw: str) -> None:
    if method == "GET":
        _json(h, 200, _cu_creds_public(app))
        return
    incoming = {}
    if raw:
        try:
            incoming = json.loads(raw)
        except ValueError:
            incoming = {}
    if not isinstance(incoming, dict):
        incoming = {}
    try:
        result = _cu_creds_update(app, incoming)
    except ValueError as exc:
        _json(h, 400, {"error": {"message": str(exc)}})
        return
    _json(h, 200, {**result, **_cu_creds_public(app)})


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
        # Extract version for optimistic locking (if provided)
        expected_version = incoming.pop("_version", None)
        try:
            config = app.apply_config(incoming, expected_version=expected_version)
        except ValueError as exc:
            msg = str(exc)
            if "version mismatch" in msg:
                _json(h, 409, {"error": {"message": msg, "code": "VERSION_MISMATCH"}})
            else:
                _json(h, 400, {"error": {"message": msg}})
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

    if path == "/api/facts" and method == "GET":
        facts = facts_mod.snapshot()
        return _json(h, 200, {
            "available": facts.available,
            "coverage": facts.coverage(),
            "free_tiers": list(facts_mod.FREE_TIERS),
        })
    if path == "/api/facts/free" and method == "GET":
        facts = facts_mod.snapshot()
        return _json(h, 200, {"candidates": facts.free_candidates(limit=50)})
    if path.startswith("/api/facts/resolve/") and method == "GET":
        name = unquote(path[len("/api/facts/resolve/"):])
        facts = facts_mod.snapshot()
        key = facts.resolve(name)
        if not key:
            return _json(h, 404, {"error": f"unresolved: {name}"})
        return _json(h, 200, {"query": name, "key": key, "record": facts.record(key)})
    if path == "/api/guide" and method == "GET":
        if GUIDE_PATH.is_file():
            _json(h, 200, {"available": True,
                           "markdown": GUIDE_PATH.read_text(encoding="utf-8")})
        else:
            _json(h, 200, {"available": False, "markdown": ""})
        return True

    if path.startswith("/api/guide/img/") and method == "GET":
        rel = unquote(path[len("/api/guide/img/"):])
        root = GUIDE_PATH.parent.resolve()
        img = (root / rel).resolve() if GUIDE_IMG_NAME.fullmatch(rel) else None
        # Screenshots live under docs/ next to the manual; anything escaping
        # that directory (../) is rejected before touching the disk.
        if img is None or root not in img.parents or not img.is_file():
            _json(h, 404, {"error": {"message": "guide image not found"}})
            return True
        _send(h, 200, img.read_bytes(), CONTENT_TYPES.get(img.suffix, "image/png"))
        return True

    if path == "/api/providers" and method == "GET":
        overlay = providers_mod.overlay_ids(
            app.config_path.parent if getattr(app, "config_path", None) else None)
        providers = []
        for entry in app.providers:
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            item["origin"] = ("overlay"
                              if str(item.get("id") or "") in overlay
                              else "builtin")
            providers.append(item)
        _json(h, 200, {"providers": providers})
        return True

    if path == "/api/providers/maintain" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        if not isinstance(incoming, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return True
        vendor_id = incoming.get("id")
        if vendor_id is not None:
            if not isinstance(vendor_id, str) or not vendor_id.strip():
                _json(h, 400, {"error": {"message": "id 必须是非空字符串"}})
                return True
            vendor_id = vendor_id.strip()
        _json(h, 200, app.maintain_providers(vendor_id))
        return True

    if path == "/api/providers" and method == "POST":
        try:
            incoming = json.loads(raw)
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        try:
            provider = app.save_provider(incoming)
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        except OSError as exc:
            _json(h, 500, {"error": {"message": f"cannot write providers: {exc}"}})
            return True
        _json(h, 200, {"ok": True, "provider": provider})
        return True

    if path.startswith("/api/providers/") and method == "DELETE":
        provider_id = unquote(path[len("/api/providers/"):])
        if not provider_id or "/" in provider_id:
            _json(h, 400, {"error": {"message": "invalid provider id"}})
            return True
        try:
            providers = app.delete_provider(provider_id)
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        except OSError as exc:
            _json(h, 500, {"error": {"message": f"cannot write providers: {exc}"}})
            return True
        _json(h, 200, {"ok": True, "providers": providers})
        return True

    if (path == "/api/report" or path.startswith("/api/report/")) and method == "GET":
        parts = path.split("/")
        month = parts[3] if len(parts) > 3 and parts[3] else None
        try:
            _json(h, 200, app.report_payload(month))
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
        return True

    # /api/usage/users must match before the generic /api/usage prefix below.
    if method == "GET" and (path == "/api/usage/users" or path.startswith("/api/usage/users/")):
        try:
            days = int(path.split("/")[4]) if len(path.split("/")) > 4 else 30
        except ValueError:
            days = 30
        if app.usage:
            _json(h, 200, app.usage.user_stats(days))
        else:
            _json(h, 200, {"range": days, "users": [], "days": [],
                           "note": "usage recorder not enabled"})
        return True

    if path.startswith("/api/usage") and method == "GET":
        parts = path.split("/")
        try:
            days = int(parts[3]) if len(parts) > 3 and parts[3] else 30
        except ValueError:
            days = 30
        if app.usage:
            payload = app.usage.series(days)
            stats = app.usage.model_stats(days)
            by_name = {c.name: c for c in app.config.channels}
            for row in stats.get("models") or []:
                channel = by_name.get(row.get("channel"))
                tier = ""
                if channel is not None:
                    tier = ((channel.tiers or {}).get(row.get("model"))
                            or channel.tier or "")
                row["tier"] = tier
            payload["models"] = stats.get("models") or []
            _json(h, 200, payload)
        else:
            _json(h, 200, {"range": days, "days": [], "channels": [], "models": [],
                           "note": "usage recorder not enabled"})
        return True

    if path == "/api/keys" and method == "GET":
        rows = app.keys.list() if app.keys else []
        # Reuse the per-channel usage shape for a compact recent-activity hint.
        _json(h, 200, {"keys": rows})
        return True

    if path == "/api/keys" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
            record = app.keys.create(str(incoming.get("name") or ""))
        except (ValueError, AttributeError) as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
        except OSError as exc:
            _json(h, 500, {"error": {"message": f"cannot save keys file: {exc}"}})
        else:
            _json(h, 200, record)
        return True

    if path == "/api/keys/revoke" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
            done = app.keys.revoke(str(incoming.get("key") or "")) if app.keys else False
        except (ValueError, OSError) as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
        else:
            _json(h, 200, {"revoked": bool(done)})
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
            result = app.probe_channel_models(incoming.get("channel"),
                                              allow_ark_quota=True)
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        _json(h, 200, result)
        return True

    if path == "/api/channels/duplicate" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        name = incoming.get("name") if isinstance(incoming, dict) else None
        if not isinstance(name, str) or not name.strip():
            _json(h, 400, {"error": {"message": "name 必须是非空字符串"}})
            return True
        try:
            _json(h, 200, app.duplicate_channel(name.strip()))
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
        return True

    if path == "/api/reload" and method == "POST":
        try:
            config = app.reload_from_disk()
        except (ValueError, OSError) as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        _json(h, 200, {"ok": True, "channels": len(config.channels)})
        return True

    if path == "/api/tasks" and method == "GET":
        _json(h, 200, app.tasks_payload())
        return True

    if path == "/api/recommend" and method == "GET":
        query = urlsplit(h.path or "").query
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        purpose = params.get("purpose", "agent")
        try:
            limit = int(params.get("limit", "20"))
        except ValueError:
            limit = 20
        _json(h, 200, app.recommend_models(purpose, limit))
        return True

    if path == "/api/admin/key" and method == "POST":
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            key = app.rotate_admin_key(str(body.get("key") or ""))
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        _json(h, 200, {"admin_key": key})
        return True

    if path == "/api/maintenance" and method == "GET":
        _json(h, 200, app.maintenance_payload())
        return True

    if (path == "/api/maintenance/logs"
            or path.startswith("/api/maintenance/logs/")) and method == "GET":
        parts = path.split("/")
        try:
            limit = int(parts[4]) if len(parts) > 4 and parts[4] else 50
        except ValueError:
            limit = 50
        _json(h, 200, app.maintenance_log_payload(limit))
        return True

    if path == "/api/maintenance/run" and method == "POST":
        _json(h, 200, app.run_maintenance("manual"))
        return True

    if (path == "/api/freealign/logs"
            or path.startswith("/api/freealign/logs/")) and method == "GET":
        parts = path.split("/")
        try:
            limit = int(parts[4]) if len(parts) > 4 and parts[4] else 50
        except ValueError:
            limit = 50
        _json(h, 200, app.free_align_log_payload(limit))
        return True

    if path == "/api/backups" and method == "GET":
        _json(h, 200, app.backup_list_payload())
        return True

    if path == "/api/freealign/run" and method == "POST":
        # Re-align free-tier channels with their vendor free-quota tools now.
        _json(h, 200, app.run_free_align("manual"))
        return True

    if path == "/api/maintenance/verify-free" and method == "POST":
        # Ping every free-channel mapping; unusable ids get an expiring label
        # that routing then skips, so dead resources stop consuming quota.
        # An optional {"channels": [...]} body limits the sweep to those free
        # channels instead of the whole free pool.
        channels = None
        if raw.strip():
            try:
                body = json.loads(raw)
            except ValueError:
                _json(h, 400, {"error": {"message": "invalid JSON body"}})
                return True
            if isinstance(body, dict) and body.get("channels"):
                channels = [str(name) for name in body["channels"]]
        _json(h, 200, app.verify_free_models(channels))
        return True

    if path == "/api/integrations/dsh" and method == "GET":
        # The DSH entry needs its state on the dashboard: whether the client
        # shares this host, where its profile points, and if we already have
        # a live tokenised URL for it.
        _json(h, 200, app.dsh_status())
        return True

    if path == "/api/integrations/dsh/web" and method == "POST":
        # Spawn the planrouter-web profile and return the URL it prints at
        # startup. The browser tab is opened by the console, not by dsh, so
        # the URL belongs to the operator's machine even when the gateway is
        # reached remotely.
        try:
            incoming = json.loads(raw) if raw else {}
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        if not isinstance(incoming, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return True
        port = incoming.get("port")
        if port is not None:
            try:
                port = int(port)
            except (TypeError, ValueError):
                _json(h, 400, {"error": {"message": "port must be a number"}})
                return True
            if not 1 <= port <= 65535:
                _json(h, 400, {"error": {"message": "port out of range"}})
                return True
        try:
            result = app.start_dsh_web(port)
        except dsh_mod.DshError as exc:
            _json(h, 503, {"ok": False, "error": str(exc)})
            return True
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        _json(h, 200 if result.get("ok") else 503,
              result if result.get("ok")
              else {"ok": False, "log": result.get("log", []),
                    "error": {"message": result.get("error") or "dsh 启动失败"}})
        return True

    if path == "/api/maintenance/test-models" and method == "POST":
        # Quiz candidate maintenance models now and rebuild the verified queue.
        _json(h, 200, app.probe_maintenance_models())
        return True

    if path == "/api/maintenance/test-model" and method == "POST":
        # Manual quiz for one (channel, model) from the free-model overview.
        try:
            incoming = json.loads(raw) if raw else {}
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        try:
            _json(h, 200, app.test_maintenance_model(incoming))
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
        return True

    if path == "/api/maintenance/retest-failed" and method == "POST":
        # Quiz every model whose last quiz failed, in one batch.
        _json(h, 200, app.retest_failed_models())
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

    if path == "/api/score/reset" and method == "POST":
        incoming = {}
        if raw:
            try:
                incoming = json.loads(raw)
            except ValueError:
                incoming = {}
        if not isinstance(incoming, dict):
            incoming = {}
        channel = incoming.get("channel")
        wire = incoming.get("wire")
        if channel is not None and (not isinstance(channel, str) or not channel):
            _json(h, 400, {"error": {"message": "invalid channel"}})
            return True
        if wire is not None and (not isinstance(wire, str) or not wire):
            _json(h, 400, {"error": {"message": "invalid wire"}})
            return True
        removed = app.reset_scores(channel, wire)
        _json(h, 200, {"ok": True, "removed": removed})
        return True

    if path == "/api/cu-credentials" and method in ("GET", "POST"):
        _cu_credentials(app, h, method, raw)
        return True

    _json(h, 404, {"error": {"message": "not found", "path": path}})
    return True
