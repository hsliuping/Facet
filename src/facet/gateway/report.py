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


def _parse_month(month: str | None, on: date) -> tuple[int, int, str]:
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
    return year, mon, f"{year:04d}-{mon:02d}-"


def build_report(records: list[dict], days: dict, month: str | None = None,
                 on: date | None = None) -> dict:
    """Join subscriptions with the usage days that fall inside `month`.

    Usage is counted once per channel even if several subscriptions link to it;
    the per-subscription figures are attribution, not an additional total.
    """
    on = on or date.today()
    year, mon, prefix = _parse_month(month, on)

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
        # A subscription no longer stores a vendor of its own: the server
        # derives it from the linked channel and injects it as `vendor`,
        # falling back to the plan name for unbound rows.
        vendor = record.get("vendor") or record.get("plan") or ""
        group = vendors.setdefault((vendor, record["account"]), {
            "vendor": vendor, "account": record["account"],
            "subscriptions": 0, "monthly_cost": {},
        })
        group["subscriptions"] += 1
        if record["status"] != "expired":
            group["monthly_cost"][currency] = round(
                group["monthly_cost"].get(currency, 0) + amount, 2,
            )
        rows.append({
            "id": record["id"], "vendor": vendor,
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


def _add_cost(target: dict[str, float], source: dict[str, float]) -> None:
    for currency, amount in source.items():
        target[currency] = round(target.get(currency, 0) + float(amount or 0), 2)


def build_member_costs(records: list[dict], users: dict, days: dict,
                       month: str | None = None, on: date | None = None) -> dict:
    """Split each linked subscription's monthly fee across members by usage.

    Weight is the member's share of the channel's monthly tokens (requests when
    the channel recorded no tokens): a plan's quota is consumed by volume, so
    tokens are the closest proxy and requests keep request-based plans usable.
    Only non-expired subscriptions with linked channels are allocated; the
    rest (no channel, no usage, or traffic sent without a member key) stays in
    `unallocated` so `allocated + unallocated` reconciles with
    `currency_totals`.
    """
    on = on or date.today()
    year, mon, prefix = _parse_month(month, on)

    channel_totals: dict[str, dict] = {}
    active_days = 0
    for day, buckets in (days or {}).items():
        if not str(day).startswith(prefix) or not isinstance(buckets, dict):
            continue
        active_days += 1
        for name, values in buckets.items():
            if isinstance(values, dict):
                _add(channel_totals.setdefault(str(name), _zero()), values)

    members: dict[str, dict] = {}
    for name, bucket in (users or {}).items():
        if not isinstance(bucket, dict):
            continue
        for day, entry in bucket.items():
            if not isinstance(entry, dict) or not str(day).startswith(prefix):
                continue
            row = members.setdefault(str(name), {
                "name": str(name), "requests": 0, "tokens": 0, "channels": {},
            })
            row["requests"] += int(entry.get("requests") or 0)
            row["tokens"] += int(entry.get("tokens") or 0)
            for channel, usage in (entry.get("channels") or {}).items():
                if not isinstance(usage, dict):
                    continue
                agg = row["channels"].setdefault(str(channel), {"requests": 0, "tokens": 0})
                agg["requests"] += int(usage.get("requests") or 0)
                agg["tokens"] += int(usage.get("tokens") or 0)

    channel_cost: dict[str, dict[str, float]] = {}
    currency_totals: dict[str, float] = {}
    unlinked = []
    for record in (subs.enrich(item, on) for item in records):
        if record["status"] == "expired":
            continue
        equivalent = record.get("monthly_equivalent") or {}
        amount = float(equivalent.get("amount") or 0)
        currency = equivalent.get("currency") or record.get("currency", "CNY")
        currency_totals[currency] = round(currency_totals.get(currency, 0) + amount, 2)
        names = [str(name) for name in (record.get("channels") or [])]
        if not names:
            unlinked.append({"plan": record["plan"], "account": record["account"],
                             "monthly": {currency: round(amount, 2)}})
            continue
        for name in names:
            bucket = channel_cost.setdefault(name, {})
            bucket[currency] = round(bucket.get(currency, 0) + amount, 2)

    weights = {
        name: ("tokens", float(values.get("tokens") or 0))
        if values.get("tokens") else ("requests", float(values.get("requests") or 0))
        for name, values in channel_totals.items()
    }
    rows = []
    allocated: dict[str, float] = {}
    for member in members.values():
        cost: dict[str, float] = {}
        channels = []
        for name, usage in member["channels"].items():
            mode, total = weights.get(name, ("requests", 0.0))
            share = float(usage.get(mode) or 0)
            fees = channel_cost.get(name) or {}
            part = ({currency: round(amount * share / total, 2)
                     for currency, amount in fees.items()}
                    if fees and total > 0 and share > 0 else {})
            _add_cost(cost, part)
            _add_cost(allocated, part)
            channels.append({"name": name, **usage, "cost": part})
        channels.sort(key=lambda item: -item["requests"])
        rows.append({**member, "cost": cost, "channels": channels})
    rows.sort(key=lambda item: (-item["requests"], item["name"]))

    channels = []
    for name, values in channel_totals.items():
        mode, total = weights[name]
        fees = channel_cost.get(name) or {}
        channels.append({
            "name": name, **values, "cost": fees,
            "mode": mode if fees else "",
            "unit_weight": total if fees else 0,
        })
    channels.sort(key=lambda item: -item["requests"])
    return {
        "month": f"{year:04d}-{mon:02d}",
        "today": on.isoformat(),
        "active_days": active_days,
        "currency_totals": currency_totals,
        "allocated": allocated,
        "unallocated": {currency: round(amount - allocated.get(currency, 0), 2)
                        for currency, amount in currency_totals.items()},
        "unlinked": unlinked,
        "users": rows,
        "channels": channels,
        "note": "分摊口径：按成员在渠道当月 tokens 占比折算订阅月费（该渠道无 tokens 时按请求数）；"
                "未关联订阅的渠道不计费，无成员用量的渠道月费与不带 key 的调用计入未分摊",
    }
