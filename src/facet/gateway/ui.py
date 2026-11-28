"""Browser UI and configuration API (no frontend build, no external assets)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import facts as facts_mod
from . import providers as providers_mod
from . import dsh as dsh_mod
from . import keys as keys_mod
from . import benchmarks as benchmarks_mod

STATIC_DIR = Path(__file__).parent / "static"
STATIC_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
# Bundled usage manual, rendered by the console in a floating panel. It lives
# in the repository's docs/ during development and is copied to /opt/facet/gateway
# by the Docker image; when neither is present (a bare `pip install`) the UI
# degrades to a hint pointing at the vendor's own documentation.
_GUIDE_CANDIDATES = (
    Path(__file__).resolve().parents[3] / "docs" / "api-key-guide.md",  # checkout
    Path("/opt/facet/gateway/docs/api-key-guide.md"),                      # container
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
    ".ico": "image/x-icon",
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
        if app.config.listen.admin_key:
            msg = "invalid admin key"
        else:
            msg = ("management API locked: listen.admin_key is not set while "
                   "listen.api_key is — set admin_key in router.json and reload")
        _json(h, 401, {"error": {"message": msg}})
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
        # ?channel= 让控制台跨归档回溯某个渠道的失败历史（audit.recent
        # 原生支持该过滤），空值表示不过滤。
        query = dict(p.split("=", 1) for p in urlsplit(h.path or "").query.split("&") if "=" in p)
        channel = unquote(query.get("channel", "")).strip() or None
        _json(h, 200, app.request_log_payload(limit, channel=channel))
        return True

    if path == "/api/diagnostics" and method == "GET":
        _json(h, 200, app.diagnostics_payload())
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
            return _json(h, 404, {"error": {"message": f"unresolved: {name}"}})
        return _json(h, 200, {"query": name, "key": key, "record": facts.record(key)})
    if path == "/api/models/compare" and method == "GET":
        data = benchmarks_mod.payload()
        # 渠道列是活的：随配置启停/换 Key 实时变，不能跟着 payload 一起缓存。
        benchmarks_mod.overlay_channels(data, app.compare_channel_rows())
        _json(h, 200, data)
        return True
    if path == "/api/guide" and method == "GET":
        # ?doc=integration serves the agent integration guide and ?doc=user-guide
        # the task-based user manual; the default is the api-key manual. Names
        # are whitelisted (no path traversal).
        query = urlsplit(h.path or "").query
        doc = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("doc", "")
        if doc == "integration":
            target = GUIDE_PATH.parent / "agent-integration.md"
        elif doc == "user-guide":
            target = GUIDE_PATH.parent / "user-guide.md"
        else:
            target = GUIDE_PATH
        if target.is_file():
            _json(h, 200, {"available": True,
                           "markdown": target.read_text(encoding="utf-8")})
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
        from .mode_meta import mode_meta as mode_meta_of
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
            # Decorate each mode with the normalized classification block
            # (free_kind / cred_kind / requires_checkin / quota_capability).
            # Copied per request so the in-memory registry entries stay clean
            # (maintenance persists those entries verbatim).
            pid = str(item.get("id") or "")
            modes = item.get("modes")
            if isinstance(modes, dict):
                decorated = {}
                for mode_key, mode in modes.items():
                    if isinstance(mode, dict):
                        mode = {**mode, "meta": mode_meta_of(pid, mode)}
                    decorated[mode_key] = mode
                item["modes"] = decorated
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

    # /api/report/members must precede the generic /api/report prefix: its
    # fourth segment is a literal, not a month.
    if path.startswith("/api/report/members") and method == "GET":
        parts = path.split("/")
        month = parts[4] if len(parts) > 4 and parts[4] else None
        try:
            _json(h, 200, app.member_costs(month))
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
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
            _json(h, 200, app.member_usage(days))
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
        _json(h, 200, {"keys": rows,
                       "master": app.config.listen.api_key,
                       "master_enabled": bool(app.config.listen.api_key_enabled)})
        return True

    if path == "/api/master-key" and method == "POST":
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            action = str(body.get("action") or "rotate")
            admin = str(body.get("admin_key") or "")
            had_admin = bool(app.config.listen.admin_key)
            if action == "pause":
                app.set_master_key_enabled(False, admin)
            elif action == "enable":
                app.set_master_key_enabled(True, admin)
            elif action == "rotate":
                app.rotate_master_key(str(body.get("key") or ""), admin)
            else:
                raise ValueError("action must be pause, enable or rotate")
        except (ValueError, OSError) as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
        else:
            resp = {"api_key": app.config.listen.api_key,
                    "enabled": bool(app.config.listen.api_key_enabled)}
            if not had_admin:
                # Keyless console leaving keyless mode: rotate_master_key just
                # seeded an admin_key so the management API is not locked out —
                # hand it back to this browser for the next request.
                resp["admin_key"] = app.config.listen.admin_key
            _json(h, 200, resp)
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

    if path == "/api/keys/limits" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
            if not isinstance(incoming, dict):
                raise ValueError("body must be an object")
            record = app.update_key_limits(str(incoming.get("key") or ""),
                                           incoming.get("limits"))
        except (ValueError, AttributeError, OSError) as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
        else:
            _json(h, 200, record)
        return True

    if path == "/api/keys/rotate" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
            record = app.rotate_member_key(str(incoming.get("key") or ""))
        except (ValueError, AttributeError, OSError) as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
        else:
            _json(h, 200, record)
        return True

    if path in ("/api/keys/enable", "/api/keys/revoke", "/api/keys/delete", "/api/keys/pause") and method == "POST":
        action = path.rsplit("/", 1)[-1]
        try:
            incoming = json.loads(raw) if raw else {}
            target = str(incoming.get("key") or "")
            if not app.keys:
                done = False
            elif action == "enable":
                done = app.keys.enable(target)
            elif action == "delete":
                done = app.keys.delete(target)
            elif action == "pause":
                done = app.keys.pause(target)
            else:
                done = app.keys.revoke(target)
        except (ValueError, OSError) as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
        else:
            _json(h, 200, {action + "d": bool(done)})
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

    if path == "/api/channels/usage" and method == "GET":
        _json(h, 200, app.channels_usage_payload())
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
            payload = app.test_channels(incoming.get("channels"),
                                        incoming.get("model"),
                                        incoming.get("timeout"))
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
            key = app.rotate_admin_key(str(body.get("key") or ""),
                                       str(body.get("old_key") or ""))
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

    if path == "/api/maintenance/refresh-benchmarks" and method == "POST":
        # Fetch public benchmark scores (Vals Terminal-Bench 4.0) now and
        # layer them over the bundled snapshot. A busy sweep answers 200
        # with busy: true — the console api() helper throws on non-2xx, and
        # the busy toast is an expected outcome, not an error.
        _json(h, 200, app.refresh_benchmarks("manual"))
        return True

    if path == "/api/changes" and method == "GET":
        # Change events (newest first) + per-source state, optional ?limit=N.
        query = urlsplit(h.path or "").query
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        try:
            limit = int(params.get("limit", "100"))
        except ValueError:
            limit = 100
        _json(h, 200, app.changes_payload(max(1, min(limit, 500))))
        return True

    if path == "/api/changes/run" and method == "POST":
        # One watch sweep now. A busy sweep answers 200 with busy: true —
        # the console api() helper throws on any non-2xx status, and the
        # busy toast is an expected outcome, not an error.
        report = app.run_change_watch("manual")
        _json(h, 200, report)
        return True

    if path == "/api/changes/read" and method == "POST":
        # {"ids": [...]} flips those events; {"all": true} flips everything
        # currently visible in the console's list.
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        if not isinstance(body, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return True
        if body.get("all"):
            events = app.changes_payload()["events"]
            ids = [event.get("id") for event in events if event.get("status") == "unread"]
        else:
            ids = [str(value) for value in (body.get("ids") or [])]
        _json(h, 200, app.mark_changes_read(ids))
        return True

    if path == "/api/changes/map" and method == "POST":
        # {"id": event, "channel"?: name} — write a new-model event into a
        # channel through the maintenance mapping path.
        try:
            body = json.loads(raw) if raw.strip() else {}
            _json(h, 200, app.map_change_models(body))
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
        return True

    if path == "/api/changes/mute" and method == "POST":
        # {"keys": [...] or "key", "muted": bool} — stop/resume reporting.
        try:
            body = json.loads(raw) if raw.strip() else {}
            if not isinstance(body, dict):
                raise ValueError("body 必须是 JSON 对象")
            keys = body.get("keys", body.get("key"))
            if isinstance(keys, str):
                keys = [keys]
            if not isinstance(keys, list):
                raise ValueError("需要信息源 key")
            _json(h, 200, app.set_changes_muted(
                [str(key) for key in keys], bool(body.get("muted", True))))
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
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
        # Spawn the facet-web profile and return the URL it prints at
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
            _json(h, 503, {"ok": False, "error": {"message": str(exc)}})
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

    if path == "/api/maintenance/ignore" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        try:
            result = app.ignore_maintenance_suggestion(incoming)
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        except OSError as exc:
            _json(h, 500, {"error": {"message": f"cannot write ignore list: {exc}"}})
            return True
        _json(h, 200, result)
        return True

    if path == "/api/backups/restore" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        try:
            result = app.restore_backup((incoming or {}).get("tag"))
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        except OSError as exc:
            _json(h, 500, {"error": {"message": f"cannot write config: {exc}"}})
            return True
        _json(h, 200, result)
        return True

    if path == "/api/migration/export" and method == "GET":
        # Whole-machine migration bundle (config + keys + subscriptions +
        # usage) as base64; the console turns it into a zip download.
        try:
            _json(h, 200, app.migration_export_payload())
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
        return True

    if path == "/api/migration/import" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        try:
            result = app.migration_import(incoming)
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        except OSError as exc:
            _json(h, 500, {"error": {"message": f"cannot write config dir: {exc}"}})
            return True
        _json(h, 200, result)
        return True

    if path.startswith("/api/channels/") and path.endswith("/reset") and method == "POST":
        name = unquote(path[len("/api/channels/"):-len("/reset")])
        if not name or "/" in name:
            _json(h, 400, {"error": {"message": "invalid channel name"}})
            return True
        ok = app.reset_channel(name)
        if ok:
            _json(h, 200, {"ok": True, "channel": name})
        else:
            _json(h, 404, {"ok": False, "channel": name,
                           "error": {"message": f"渠道不存在：{name}"}})
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

    if path == "/api/setup/verify-call" and method == "POST":
        try:
            incoming = json.loads(raw) if raw else {}
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        if not isinstance(incoming, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return True
        try:
            payload = app.verify_setup_call(model=incoming.get("model"),
                                            key=incoming.get("key"))
        except ValueError as exc:
            _json(h, 400, {"error": {"message": str(exc)}})
            return True
        except OSError as exc:
            _json(h, 500, {"error": {"message": f"verify failed: {exc}"}})
            return True
        _json(h, 200, payload)
        return True

    if path == "/api/setup/wizard" and method == "GET":
        # The console asks whether THIS server considers the wizard done before
        # auto-reopening it: the browser's localStorage flag alone would
        # suppress the wizard forever after a reinstall or directory move
        # (fresh config, stale browser flag). The wizard POST writes the file
        # directly without touching app.raw, so read the file here too.
        router_path = Path(app.config_path or "router.json")
        wizard_done = False
        try:
            existing = json.loads(router_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                wizard_done = (existing.get("setup") or {}).get("wizard_done") is True
        except (OSError, ValueError):
            wizard_done = False
        _json(h, 200, {"ok": True, "wizard_done": wizard_done})
        return True

    if path == "/api/setup/wizard" and method == "POST":
        incoming = {}
        if raw:
            try:
                incoming = json.loads(raw)
            except ValueError:
                incoming = {}
        if not isinstance(incoming, dict):
            incoming = {}
        done = incoming.get("done")
        router_path = Path(app.config_path or "router.json")
        router_path.parent.mkdir(parents=True, exist_ok=True)
        if router_path.exists():
            # A corrupted config must abort the save, never be overwritten
            # from this half-known state (the request only carries wizard
            # state — rewriting router.json with it would drop the user's
            # channels and routes).
            try:
                existing = json.loads(router_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = None
            if not isinstance(existing, dict):
                _json(h, 500, {"ok": False, "error": {
                    "message": "配置文件损坏：无法保存向导状态，请先在「配置」页修复 router.json"}})
                return True
        else:
            existing = {}
        # Ensure setup dict exists and mark wizard_done
        existing.setdefault("setup", {})
        if done is True:
            existing["setup"]["wizard_done"] = True
        try:
            router_path.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            _json(h, 500, {"ok": False,
                           "error": {"message": f"无法写入配置文件：{exc}"}})
            return True
        # Keep the in-memory raw in step with the file: apply_config
        # persists from memory, so a stale wizard_done=False there would
        # silently undo this flag on the next console save and re-open
        # the wizard.
        if done is True:
            app.raw.setdefault("setup", {})["wizard_done"] = True
        _json(h, 200, {"ok": True, "wizard_done": existing["setup"].get("wizard_done")})
        return True

    if path == "/api/center/sync" and method == "POST":
        # 手动触发一次同步（也用于消息页的"立即同步"按钮）。
        if getattr(app, "center", None):
            try:
                _json(h, 200, app.center.sync_now())
            except Exception as exc:
                _json(h, 500, {"error": {"message": str(exc)}})
        else:
            _json(h, 200, {"ok": False, "note": "center 客户端未启用（无配置文件）"})
        return True

    if path == "/api/center/settings" and method == "POST":
        # 中心设置与隐私开关（设计 5.1）：坏值回退默认并回报 errors。
        if getattr(app, "center", None):
            try:
                incoming = json.loads(raw) if raw.strip() else {}
            except ValueError:
                _json(h, 400, {"error": {"message": "invalid JSON body"}})
                return True
            if not isinstance(incoming, dict):
                _json(h, 400, {"error": {"message": "body must be an object"}})
                return True
            settings, errors = app.center.save_settings(incoming)
            if errors:
                _json(h, 200, {"settings": settings, "errors": errors})
            else:
                _json(h, 200, {"settings": settings})
        else:
            _json(h, 200, {"settings": {}, "errors": ["center 客户端未启用"]})
        return True

    if path.startswith("/api/center/prefill/") and method == "GET":
        # 一键预填：只接受验签过的注册表里的厂家（伪造拒绝，api_key 永空）。
        vendor_id = unquote(path[len("/api/center/prefill/"):])
        if not vendor_id or "/" in vendor_id:
            _json(h, 400, {"error": {"message": "invalid vendor id"}})
            return True
        if getattr(app, "center", None):
            try:
                _json(h, 200, app.center.prefill(vendor_id))
            except ValueError as exc:
                _json(h, 404, {"error": {"message": str(exc)}})
        else:
            _json(h, 200, {"error": {"message": "center 客户端未启用"}})
        return True

    if path.startswith("/api/center/prefill/") and method == "POST":
        # 用户确认后的预填落盘。body {"vendor": id}。
        vendor_id = unquote(path[len("/api/center/prefill/"):])
        if not vendor_id or "/" in vendor_id:
            _json(h, 400, {"error": {"message": "invalid vendor id"}})
            return True
        try:
            incoming = json.loads(raw) if raw.strip() else {}
        except ValueError:
            _json(h, 400, {"error": {"message": "invalid JSON body"}})
            return True
        if not isinstance(incoming, dict):
            _json(h, 400, {"error": {"message": "body must be an object"}})
            return True
        if getattr(app, "center", None):
            try:
                _json(h, 200, app.center.confirm_prefill(incoming.get("vendor") or vendor_id))
            except (ValueError, RuntimeError) as exc:
                _json(h, 400, {"error": {"message": str(exc)}})
            except OSError as exc:
                _json(h, 500, {"error": {"message": f"cannot write config: {exc}"}})
        else:
            _json(h, 200, {"error": {"message": "center 客户端未启用"}})
        return True

    if path == "/api/messages" and method == "GET":
        # 消息中心（设计 3.7.1）：未读/归档/查找/同步状态/空状态。
        if getattr(app, "center", None):
            _json(h, 200, app.center.ui_payload())
        else:
            _json(h, 200, {"enabled": False, "messages": [], "sync": {},
                           "note": "center 客户端未启用"})
        return True

    if path == "/api/messages/read" and method == "POST":
        # 未读 → 已读归档。
        if getattr(app, "center", None):
            try:
                _json(h, 200, {"read": app.center.mark_all_read()})
            except Exception as exc:
                _json(h, 500, {"error": {"message": str(exc)}})
        else:
            _json(h, 200, {"read": 0})
        return True

    if path == "/api/messages/dismiss" and method == "POST":
        # {"id": ...} 忽略某条（vendor_new 同时加入忽略名单）。
        if getattr(app, "center", None):
            try:
                body = json.loads(raw) if raw.strip() else {}
            except ValueError:
                _json(h, 400, {"error": {"message": "invalid JSON body"}})
                return True
            item_id = str((body or {}).get("id") or "")
            _json(h, 200, {"ok": app.center.dismiss_item(item_id)})
        else:
            _json(h, 200, {"ok": False})
        return True

    if path == "/api/messages/apply" and method == "POST":
        # {"id": ...} 用户确认后的合并动作（vendor_new / preset_modified）。
        if getattr(app, "center", None):
            try:
                body = json.loads(raw) if raw.strip() else {}
            except ValueError:
                _json(h, 400, {"error": {"message": "invalid JSON body"}})
                return True
            item_id = str((body or {}).get("id") or "")
            try:
                _json(h, 200, app.center.apply_item(item_id))
            except (ValueError, RuntimeError) as exc:
                _json(h, 400, {"error": {"message": str(exc)}})
            except OSError as exc:
                _json(h, 500, {"error": {"message": f"cannot write config: {exc}"}})
        else:
            _json(h, 200, {"error": {"message": "center 客户端未启用"}})
        return True

    _json(h, 404, {"error": {"message": "not found", "path": path}})
    return True
