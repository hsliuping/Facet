"""Source watch: observe upstream info sources and record their changes.

Three layers of sources are watched, with no new configuration surface —
everything is already configured for catalog refresh and maintenance:

- the vendor registry (builtin providers.json + user overlay): every mode's
  ``docs_url`` and ``models_url``,
- every enabled channel's own models endpoint (``base_url`` + models path,
  with the channel's key — the same read ``probe_models`` performs),
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
from urllib.error import HTTPError, URLError
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
# Read markers outlive their events once the JSONL trims; this keeps the
# snapshots file bounded regardless.
READ_MARKS_KEEP = 2000


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

    # Layer 2: enabled channels' own models endpoints.
    for channel in getattr(app, "config", None) and app.config.channels or []:
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
        return {"url": url, "sha256": _sha256(raw), "models": models,
                "text_head": head, "ok": True, "error": ""}
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
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
    """
    if not old:
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


def mark_read(app, ids: list[str], read: bool = True) -> int:
    """Flip the read flag of events by id (in-file; the JSONL is not rewritten)."""
    data = load_snapshots(app)
    marks = data.get("read_events")
    if not isinstance(marks, dict):
        marks = {}
    changed = 0
    for event_id in ids:
        if not isinstance(event_id, str) or not event_id:
            continue
        if read:
            if event_id not in marks:
                changed += 1
            marks[event_id] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                            time.localtime())
        elif event_id in marks:
            del marks[event_id]
            changed += 1
    # Older read markers than the JSONL keeps are dead weight; prune to cap.
    if len(marks) > READ_MARKS_KEEP:
        ordered = sorted(marks.items(), key=lambda kv: kv[1], reverse=True)
        marks = dict(ordered[:READ_MARKS_KEEP])
    data["read_events"] = marks
    save_snapshots(app, data)
    return changed


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
    read_marks = old_data.get("read_events") if isinstance(old_data.get("read_events"), dict) else {}

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
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
        # The dedup key survives failed sweeps and unchanged ones: a diff that
        # fired once must not re-fire three sweeps later just because the
        # entries in between carried no events of their own.
        if old and old.get("last_event_key"):
            entry["last_event_key"] = old["last_event_key"]
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
        for diff in diff_snapshot(old, result, source["kind"]):
            key_hash = event_key(diff, key)
            # Same event as the source's last one (page re-served the same
            # diff, or doc chrome keeps churning): skip the duplicate.
            if old and old.get("last_event_key") == key_hash:
                continue
            now = time.time()
            events.append({
                "id": uuid.uuid4().hex[:12],
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + "Z",
                "kind": diff["kind"],
                "source": {"key": key, "origin": source["origin"],
                           "label": source["label"], "url": source["url"]},
                "evidence": {"excerpt": result["text_head"][:EXCERPT_MAX],
                             "sha256": result["sha256"],
                             "url": source["url"]},
                "detail": diff["detail"],
                "status": "unread",
            })
            entry["last_event_key"] = key_hash
            entry["last_event_at"] = entry["checked_at"]
        new_snaps[key] = entry

    data = {"snapshots": new_snaps, "read_events": read_marks,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())}
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

    parts = [f"监视 {len(sources)} 个信息源"]
    if error_count:
        parts.append(f"{error_count} 个抓取失败")
    if appended:
        parts.append(f"{appended} 条变更")
    return {
        "ok": True, "trigger": "", "sources": len(sources),
        "ok_sources": ok_count, "error_sources": error_count,
        "events": events, "appended": appended,
        "elapsed_ms": round((time.time() - started) * 1000),
        "summary": "，".join(parts) if sources else "没有可监视的信息源",
    }


def changes_payload(app, limit: int = EVENTS_VIEW_LIMIT) -> dict:
    """Console payload: events (newest first, with read flags) + sources."""
    events = load_events(app, max(limit, 1))
    data = load_snapshots(app)
    read_marks = data.get("read_events") if isinstance(data.get("read_events"), dict) else {}
    unread = 0
    for event in events:
        event["status"] = "read" if event.get("id") in read_marks else "unread"
        if event["status"] == "unread":
            unread += 1
    snaps = []
    for entry in (data.get("snapshots") or {}).values():
        if isinstance(entry, dict):
            snaps.append({
                "label": entry.get("label"), "kind": entry.get("kind"),
                "origin": entry.get("origin"), "url": entry.get("url"),
                "ok": entry.get("ok"), "error": entry.get("error"),
                "model_count": entry.get("model_count", len(entry.get("models") or [])),
                "checked_at": entry.get("checked_at"),
            })
    snaps.sort(key=lambda s: str(s.get("label") or ""))
    return {
        "events": events[:max(limit, 1)], "unread": unread,
        "sources": snaps,
        "summary": {"total": len(snaps), "ok": sum(1 for s in snaps if s.get("ok")),
                    "error": sum(1 for s in snaps if s.get("ok") is False)},
    }
