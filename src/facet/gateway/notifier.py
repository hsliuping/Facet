"""Periodic renewal reminders and runtime alerts through a webhook.

Local-first: the service only posts a short text to a URL you configure. It
never sends prompts, keys or model outputs. Renewal alerts are de-duplicated by
(subscription id, expiry, status); runtime alerts (route chains down, free
quota exhausted, channels failing repeatedly) are throttled per cause. Both
are kept in a local notifications.json so the console can show them.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

from . import subscriptions as subs
from .core import resolve_secret

MAX_HISTORY = 20
# A persistent runtime condition repeats its alert at most once per window;
# transitions (alarm raised / condition resolved) always record once.
RUNTIME_THROTTLE_SECONDS = 3600


def alert_key(alert: dict) -> str:
    return f"{alert['id']}:{alert['expires_at']}:{alert['status']}"


def alert_message(alert: dict) -> str:
    label = "已过期" if alert["status"] == "expired" else "即将到期"
    days = (f"逾期 {abs(alert['days_left'])} 天" if alert["days_left"] < 0
            else f"剩余 {alert['days_left']} 天")
    account = alert.get("account") or "未填账号"
    return (
        f"[PlanRouter 续费提醒] {label}\n"
        f"厂家：{alert['vendor']}\n账号：{account}\n套餐：{alert['plan']}"
        f"{(' / ' + alert['tier']) if alert.get('tier') else ''}\n"
        f"到期：{alert['expires_at']}（{days}）\n"
        f"价格：{alert['price']} {alert['currency']}\n"
        f"请在控制台「套餐订阅」里确认续费。"
    )


class ReminderService:
    def __init__(self, subscriptions_path, state_path, get_records, get_settings,
                 sender=None, get_health=None):
        self.subscriptions_path = Path(subscriptions_path) if subscriptions_path else None
        self.state_path = Path(state_path) if state_path else None
        self.get_records = get_records
        self.get_settings = get_settings
        self.sender = sender
        # Live runtime conditions as [{kind, key, title, message}]; see
        # App.runtime_alerts. None keeps the service renewal-only.
        self.get_health = get_health
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _read_state(self) -> dict:
        if self.state_path and self.state_path.is_file():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {
                        "sent": data.get("sent") or {},
                        "history": data.get("history") or [],
                        "runtime": data.get("runtime") or {},
                        "last_check": data.get("last_check") or 0,
                        "last_error": data.get("last_error") or "",
                    }
            except (OSError, ValueError):
                pass
        return {"sent": {}, "history": [], "runtime": {}, "last_check": 0, "last_error": ""}

    def _write_state(self, state: dict) -> None:
        if not self.state_path:
            return
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            import os
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _post(self, url: str, message: str) -> None:
        payload = json.dumps({"msgtype": "text", "text": {"content": message}},
                             ensure_ascii=False).encode("utf-8")
        request = Request(url, data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
        target = self.sender or urlopen
        result = target(request, timeout=10) if self.sender is None else target(request)
        if hasattr(result, "read"):
            result.read()
        if hasattr(result, "close"):
            result.close()

    def status(self) -> dict:
        state = self._read_state()
        settings = self.get_settings()
        pending = subs.summarize(self.get_records())["alerts"]
        return {
            "settings": {
                **settings,
                "webhook_url": self._mask(settings.get("webhook_url")),
            },
            "pending": pending,
            "last_check": state["last_check"],
            "last_error": state["last_error"],
            "history": state["history"][:MAX_HISTORY],
            "runtime": state["runtime"],
            "enabled": bool(settings["notify_enabled"] and settings["webhook_url"]),
        }

    @staticmethod
    def _mask(url: str | None) -> str:
        if not url:
            return ""
        return url if url.startswith("env:") else "***"

    def send_test(self) -> dict:
        settings = self.get_settings()
        url = resolve_secret(settings.get("webhook_url"))
        if not url:
            raise ValueError("请先填写 webhook_url（或 env:VAR）")
        self._post(url, "[PlanRouter] 通知测试：如果你收到这条消息，说明续费提醒配置正确。")
        return {"ok": True, "at": time.time()}

    def _deliver(self, message: str) -> tuple[bool, str]:
        settings = self.get_settings()
        url = resolve_secret(settings.get("webhook_url"))
        try:
            self._post(url, message)
            return True, ""
        except Exception as exc:  # a broken webhook must not break the router
            return False, str(exc)[:200]

    def _history(self, state: dict, key: str, title: str, ok: bool, error: str,
                 resolved: bool = False) -> None:
        entry = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "key": key,
            "ok": ok,
            "error": error,
            "title": title,
        }
        if resolved:
            entry["resolved"] = True
        state["history"].insert(0, entry)
        state["history"] = state["history"][:MAX_HISTORY]
        state["last_error"] = "" if ok else error

    def check(self, force: bool = False) -> dict:
        settings = self.get_settings()
        url = resolve_secret(settings.get("webhook_url"))
        state = self._read_state()
        state["last_check"] = time.time()
        if not settings["notify_enabled"] or not url:
            state["last_error"] = ""
            self._write_state(state)
            return {"sent": 0, "skipped": "disabled", "history": state["history"]}

        alerts = subs.summarize(self.get_records())["alerts"]
        sent = 0
        with self.lock:
            for alert in alerts:
                key = alert_key(alert)
                if not force and key in state["sent"]:
                    continue
                ok, error = self._deliver(alert_message(alert))
                self._history(state, key,
                              f"{alert['vendor']} / {alert['account'] or '—'} / {alert['plan']}",
                              ok, error)
                if ok:
                    state["sent"][key] = time.time()
                    sent += 1
        self._write_state(state)
        return {"sent": sent, "history": state["history"], "last_error": state["last_error"]}

    def check_runtime(self) -> dict:
        """Alarm on live runtime conditions, throttled per cause.

        Conditions come from `get_health` as [{kind, key, title, message}].
        While one persists it re-alerts at most once per
        RUNTIME_THROTTLE_SECONDS; when it clears, a single resolved entry
        closes it out. The console history records transitions even with the
        webhook disabled.
        """
        try:
            conditions = list((self.get_health or (lambda: []))())
        except Exception:  # a broken condition probe must not kill the loop
            return {"sent": 0, "resolved": 0}
        active = {str(c["key"]): c for c in conditions if isinstance(c, dict)}
        settings = self.get_settings()
        url = resolve_secret(settings.get("webhook_url"))
        enabled = bool(settings["notify_enabled"] and url)
        state = self._read_state()
        now = time.time()
        sent = resolved = 0
        with self.lock:
            runtime = state.setdefault("runtime", {})
            for key, cond in active.items():
                entry = runtime.setdefault(key, {"active": False, "last_sent": 0.0, "title": ""})
                fresh = not entry.get("active")
                entry["title"] = str(cond.get("title") or key)
                if not fresh and now - float(entry.get("last_sent") or 0) < RUNTIME_THROTTLE_SECONDS:
                    continue  # same cause already alarmed inside the window
                entry["active"] = True
                entry["last_sent"] = now
                if enabled:
                    ok, error = self._deliver(str(cond.get("message") or entry["title"]))
                    self._history(state, key, entry["title"], ok, error)
                    if ok:
                        sent += 1
                elif fresh:
                    self._history(state, key, entry["title"], True, "")
            for key, entry in list(runtime.items()):
                if entry.get("active") and key not in active:
                    entry["active"] = False
                    resolved += 1
                    title = str(entry.get("title") or key)
                    ok, error = True, ""
                    if enabled:
                        ok, error = self._deliver(f"[PlanRouter 运行告警恢复] {title}")
                    self._history(state, key, title, ok, error, resolved=True)
        self._write_state(state)
        return {"sent": sent, "resolved": resolved, "active": sorted(active)}

    def start(self) -> None:
        def loop():
            # give the server a moment to come up, then check once
            if self._stop.wait(10):
                return
            self.check()
            self.check_runtime()
            last_full = time.time()
            while True:
                # runtime conditions are cheap to scan and deserve a quick
                # verdict; renewal checks keep their configurable cadence and
                # pick up interval changes without restarting the router
                if self._stop.wait(60):
                    return
                self.check_runtime()
                interval = max(1, int(self.get_settings()["interval_minutes"])) * 60
                if time.time() - last_full >= interval:
                    self.check()
                    last_full = time.time()

        self._thread = threading.Thread(target=loop, daemon=True, name="planrouter-reminder")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
