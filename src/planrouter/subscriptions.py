"""Vendor plan subscriptions: what you bought, when it expires, what it costs.

A subscription is separate from a channel: it records vendor/account/plan/price
and validity, while a channel records how requests reach it. One subscription
may back several channels (e.g. two accounts at the same vendor).
"""
from __future__ import annotations

import calendar
import json
import os
import uuid
from datetime import date
from pathlib import Path

BILLING_CYCLES = ("monthly", "quarterly", "yearly", "one_time", "custom")
CURRENCIES = ("CNY", "USD", "EUR", "HKD")
DEFAULT_REMIND_DAYS = 7
CYCLE_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12, "one_time": 1, "custom": 1}
CYCLE_DIVISOR = {"monthly": 1, "quarterly": 3, "yearly": 12, "one_time": 1, "custom": 1}


def _date(value, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 格式") from exc


def add_months(day: date, months: int) -> date:
    """Add calendar months, clamping to the last valid day of the month."""
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def normalize(record: dict) -> dict:
    if not isinstance(record, dict):
        raise ValueError("每条订阅必须是一个对象")
    vendor = str(record.get("vendor") or "").strip()
    plan = str(record.get("plan") or "").strip()
    if not vendor:
        raise ValueError("订阅缺少厂家 vendor")
    if not plan:
        raise ValueError("订阅缺少套餐名称 plan")
    try:
        price = float(record.get("price") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("price 必须是数字") from exc
    if price < 0:
        raise ValueError("price 不能为负数")
    currency = str(record.get("currency") or "CNY").upper()
    if currency not in CURRENCIES:
        raise ValueError(f"currency 必须是 {', '.join(CURRENCIES)} 之一")
    cycle = str(record.get("billing_cycle") or "monthly")
    if cycle not in BILLING_CYCLES:
        raise ValueError(f"billing_cycle 必须是 {', '.join(BILLING_CYCLES)} 之一")
    try:
        remind_days = int(record.get("remind_days") if record.get("remind_days") not in (None, "") else DEFAULT_REMIND_DAYS)
    except (TypeError, ValueError) as exc:
        raise ValueError("remind_days 必须是整数") from exc
    if remind_days < 0:
        raise ValueError("remind_days 不能为负数")
    raw_channels = record.get("channels") or []
    if isinstance(raw_channels, str):
        raw_channels = raw_channels.split(",")
    channels = [str(x).strip() for x in raw_channels if str(x).strip()]
    started = _date(record.get("started_at"), "started_at")
    expires = _date(record.get("expires_at"), "expires_at")
    if started and expires and expires < started:
        raise ValueError("expires_at 不能早于 started_at")
    return {
        "id": str(record.get("id") or ("sub-" + uuid.uuid4().hex[:10])).strip(),
        "vendor": vendor,
        "account": str(record.get("account") or "").strip(),
        "plan": plan,
        "tier": str(record.get("tier") or "").strip(),
        "price": round(price, 2),
        "currency": currency,
        "billing_cycle": cycle,
        "started_at": started.isoformat() if started else "",
        "expires_at": expires.isoformat() if expires else "",
        "auto_renew": bool(record.get("auto_renew", False)),
        "remind_days": remind_days,
        "channels": channels,
        "note": str(record.get("note") or "").strip(),
    }


def normalize_all(raw) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("subscriptions")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("subscriptions 必须是数组")
    records = [normalize(item) for item in raw]
    ids = [r["id"] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError("订阅 id 重复")
    return records


def monthly_equivalent(record: dict) -> dict | None:
    cycle = record.get("billing_cycle")
    divisor = CYCLE_DIVISOR.get(cycle)
    if not divisor:
        return None
    return {"currency": record.get("currency", "CNY"),
            "amount": round(float(record.get("price") or 0) / divisor, 2)}


def enrich(record: dict, on: date | None = None) -> dict:
    on = on or date.today()
    expires = _date(record.get("expires_at"), "expires_at")
    days_left = (expires - on).days if expires else None
    if days_left is None:
        status = "unknown"
    elif days_left < 0:
        status = "expired"
    elif days_left <= int(record.get("remind_days") or DEFAULT_REMIND_DAYS):
        status = "expiring"
    else:
        status = "active"
    result = dict(record)
    result.update({
        "days_left": days_left,
        "status": status,
        "monthly_equivalent": monthly_equivalent(record),
    })
    return result


def summarize(records: list[dict], on: date | None = None) -> dict:
    on = on or date.today()
    enriched = [enrich(record, on) for record in records]
    counts = {key: 0 for key in ("active", "expiring", "expired", "unknown")}
    monthly_cost: dict[str, float] = {}
    for record in enriched:
        counts[record["status"]] += 1
        equivalent = record["monthly_equivalent"]
        if equivalent and record["status"] != "expired":
            code = equivalent["currency"]
            monthly_cost[code] = round(monthly_cost.get(code, 0) + equivalent["amount"], 2)
    alerts = [
        {
            "id": r["id"], "vendor": r["vendor"], "account": r["account"],
            "plan": r["plan"], "tier": r["tier"], "expires_at": r["expires_at"],
            "days_left": r["days_left"], "status": r["status"],
            "price": r["price"], "currency": r["currency"],
        }
        for r in enriched if r["status"] in ("expiring", "expired")
    ]
    alerts.sort(key=lambda r: (r["days_left"] is None, r["days_left"]))
    upcoming = sorted(
        [r for r in enriched if r["days_left"] is not None and r["days_left"] >= 0],
        key=lambda r: r["days_left"],
    )[:5]
    return {
        "today": on.isoformat(),
        "total": len(enriched),
        "counts": counts,
        "monthly_cost": monthly_cost,
        "alerts": alerts,
        "upcoming": [
            {"id": r["id"], "vendor": r["vendor"], "account": r["account"],
             "plan": r["plan"], "expires_at": r["expires_at"], "days_left": r["days_left"]}
            for r in upcoming
        ],
    }


def next_expiry(record: dict, on: date | None = None) -> date:
    """Default renewal date: one billing cycle after the later of expiry/today."""
    on = on or date.today()
    expires = _date(record.get("expires_at"), "expires_at") or on
    base = max(expires, on)
    return add_months(base, CYCLE_MONTHS.get(record.get("billing_cycle"), 1))


def renew_record(record: dict, payload: dict | None = None, on: date | None = None) -> dict:
    """Produce a renewed record; UI pre-fills these values for confirmation."""
    on = on or date.today()
    payload = payload or {}
    updated = dict(record)
    updated["started_at"] = payload.get("started_at") or on.isoformat()
    updated["expires_at"] = payload.get("expires_at") or next_expiry(record, on).isoformat()
    if payload.get("price") not in (None, ""):
        updated["price"] = payload["price"]
    if payload.get("tier") not in (None, ""):
        updated["tier"] = payload["tier"]
    return normalize(updated)


DEFAULT_SETTINGS = {
    "block_expired_channels": True,
    "notify_enabled": False,
    "webhook_url": "",
    "interval_minutes": 60,
}


def normalize_settings(raw) -> dict:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("settings 必须是对象")
    out = dict(DEFAULT_SETTINGS)
    out["block_expired_channels"] = bool(raw.get("block_expired_channels", True))
    out["notify_enabled"] = bool(raw.get("notify_enabled", False))
    url = str(raw.get("webhook_url") or "").strip()
    if url and not (url.startswith(("http://", "https://", "env:"))):
        raise ValueError("webhook_url 必须是 http(s) URL 或 env:VAR")
    out["webhook_url"] = url
    try:
        interval = int(raw.get("interval_minutes") or 60)
    except (TypeError, ValueError) as exc:
        raise ValueError("interval_minutes 必须是整数") from exc
    if not 1 <= interval <= 1440:
        raise ValueError("interval_minutes 必须在 1-1440 之间")
    out["interval_minutes"] = interval
    return out


def _read_document(path: str | Path | None) -> dict:
    if not path or not Path(path).is_file():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_settings(path: str | Path | None) -> dict:
    return normalize_settings(_read_document(path).get("settings"))


def load_file(path: str | Path | None) -> list[dict]:
    document = _read_document(path)
    return normalize_all(document.get("subscriptions") if isinstance(document, dict) else document)


def save_file(path: str | Path, records: list[dict], settings: dict | None = None) -> None:
    """Write records while preserving settings and the human-readable note."""
    target = Path(path)
    document = _read_document(target)
    payload = {}
    if document.get("_readme"):
        payload["_readme"] = document["_readme"]
    payload["settings"] = normalize_settings(
        settings if settings is not None else document.get("settings")
    )
    payload["subscriptions"] = records
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
