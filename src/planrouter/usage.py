"""Daily usage buckets behind the trend chart (no external dependencies)."""
from __future__ import annotations

import json
import os
import threading
from datetime import date, timedelta
from pathlib import Path

COUNTERS = ("requests", "success", "failed", "tokens")


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

    def record(self, channel: str, success: bool, tokens: int = 0, on: date | None = None) -> None:
        day = (on or date.today()).isoformat()
        with self.lock:
            bucket = self.data["days"].setdefault(day, {})
            entry = bucket.setdefault(channel, {key: 0 for key in COUNTERS})
            entry["requests"] += 1
            entry["success" if success else "failed"] += 1
            entry["tokens"] += int(tokens or 0)
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
                    name, {"name": name, **{key: 0 for key in COUNTERS}},
                )
                for key in COUNTERS:
                    aggregate[key] += values.get(key, 0)
        channels = sorted(totals.values(), key=lambda item: -item["requests"])
        return {
            "range": days,
            "days": timeline,
            "channels": channels,
            "note": "tokens 含上游报告的流式用量；套餐为月费，此处不折算按量费用",
        }
