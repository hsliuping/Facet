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
    def __init__(self, path, interval: float = 5.0):
        self.path = Path(path)
        self.interval = interval
        self.lock = threading.Lock()
        self.data = self._read()
        self.dirty = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="planrouter-usage")

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
               input_tokens: int = 0, cached_tokens: int = 0) -> None:
        """Count one request for `channel` (and optionally its wire model).

        `input_tokens`/`cached_tokens` come from the upstream usage block so
        the console can show a cache hit rate; they stay absent when the
        upstream does not report cache details.
        """
        day = (on or date.today()).isoformat()
        with self.lock:
            bucket = self.data["days"].setdefault(day, {})
            entry = bucket.setdefault(channel, {key: 0 for key in COUNTERS})
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
        except OSError:
            pass

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
