"""Monthly cost/usage report: subscriptions joined with per-channel usage."""
from __future__ import annotations

from datetime import date

from . import subscriptions as subs

COUNTERS = ("requests", "success", "failed", "tokens")


def _zero() -> dict:
    return {key: 0 for key in COUNTERS}


def _add(target: dict, source: dict) -> None:
    for key in COUNTERS:
        target[key] += int(source.get(key, 0) or 0)


def build_report(records: list[dict], days: dict, month: str | None = None,
                 on: date | None = None) -> dict:
    """Join subscriptions with the usage days that fall inside `month`.

    Usage is counted once per channel even if several subscriptions link to it;
    the per-subscription figures are attribution, not an additional total.
    """
    on = on or date.today()
    month = (month or on.strftime("%Y-%m")).strip()
    try:
        parts = month.split("-")
        if len(parts) != 2:
            raise ValueError
        year, mon = int(parts[0]), int(parts[1])
        if not (1 <= mon <= 12 and 1900 <= year <= 9999):
            raise ValueError
    except ValueError as exc:
        raise ValueError("month 必须是 YYYY-MM 格式") from exc
    prefix = f"{year:04d}-{mon:02d}-"

    channel_totals: dict[str, dict] = {}
    active_days = 0
    for day, buckets in (days or {}).items():
        if not str(day).startswith(prefix) or not isinstance(buckets, dict):
            continue
        active_days += 1
        for name, values in buckets.items():
            if isinstance(values, dict):
                _add(channel_totals.setdefault(str(name), _zero()), values)

    enriched = [subs.enrich(record, on) for record in records]
    linked = {name for record in enriched for name in (record.get("channels") or [])}
    rows = []
    currency_totals: dict[str, float] = {}
    vendors: dict[tuple[str, str], dict] = {}

    for record in enriched:
        usage = _zero()
        for name in record.get("channels") or []:
            if name in channel_totals:
                _add(usage, channel_totals[name])
        equivalent = record.get("monthly_equivalent") or {}
        amount = float(equivalent.get("amount") or 0)
        currency = equivalent.get("currency") or record.get("currency", "CNY")
        if record["status"] != "expired":
            currency_totals[currency] = round(currency_totals.get(currency, 0) + amount, 2)
        group = vendors.setdefault((record["vendor"], record["account"]), {
            "vendor": record["vendor"], "account": record["account"],
            "subscriptions": 0, "monthly_cost": {},
        })
        group["subscriptions"] += 1
        if record["status"] != "expired":
            group["monthly_cost"][currency] = round(
                group["monthly_cost"].get(currency, 0) + amount, 2,
            )
        rows.append({
            "id": record["id"], "vendor": record["vendor"],
            "account": record["account"], "plan": record["plan"],
            "tier": record["tier"], "status": record["status"],
            "days_left": record["days_left"], "expires_at": record["expires_at"],
            "price": record["price"], "currency": record["currency"],
            "billing_cycle": record["billing_cycle"],
            "monthly_equivalent": equivalent,
            "channels": record.get("channels") or [],
            "quota_limit": record.get("quota_limit"),
            "quota_used": record.get("quota_used"),
            "quota_unit": record.get("quota_unit", ""),
            "quota_remaining": record.get("quota_remaining"),
            "quota_ratio": record.get("quota_ratio"),
            "usage": usage,
            "cost_per_1k_requests": (round(amount / usage["requests"] * 1000, 2)
                                     if usage["requests"] else None),
        })

    totals = _zero()
    for values in channel_totals.values():
        _add(totals, values)
    channels = [
        {"name": name, **values, "linked": name in linked}
        for name, values in channel_totals.items()
    ]
    channels.sort(key=lambda item: -item["requests"])
    return {
        "month": f"{year:04d}-{mon:02d}",
        "today": on.isoformat(),
        "active_days": active_days,
        "currency_totals": currency_totals,
        "total": totals,
        "subscriptions": rows,
        "vendors": sorted(vendors.values(), key=lambda item: (item["vendor"], item["account"])),
        "channels": channels,
        "note": "成本为月费等效值，不折算按量费用；同一渠道被多个订阅关联时用量只计一次",
    }
