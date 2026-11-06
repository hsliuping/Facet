"""Periodic renewal reminders through a webhook (WeCom / DingTalk / custom JSON).

Local-first: the service only posts a short text to a URL you configure. It
never sends prompts, keys or model outputs. Sent alerts are de-duplicated by
(subscription id, expiry, status) and kept in a local notifications.json.
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
    def __init__(self, subscriptions_path, state_path, get_records, get_settings, sender=None):
        self.subscriptions_path = Path(subscriptions_path) if subscriptions_path else None
        self.state_path = Path(state_path) if state_path else None
        self.get_records = get_records
        self.get_settings = get_settings
        self.sender = sender
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
                        "last_check": data.get("last_check") or 0,
                        "last_error": data.get("last_error") or "",
                    }
            except (OSError, ValueError):
                pass
        return {"sent": {}, "history": [], "last_check": 0, "last_error": ""}

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
                message = alert_message(alert)
                try:
                    self._post(url, message)
                    ok, error = True, ""
                except Exception as exc:  # a broken webhook must not break the router
                    ok, error = False, str(exc)[:200]
                entry = {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "key": key,
                    "ok": ok,
                    "error": error,
                    "title": f"{alert['vendor']} / {alert['account'] or '—'} / {alert['plan']}",
                }
                state["history"].insert(0, entry)
                state["history"] = state["history"][:MAX_HISTORY]
                state["last_error"] = "" if ok else error
                if ok:
                    state["sent"][key] = time.time()
                    sent += 1
        self._write_state(state)
        return {"sent": sent, "history": state["history"], "last_error": state["last_error"]}

    def start(self) -> None:
        def loop():
            # give the server a moment to come up, then check once
            if self._stop.wait(10):
                return
            self.check()
            while True:
                # pick up interval changes without restarting the router
                interval = max(1, int(self.get_settings()["interval_minutes"])) * 60
                if self._stop.wait(interval):
                    return
                self.check()

        self._thread = threading.Thread(target=loop, daemon=True, name="planrouter-reminder")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
