"""Daily usage buckets behind the trend chart (no external dependencies)."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path

COUNTERS = ("requests", "success", "failed", "tokens")
TOKEN_COUNTERS = ("input_tokens", "cached_tokens")


class UsageRecorder:
    def __init__(self, path, interval: float = 5.0, retention_days: int = 180,
                 on_error=None, on_ok=None):
        self.path = Path(path)
        self.interval = interval
        # Prune buckets older than this on the day switch; 0 keeps all days.
        self.retention_days = max(0, int(retention_days or 0))
        # Persistence outcome callbacks (kind, exc) / (kind): the caller turns
        # a failed write into a runtime alert instead of a silent drop.
        self._on_error = on_error
        self._on_ok = on_ok
        self.lock = threading.Lock()
        self.data = self._read()
        self.dirty = False
        self._last_prune_day = date.today().isoformat()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="planrouter-usage")
        if self.retention_days:
            self._prune(date.today())

    def _read(self) -> dict:
        if self.path.is_file():
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(document, dict) and isinstance(document.get("days"), dict):
                    return document
            except (OSError, ValueError):
                pass
        return {"days": {}}

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        self.flush()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.flush()

    def record(self, channel: str, success: bool, tokens: int = 0,
               on: date | None = None, model: str = "",
               input_tokens: int = 0, cached_tokens: int = 0,
               user: str = "") -> None:
        """Count one request for `channel` (and optionally its wire model).

        `input_tokens`/`cached_tokens` come from the upstream usage block so
        the console can show a cache hit rate; they stay absent when the
        upstream does not report cache details. `user` attributes the request
        to a member key ("" = owner/anonymous); member buckets live beside
        the per-channel data under a parallel top-level `users` key.
        """
        day = (on or date.today()).isoformat()
        if self.retention_days and day != self._last_prune_day:
            self._prune(on or date.today())
            self._last_prune_day = day
        with self.lock:
            bucket = self.data["days"].setdefault(day, {})
            entry = bucket.setdefault(channel, {key: 0 for key in COUNTERS})
            self._tally(entry, success, tokens, model, input_tokens, cached_tokens)
            if user:
                user_bucket = self.data.setdefault("users", {})
                user_day = user_bucket.setdefault(user, {}).setdefault(day, {key: 0 for key in COUNTERS})
                self._tally(user_day, success, tokens, model, input_tokens, cached_tokens)
                # Per-member channel mix (no model split): enough for the
                # console's "who used which channel" view and cost sharing.
                channels = user_day.setdefault("channels", {})
                row = channels.setdefault(channel, {"requests": 0, "tokens": 0})
                row["requests"] += 1
                row["tokens"] += int(tokens or 0)
            self.dirty = True

    def _tally(self, entry: dict, success: bool, tokens: int, model: str,
               input_tokens: int, cached_tokens: int) -> None:
        entry["requests"] += 1
        entry["success" if success else "failed"] += 1
        entry["tokens"] += int(tokens or 0)
        if input_tokens or cached_tokens:
            entry["input_tokens"] = int(entry.get("input_tokens") or 0) + int(input_tokens or 0)
            entry["cached_tokens"] = int(entry.get("cached_tokens") or 0) + int(cached_tokens or 0)
        if model:
            models = entry.setdefault("models", {})
            row = models.setdefault(model, {
                "requests": 0, "success": 0, "failed": 0, "tokens": 0,
                **{key: 0 for key in TOKEN_COUNTERS}, "last_used": 0.0,
            })
            row["requests"] += 1
            row["success" if success else "failed"] += 1
            row["tokens"] += int(tokens or 0)
            row["input_tokens"] = int(row.get("input_tokens") or 0) + int(input_tokens or 0)
            row["cached_tokens"] = int(row.get("cached_tokens") or 0) + int(cached_tokens or 0)
            row["last_used"] = time.time()

    def _prune(self, on: date) -> None:
        """Drop day buckets older than the retention window.

        Both the per-channel `days` map and the parallel `users` map are
        trimmed so neither grows without bound; a user left with no days is
        removed entirely. ISO dates compare correctly as strings.
        """
        if not self.retention_days:
            return
        cutoff = (on - timedelta(days=self.retention_days)).isoformat()
        with self.lock:
            days = self.data.get("days") or {}
            stale = [day for day in days if day < cutoff]
            for day in stale:
                del days[day]
            changed = bool(stale)
            users = self.data.get("users") or {}
            for name in list(users):
                bucket = users[name]
                gone = [day for day in bucket if day < cutoff]
                for day in gone:
                    del bucket[day]
                changed = changed or bool(gone)
                if not bucket:
                    del users[name]
            if changed:
                self.dirty = True

    def flush(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            payload = json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            self.dirty = False
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            # Keep the in-memory data marked dirty so the next tick retries,
            # and let the caller raise a runtime alert instead of losing it
            # without a trace.
            with self.lock:
                self.dirty = True
            if self._on_error:
                self._on_error("usage", exc)
        else:
            if self._on_ok:
                self._on_ok("usage")

    def model_usage(self, channel: str, model: str, period: str = "total",
                    on: date | None = None) -> dict:
        """Local consumption of one (channel, wire model) inside a quota window.

        `period` is "total" (all recorded days), "month" or "day". `success`
        excludes failed calls because vendors only bill answered requests.
        """
        on = on or date.today()
        month = on.isoformat()[:7]
        today = on.isoformat()
        used = {"requests": 0, "success": 0, "failed": 0, "tokens": 0}
        with self.lock:
            for day, bucket in self.data["days"].items():
                if period == "day" and day != today:
                    continue
                if period == "month" and not day.startswith(month):
                    continue
                entry = (bucket.get(channel) or {}).get("models") or {}
                row = entry.get(model)
                if not isinstance(row, dict):
                    continue
                for key in used:
                    used[key] += int(row.get(key) or 0)
        return used

    def series(self, days: int = 30, on: date | None = None) -> dict:
        on = on or date.today()
        days = max(1, min(days, 365))
        with self.lock:
            snapshot = {day: dict(channels) for day, channels in self.data["days"].items()}
        timeline = []
        totals: dict[str, dict] = {}
        for offset in range(days - 1, -1, -1):
            day = (on - timedelta(days=offset)).isoformat()
            bucket = snapshot.get(day, {})
            item = {"date": day}
            for key in COUNTERS:
                item[key] = sum(values.get(key, 0) for values in bucket.values())
            timeline.append(item)
            for name, values in bucket.items():
                aggregate = totals.setdefault(
                    name, {"name": name, **{key: 0 for key in COUNTERS},
                           **{key: 0 for key in TOKEN_COUNTERS}},
                )
                for key in COUNTERS:
                    aggregate[key] += values.get(key, 0)
                for key in TOKEN_COUNTERS:
                    aggregate[key] += int(values.get(key) or 0)
        channels = sorted(totals.values(), key=lambda item: -item["requests"])
        return {
            "range": days,
            "days": timeline,
            "channels": channels,
            "note": "tokens 含上游报告的流式用量；缓存率 = 缓存命中输入 / 总输入 tokens；"
                    "套餐为月费，此处不折算按量费用",
        }

    def model_stats(self, days: int = 30, on: date | None = None) -> dict:
        """Per (channel, model) request/token totals plus channel cache counters."""
        on = on or date.today()
        days = max(1, min(days, 365))
        window = {(on - timedelta(days=offset)).isoformat() for offset in range(days)}
        with self.lock:
            snapshot = {day: dict(channels) for day, channels in self.data["days"].items()}
        models: dict[tuple[str, str], dict] = {}
        cache: dict[str, dict] = {}
        for day, bucket in snapshot.items():
            if day not in window:
                continue
            for name, values in bucket.items():
                channel_cache = cache.setdefault(
                    name, {"name": name, **{key: 0 for key in TOKEN_COUNTERS}})
                for key in TOKEN_COUNTERS:
                    channel_cache[key] += int(values.get(key) or 0)
                for model, item in (values.get("models") or {}).items():
                    if not isinstance(item, dict):
                        continue
                    row = models.setdefault((name, model), {
                        "channel": name, "model": model,
                        **{key: 0 for key in COUNTERS},
                        **{key: 0 for key in TOKEN_COUNTERS}, "last_used": 0.0,
                    })
                    for key in COUNTERS + TOKEN_COUNTERS:
                        row[key] += int(item.get(key) or 0)
                    row["last_used"] = max(
                        row["last_used"], float(item.get("last_used") or 0))
        return {
            "range": days,
            "models": sorted(models.values(),
                             key=lambda item: (-item["requests"], item["channel"], item["model"])),
            "cache": sorted(cache.values(), key=lambda item: item["name"]),
        }

    def member_totals(self, name: str, since: date, on: date | None = None) -> dict:
        """One member's counters from `since` (inclusive) up to `on`, for quotas.

        `since` is the quota-window start — today for daily caps, the first of
        the month for monthly ones. Channels ride along so the gateway can
        attribute spend without a second pass.
        """
        on = on or date.today()
        since_iso = since.isoformat()
        today_iso = on.isoformat()
        totals = {key: 0 for key in COUNTERS}
        channels: dict[str, dict] = {}
        with self.lock:
            bucket = (self.data.get("users") or {}).get(name) or {}
            for day, entry in bucket.items():
                if not isinstance(entry, dict) or not (since_iso <= day <= today_iso):
                    continue
                for key in COUNTERS:
                    totals[key] += int(entry.get(key) or 0)
                for channel, row in (entry.get("channels") or {}).items():
                    if not isinstance(row, dict):
                        continue
                    agg = channels.setdefault(channel, {"requests": 0, "tokens": 0})
                    agg["requests"] += int(row.get("requests") or 0)
                    agg["tokens"] += int(row.get("tokens") or 0)
        return {"since": since_iso, "until": today_iso, **totals, "channels": channels}

    def user_stats(self, days: int = 30, on: date | None = None) -> dict:
        """Per-member usage over a window: totals, channel mix, per-day timeline.

        Each member row carries the per-channel totals, the latest day's
        counters (`today`, for quota progress bars) and a `spike` flag raised
        when that day has >=10 requests and at least 3x the member's average
        on their earlier recorded days — the console surfaces bursts with it.
        """
        on = on or date.today()
        days = max(1, min(days, 365))
        window = {(on - timedelta(days=offset)).isoformat() for offset in range(days)}
        latest = on.isoformat()
        with self.lock:
            users = {name: dict(bucket) for name, bucket
                     in (self.data.get("users") or {}).items()}
        timeline: dict[str, dict] = {}
        totals: dict[str, dict] = {}
        for name, bucket in users.items():
            for day, entry in bucket.items():
                if not isinstance(entry, dict) or day not in window:
                    continue
                day_row = timeline.setdefault(day, {})
                row = day_row.setdefault(name, {key: 0 for key in COUNTERS})
                for key in COUNTERS:
                    row[key] += int(entry.get(key) or 0)
                aggregate = totals.setdefault(
                    name, {"name": name, **{key: 0 for key in COUNTERS},
                           "channels": {}, "today": {key: 0 for key in COUNTERS}})
                for key in COUNTERS:
                    aggregate[key] += int(entry.get(key) or 0)
                if day == latest:
                    for key in COUNTERS:
                        aggregate["today"][key] += int(entry.get(key) or 0)
                for channel, ch in (entry.get("channels") or {}).items():
                    if not isinstance(ch, dict):
                        continue
                    agg = aggregate["channels"].setdefault(
                        channel, {"requests": 0, "tokens": 0})
                    agg["requests"] += int(ch.get("requests") or 0)
                    agg["tokens"] += int(ch.get("tokens") or 0)
        for info in totals.values():
            # Spike compares the latest day against the mean of the member's
            # earlier recorded days inside the window (missing days are idle).
            today_requests = info["today"]["requests"]
            prior = [int(entry.get("requests") or 0)
                     for day, entry in users.get(info["name"], {}).items()
                     if isinstance(entry, dict) and day in window and day != latest]
            mean_prior = (sum(prior) / len(prior)) if prior else 0.0
            info["spike"] = today_requests >= 10 and today_requests >= 3 * mean_prior
        day_list = [{"date": day, "users": timeline.get(day) or {}}
                    for day in sorted(timeline)]
        return {
            "range": days,
            "today": latest,
            "users": sorted(totals.values(), key=lambda item: -item["requests"]),
            "days": day_list,
        }
