"""Source watch: observe upstream info sources and record their changes.

Three layers of sources are watched, with no new configuration surface —
everything is already configured for catalog refresh and maintenance:

- the vendor registry (builtin providers.json + user overlay): every mode's
  ``docs_url`` and ``models_url``,
- every enabled channel's own models endpoint (``base_url`` + models path,
  with the channel's key) — only while no catalog sweep runs (see
  collect_sources): refresh_all_catalogs already fetches exactly this
  endpoint and reports its model-set diff,
- every subscription's plan-document URL (``models_url``).

Each sweep fetches every source concurrently, extracts the model set (docs
pages through the deterministic list-line extractor, JSON endpoints through
the payload parser maintenance uses) and compares it with the previous
snapshot in ``change-snapshots.json``. Differences become change events
appended to ``change-events.jsonl``; the console's self-maintenance page
lists them. The watch only observes: nothing is auto-applied, nothing is
written back to config or registry, and no LLM is involved.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

from . import maintenance as maint
from .core import ANTHROPIC, OPENAI, open_with_proxy
from .probe import models_path_for

# One source fetch is a cheap GET; docs CDNs can be slow, so stay inside the
# same 10s budget maintenance uses. Workers are bounded: a sweep with dozens
# of sources must not open one connection per source, but a dozen concurrent
# GETs keep a full sweep (manual button) inside ~2 minutes.
FETCH_TIMEOUT = 10.0
FETCH_WORKERS = 12
# The console renders this many events per load; the JSONL keeps more
# (append_jsonl trims at 2 MiB to the same cap as the other report logs).
EVENTS_VIEW_LIMIT = 100
EXCERPT_MAX = 300
# Dedup identity keeps this many recent event keys per source, so a sweep
# that fired added+removed at once does not re-fire the earlier event on a
# later flip-flop (only the newest key would survive otherwise).
EVENT_KEY_KEEP = 8
# Read markers outlive their events once the JSONL trims; this keeps the
# snapshots file bounded regardless.
READ_MARKS_KEEP = 2000


def _iso_utc(now: float | None = None) -> str:
    """ISO-8601 UTC with an explicit Z, the timestamp format every file the
    watch writes uses. The console converts event timestamps to local time
    through the Z marker; a bare local time next to it would make the files
    self-inconsistent."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + "Z"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------- sources
def collect_sources(app) -> list[dict]:
    """Every URL the watch should observe, with how to fetch it.

    Sources are dicts with a stable ``key`` (persists across restarts as the
    snapshot file's row id), a ``kind`` ("docs" pages are hashed for
    doc-updates, "models" JSON endpoints are parsed), the fetch credentials
    borrowed from the owning channel when one exists, and a human ``label``
    for the console. Deduped by key and by URL: a subscription's plan doc
    that is already watched as a registry mode's docs_url is fetched once.
    """
    sources: dict[str, dict] = {}

    def add(key: str, kind: str, origin: str, label: str, url: str,
            api_key: str = "", proxy: str = "", protocol: str = OPENAI) -> None:
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")) or key in sources:
            return
        if any(s["url"] == url for s in sources.values()):
            return
        sources[key] = {
            "key": key, "kind": kind, "origin": origin, "label": label,
            "url": url, "api_key": api_key or "", "proxy": proxy or "",
            "protocol": protocol if protocol in (OPENAI, ANTHROPIC) else OPENAI,
        }

    # Layer 1: vendor registry modes (builtin providers.json + overlay).
    for provider in getattr(app, "providers", None) or []:
        if not isinstance(provider, dict):
            continue
        pid = str(provider.get("id") or "")
        pname = str(provider.get("name") or pid or "—")
        for mode_key, mode in (provider.get("modes") or {}).items():
            if not isinstance(mode, dict):
                continue
            mode_label = str(mode.get("label") or mode_key)
            label = f"{pname} · {mode_label}"
            docs_url = str(mode.get("docs_url") or "").strip()
            models_url = str(mode.get("models_url") or "").strip()
            channel = maint._channel_for_provider(app, pid,
                                                  str(mode.get("base_url") or ""))
            api_key = channel.api_key if channel else ""
            proxy = (channel.proxy if channel and channel.proxy
                     else (app.docs_proxy() if hasattr(app, "docs_proxy") else ""))
            if docs_url:
                add(f"provider:{pid}:{mode_key}:docs", "docs", "provider",
                    label, docs_url, proxy=proxy)
            if models_url:
                add(f"provider:{pid}:{mode_key}:models", "models", "provider",
                    label, models_url, api_key=api_key, proxy=proxy)

    # Layer 2: enabled channels' own models endpoints. Skipped while a
    # catalog sweep is active (catalog_refresh_hours, or maintenance_hours
    # because run_maintenance sweeps catalogs too): refresh_all_catalogs
    # fetches exactly these endpoints and records the model-set diff per
    # channel, so a watch fetch would double the traffic and double-report
    # every model-set change. With both sweeps off the watch is the only
    # observer and keeps covering the channels.
    defaults = getattr(getattr(app, "config", None), "defaults", None)
    catalog_covered = bool(getattr(defaults, "catalog_refresh_hours", 0)
                           or getattr(defaults, "maintenance_hours", 0))
    channels = () if catalog_covered else (
        getattr(app, "config", None) and app.config.channels or [])
    for channel in channels:
        if not getattr(channel, "enabled", False) or not channel.base_url:
            continue
        url = channel.base_url.rstrip("/") + models_path_for(
            channel.base_url, channel.upstream_path)
        add(f"channel:{channel.name}", "models", "channel",
            f"渠道 {channel.name}", url, api_key=channel.api_key or "",
            proxy=channel.proxy, protocol=channel.protocol)

    # Layer 3: subscription plan documents.
    for record in getattr(app, "subscriptions", None) or []:
        if not isinstance(record, dict):
            continue
        url = str(record.get("models_url") or "").strip()
        if not url:
            continue
        label_fn = getattr(app, "subscription_label", None)
        label = (label_fn(record) if callable(label_fn) else "") \
            or str(record.get("plan") or "—")
        add(f"subscription:{record.get('id') or record.get('plan') or url}",
            "docs", "subscription", f"套餐 {label}", url,
            proxy=app.docs_proxy() if hasattr(app, "docs_proxy") else "")

    return list(sources.values())


# -------------------------------------------------------------- snapshots
def snapshot_source(source: dict) -> dict:
    """Fetch one source and reduce it to a comparable snapshot.

    Never raises: a failed fetch returns a snapshot carrying the error so
    the sweep can record it and keep the previous one. The model set is the
    comparable payload; ``sha256`` covers the raw body so unchanged pages
    are skipped without re-running the extractor.
    """
    url = source["url"]
    try:
        if source["kind"] == "docs":
            raw = maint.fetch_plan_doc(url, proxy=source["proxy"])
            text = maint.html_to_text(raw)
            models = maint.extract_plan_models(text)
            head = text[:EXCERPT_MAX]
        else:
            api_key = source.get("api_key") or ""
            headers = {"User-Agent": "planrouter-watch/1.0"}
            if api_key:
                if source.get("protocol") == ANTHROPIC:
                    headers["x-api-key"] = api_key
                    headers["anthropic-version"] = "2023-06-01"
                else:
                    headers["Authorization"] = f"Bearer {api_key}"
            req = Request(url, headers=headers)
            with open_with_proxy(req, FETCH_TIMEOUT, source["proxy"]) as resp:
                raw = resp.read(2_000_000).decode("utf-8", "replace")
            models = maint.parse_models_payload(raw)
            head = raw[:EXCERPT_MAX]
            # Bailian-style payloads advertise a larger total and page via
            # page_no/page_size; follow the pages exactly like the registry
            # refresh does, so the watched set matches the authoritative
            # catalog instead of under-reporting the first page forever.
            total = maint._pagination_total(raw)
            if total > len(models):
                try:
                    merged = set(models)
                    for page_no in range(2, 11):
                        req = Request(maint._page_url(url, page_no),
                                      headers=headers)
                        with open_with_proxy(req, FETCH_TIMEOUT,
                                             source["proxy"]) as resp:
                            page_raw = resp.read(2_000_000).decode(
                                "utf-8", "replace")
                        page_ids = maint.parse_models_payload(page_raw)
                        if not page_ids:
                            break
                        merged.update(page_ids)
                        if len(merged) >= total:
                            break
                    models = sorted(merged)
                except Exception:
                    pass  # keep the first page; the next sweep retries
        return {"url": url, "sha256": _sha256(raw), "models": models,
                "text_head": head, "ok": True, "error": ""}
    except Exception as exc:
        # Boundary: one bad source must never abort the whole sweep, and
        # urllib raises exotic types (http.client.IncompleteRead is not an
        # OSError) that no fixed tuple can fully cover. HTTPError bodies
        # still contribute their excerpt to the error line.
        detail = ""
        if isinstance(exc, HTTPError):
            try:
                detail = exc.read(600).decode("utf-8", "replace")[:200]
            except Exception:
                detail = ""
        return {"url": url, "sha256": "", "models": [], "text_head": "",
                "ok": False,
                "error": f"{exc.__class__.__name__}: {exc or detail}"[:200]}


def diff_snapshot(old: dict | None, new: dict, kind: str) -> list[dict]:
    """Compare one snapshot pair into change events (no LLM, pure rules).

    Model-set differences become ``new_model`` / ``removed_model`` events;
    a body change without a model-set change is a ``doc_updated`` for docs
    pages (JSON payloads churn on vendor metadata, so only docs pages report
    this) or a ``catalog_changed`` when only the duplicate structure moved.
    The first sighting of a source is the baseline and produces nothing.
    A failed sweep with an empty inherited model set is not a baseline
    either: the success after it rebuilds silently instead of flooding
    ``new_model`` events for a catalog that was merely invisible.
    """
    if not old:
        return []
    if old.get("ok") is False and not old.get("models"):
        return []
    old_models = {str(m) for m in old.get("models") or []}
    new_models = {str(m) for m in new.get("models") or []}
    added = sorted(new_models - old_models)
    removed = sorted(old_models - new_models)
    events = []
    if added:
        events.append({"kind": "new_model",
                       "detail": {"added": added, "removed": []}})
    if removed:
        events.append({"kind": "removed_model",
                       "detail": {"added": [], "removed": removed}})
    if not events and old.get("sha256") != new.get("sha256"):
        if kind == "docs":
            events.append({"kind": "doc_updated", "detail": {}})
        elif len(old.get("models") or []) != len(new.get("models") or []):
            events.append({"kind": "catalog_changed", "detail": {}})
    return events


def event_key(event: dict, source_key: str) -> str:
    """Dedup identity: same kind + source + same model sets = same event."""
    detail = event.get("detail") or {}
    payload = "|".join([
        str(event.get("kind") or ""), source_key,
        ",".join(sorted(detail.get("added") or [])),
        ",".join(sorted(detail.get("removed") or [])),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- persistence
def snapshot_path(app) -> Path | None:
    config_path = getattr(app, "config_path", None)
    return config_path.parent / "change-snapshots.json" if config_path else None


def load_snapshots(app) -> dict:
    path = snapshot_path(app)
    if not path:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_snapshots(app, data: dict) -> None:
    """Atomic write (tmp + os.replace); a failed write is reported, not fatal."""
    path = snapshot_path(app)
    if not path:
        return
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        on_error = getattr(app, "_note_write_failure", None)
        if callable(on_error):
            on_error("change_log", OSError(f"change-snapshots.json: {path}"))


def load_events(app, limit: int = EVENTS_VIEW_LIMIT) -> list[dict]:
    """Newest-first tail of change-events.jsonl."""
    path = snapshot_path(app)
    if not path:
        return []
    events_path = path.with_name("change-events.jsonl")
    lines, _ = maint.read_jsonl_tail(events_path, max(limit, 1))
    events = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            events.append(entry)
    return list(reversed(events))


def _prune_marks(marks: dict) -> dict:
    """Read markers older than the JSONL's history are dead weight; prune."""
    if len(marks) > READ_MARKS_KEEP:
        ordered = sorted(marks.items(), key=lambda kv: kv[1], reverse=True)
        return dict(ordered[:READ_MARKS_KEEP])
    return marks


def mark_read(app, ids: list[str], read: bool = True) -> int:
    """Flip the read flag of events by id (in-file; the JSONL is not rewritten).

    A sweep runs for minutes and saves the snapshots file when it finishes,
    so this re-reads the freshest on-disk state and rewrites only the
    ``read_events`` field — the sweep's snapshots and dedup keys are never
    clobbered, and read marks flipped mid-sweep are never rolled back.
    """
    data = load_snapshots(app)
    marks = data.get("read_events") if isinstance(data.get("read_events"), dict) else {}
    changed = 0
    touched: dict[str, str] = {}
    removed: list[str] = []
    for event_id in ids:
        if not isinstance(event_id, str) or not event_id:
            continue
        if read:
            if event_id not in marks:
                changed += 1
            touched[event_id] = _iso_utc()
        elif event_id in marks:
            removed.append(event_id)
            changed += 1
    fresh = load_snapshots(app)
    base = fresh.get("read_events") if isinstance(fresh.get("read_events"), dict) else {}
    if read:
        base.update(touched)
    else:
        for event_id in removed:
            base.pop(event_id, None)
    fresh["read_events"] = _prune_marks(base)
    if "snapshots" not in fresh and isinstance(data.get("snapshots"), dict):
        fresh["snapshots"] = data["snapshots"]
    save_snapshots(app, fresh)
    return changed


def set_muted(app, keys: list[str], muted: bool = True) -> dict:
    """Silence (or re-enable) change reporting for source keys.

    A muted source is still fetched and its snapshot still advances — only
    event creation is skipped — so un-muting later does not dump the whole
    missed delta onto the console at once. Same re-read discipline as
    ``mark_read``: only the ``muted`` field is rewritten, never snapshots.
    """
    wanted = [str(key or "").strip() for key in (keys or [])]
    wanted = [key for key in wanted if key]
    if not wanted:
        raise ValueError("需要至少一个信息源 key")
    data = load_snapshots(app)
    marks = data.get("muted") if isinstance(data.get("muted"), dict) else {}
    changed = 0
    touched: list[str] = []
    removed: list[str] = []
    for key in wanted:
        if muted and key not in marks:
            touched.append(key)
        elif not muted and key in marks:
            removed.append(key)
    fresh = load_snapshots(app)
    base = fresh.get("muted") if isinstance(fresh.get("muted"), dict) else dict(marks)
    for key in touched:
        base[key] = _iso_utc()
        changed += 1
    for key in removed:
        base.pop(key, None)
        changed += 1
    fresh["muted"] = base
    if "snapshots" not in fresh and isinstance(data.get("snapshots"), dict):
        fresh["snapshots"] = data["snapshots"]
    save_snapshots(app, fresh)
    return {"ok": True, "changed": changed, "muted": sorted(base)}


def map_targets(app, source: dict) -> list[str]:
    """Channels a ``new_model`` event from `source` could be mapped into.

    A channel-catalog change names its own channel; a vendor-registry change
    offers the channels bound to that provider (matching billing mode first);
    a subscription plan doc offers the channels the subscription links. The
    backend only suggests — applying is always an explicit console action.
    """
    key = str((source or {}).get("key") or "")
    origin = str((source or {}).get("origin") or "")
    channels = list(getattr(getattr(app, "config", None), "channels", None) or [])
    names = {channel.name for channel in channels}
    if origin == "channel" and key.startswith("channel:"):
        name = key[len("channel:"):]
        return [name] if name in names else []
    if origin == "provider" and key.startswith("provider:"):
        parts = key.split(":")
        pid = parts[1] if len(parts) > 1 else ""
        mode = parts[2] if len(parts) > 2 else ""
        same = [c.name for c in channels
                if c.provider == pid and (c.mode or c.business_line) == mode]
        rest = [c.name for c in channels if c.provider == pid and c.name not in same]
        return same + rest
    if origin == "subscription" and key.startswith("subscription:"):
        sid = key[len("subscription:"):]
        for record in getattr(app, "subscriptions", None) or []:
            if not isinstance(record, dict) or str(record.get("id") or "") != sid:
                continue
            linked = getattr(app, "subscription_channels", None)
            if callable(linked):
                return [name for name in linked(record) if name in names]
            return []
    return []


def notify_events(app, events: list[dict]) -> dict:
    """Push one digest for this sweep's events through the shared webhook."""
    notifier = getattr(app, "notifier", None)
    notify = getattr(notifier, "notify_changes", None)
    if not callable(notify):
        return {"sent": 0, "reason": "unavailable"}
    try:
        return notify(events)
    except Exception as exc:  # a broken webhook must not break the sweep
        return {"sent": 0, "error": str(exc)[:200]}


def notify_status(app) -> dict:
    notifier = getattr(app, "notifier", None)
    delivery = getattr(notifier, "delivery", None)
    if not callable(delivery):
        return {"ok": False, "reason": "unavailable",
                "hint": "通知服务未启动（未指定 notifications 文件路径）。"}
    try:
        return delivery()
    except Exception as exc:
        return {"ok": False, "reason": "error", "hint": str(exc)[:200]}


# ----------------------------------------------------------------- sweep
def run_watch(app) -> dict:
    """One watch sweep: fetch all sources, diff, record events.

    Returns a report for the console and the scheduled runner; never raises
    for per-source failures (they land in the snapshots), but propagates
    nothing — the caller's ScheduledTask loop is the only error boundary it
    needs.
    """
    sources = collect_sources(app)
    started = time.time()
    if sources:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            fetched = list(pool.map(snapshot_source, sources))
    else:
        fetched = []
    fetched_by_key = {s["key"]: r for s, r in zip(sources, fetched)}

    old_data = load_snapshots(app)
    old_snaps = old_data.get("snapshots") if isinstance(old_data.get("snapshots"), dict) else {}
    # Muted sources are still fetched (their snapshot must stay fresh) but
    # produce no events. Read once per sweep: a mute flipped mid-sweep takes
    # effect from the next one, which is the same race read marks accept.
    muted_marks = old_data.get("muted") if isinstance(old_data.get("muted"), dict) else {}
    muted = {key for key in muted_marks if isinstance(key, str) and key}

    events: list[dict] = []
    new_snaps: dict[str, dict] = {}
    ok_count = 0
    error_count = 0
    for source in sources:
        key = source["key"]
        result = fetched_by_key[key]
        old = old_snaps.get(key)
        entry = {
            "key": key, "origin": source["origin"], "label": source["label"],
            "url": source["url"], "kind": source["kind"],
            "checked_at": _iso_utc(),
        }
        # Dedup keys survive failed sweeps and unchanged ones: a diff that
        # fired once must not re-fire three sweeps later just because the
        # entries in between carried no events of their own. The last
        # EVENT_KEY_KEEP keys are kept, so a sweep that fired added+removed
        # at once keeps every event's identity (the legacy single
        # last_event_key field migrates into the list).
        keys = [k for k in (old or {}).get("last_event_keys") or []
                if isinstance(k, str) and k]
        legacy = (old or {}).get("last_event_key")
        if isinstance(legacy, str) and legacy and legacy not in keys:
            keys.append(legacy)
        entry["last_event_keys"] = keys[-EVENT_KEY_KEEP:]
        if not result["ok"]:
            error_count += 1
            entry.update({"ok": False, "error": result["error"],
                          "models": (old or {}).get("models") or [],
                          "sha256": (old or {}).get("sha256") or ""})
            new_snaps[key] = entry
            continue
        ok_count += 1
        entry.update({"ok": True, "error": "",
                      "sha256": result["sha256"],
                      "models": result["models"],
                      "text_head": result["text_head"],
                      "model_count": len(result["models"])})
        if key in muted:
            # Snapshot advanced (so resuming later reports only what changes
            # from now on) but the diff is swallowed on purpose.
            new_snaps[key] = entry
            continue
        for diff in diff_snapshot(old, result, source["kind"]):
            key_hash = event_key(diff, key)
            # Page re-served the same diff (or doc chrome keeps churning):
            # an already-known key means the duplicate is skipped.
            if key_hash in keys:
                continue
            now = time.time()
            events.append({
                "id": uuid.uuid4().hex[:12],
                "ts": _iso_utc(now),
                "kind": diff["kind"],
                "source": {"key": key, "origin": source["origin"],
                           "label": source["label"], "url": source["url"]},
                "evidence": {"excerpt": result["text_head"][:EXCERPT_MAX],
                             "sha256": result["sha256"],
                             "url": source["url"]},
                "detail": diff["detail"],
                "status": "unread",
            })
            keys.append(key_hash)
            entry["last_event_keys"] = keys[-EVENT_KEY_KEEP:]
            entry["last_event_at"] = entry["checked_at"]
        new_snaps[key] = entry

    # Read marks and mutes may have been flipped while this sweep was
    # fetching (a full sweep takes minutes); both are re-read instead of
    # rolling the user's actions back on save.
    fresh = load_snapshots(app)
    fresh_marks = fresh.get("read_events")
    fresh_muted = fresh.get("muted")
    data = {"snapshots": new_snaps,
            "read_events": fresh_marks if isinstance(fresh_marks, dict) else {},
            "muted": fresh_muted if isinstance(fresh_muted, dict) else {},
            "updated_at": _iso_utc()}
    save_snapshots(app, data)

    # Append after the snapshots are safe: a crash between the two writes
    # re-reports one diff on the next sweep, never loses one.
    appended = 0
    if events:
        events_path = snapshot_path(app)
        if events_path:
            from .server import append_jsonl
            for event in events:
                append_jsonl(events_path.with_name("change-events.jsonl"),
                             event, "change_log",
                             on_error=getattr(app, "_note_write_failure", None),
                             on_ok=getattr(app, "_note_write_ok", None))
                appended += 1

    # One digest per sweep, after the events are durably recorded: an
    # unreachable webhook shows up in the report, never breaks the sweep.
    notified = notify_events(app, events) if events else {"sent": 0, "reason": ""}

    parts = [f"监视 {len(sources)} 个信息源"]
    if error_count:
        parts.append(f"{error_count} 个抓取失败")
    if appended:
        parts.append(f"{appended} 条变更")
    if notified.get("sent"):
        parts.append("已推送通知")
    elif notified.get("error"):
        parts.append("通知推送失败")
    return {
        "ok": True, "trigger": "", "sources": len(sources),
        "ok_sources": ok_count, "error_sources": error_count,
        "events": events, "appended": appended, "notify": notified,
        "elapsed_ms": round((time.time() - started) * 1000),
        "summary": "，".join(parts) if sources else "没有可监视的信息源",
    }


def changes_payload(app, limit: int = EVENTS_VIEW_LIMIT) -> dict:
    """Console payload: events (newest first, with read flags) + sources.

    Each ``new_model`` event carries ``targets`` — the channels it could be
    mapped into — so the console can render the one-click mapping without a
    second round trip. Muted sources are marked, not hidden: their past
    events stay readable, they just stop receiving new ones.
    """
    events = load_events(app, max(limit, 1))
    data = load_snapshots(app)
    read_marks = data.get("read_events") if isinstance(data.get("read_events"), dict) else {}
    muted = data.get("muted") if isinstance(data.get("muted"), dict) else {}
    unread = 0
    for event in events:
        event["status"] = "read" if event.get("id") in read_marks else "unread"
        if event["status"] == "unread":
            unread += 1
        event["targets"] = (map_targets(app, event.get("source") or {})
                            if event.get("kind") == "new_model" else [])
    snaps = []
    for entry in (data.get("snapshots") or {}).values():
        if isinstance(entry, dict):
            snaps.append({
                "key": entry.get("key"),
                "label": entry.get("label"), "kind": entry.get("kind"),
                "origin": entry.get("origin"), "url": entry.get("url"),
                "ok": entry.get("ok"), "error": entry.get("error"),
                "muted": bool(entry.get("key") in muted),
                "model_count": entry.get("model_count", len(entry.get("models") or [])),
                "checked_at": entry.get("checked_at"),
            })
    snaps.sort(key=lambda s: str(s.get("label") or ""))
    return {
        "events": events[:max(limit, 1)], "unread": unread,
        "sources": snaps, "muted": muted, "notify": notify_status(app),
        "summary": {"total": len(snaps), "ok": sum(1 for s in snaps if s.get("ok")),
                    "error": sum(1 for s in snaps if s.get("ok") is False),
                    "muted": len(muted)},
    }
