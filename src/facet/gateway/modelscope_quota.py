"""ModelScope (魔搭) magicubes (魔粒): the daily grant and the balance snapshot.

ModelScope publishes no check-in API, but a signed-in ``GET
/api/v1/users/login/info`` counts as the day's first website sign-in and lands
the daily grant (``daily_active`` 200 magicubes, plus ``aliyun_bindlogin`` 50
when an Aliyun account is bound). Magicubes last one day — they expire at the
next grant instead of accumulating — and re-signing in the same day grants
nothing more, so the flow is: read the ledger, sign in only when today's grant
is missing, then confirm on the ledger.

The SDK token is credentials for both planes at once: the inference
``Authorization: Bearer`` and the web session ``Cookie: m_session_id=ms-…``.
So a channel's existing api_key is the only credential needed — no browser,
no password, no cookie harvesting. The endpoint contract was first written up
by the magpie-plugin-msmoli project. This is for your own account only:
automation is outside ModelScope's terms, so the platform may act on it at
account level (see the console notice).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .core import resolve_secret

_HOST = "https://www.modelscope.cn"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_BEIJING = timezone(timedelta(hours=8))
# Ledger rule_keys that mean "today's login grant landed".
_GRANT_RULES = ("daily_active", "aliyun_bindlogin")


def _get(path: str, token: str) -> dict:
    """One cookie-authenticated GET against the website plane."""
    req = urllib.request.Request(
        _HOST + path,
        headers={"Cookie": f"m_session_id={token}", "User-Agent": _UA,
                 "Referer": _HOST + "/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except Exception as exc:
        raise RuntimeError(str(exc) or exc.__class__.__name__) from exc


def _ledger(token: str) -> list:
    """Latest magicube transactions, newest first."""
    data = (_get("/openapi/v1/magicubes/transactions?page=1&page_size=50",
                 token) or {}).get("data") or {}
    rows = data.get("records")
    if not isinstance(rows, list):
        raise RuntimeError("transactions response has no records")
    return rows


def _today_grant(rows: list, today: str) -> tuple:
    """(earned, spent, grant_landed, expire_hint) for one Beijing day."""
    earned = spent = 0.0
    landed = False
    hint = ""
    for row in rows:
        if not isinstance(row, dict) or row.get("gmt_created") != today:
            continue
        amount = float(row.get("total_amount") or 0)
        kind = str(row.get("type") or "")
        if kind == "EARN":
            earned += amount
            if str(row.get("rule_key") or "") in _GRANT_RULES:
                landed = True
                expire = str(row.get("expire_at") or "")
                if expire:
                    # '2026-11-18 08:40:01' -> '11-18 08:40'
                    day, _, clock = expire.partition(" ")
                    hint = f"{day[5:]} {clock[:5]}"
        elif kind == "SPEND_CONFIRM":
            spent += amount
    return earned, spent, landed, hint


def _balance(token: str) -> float:
    data = (_get("/openapi/v1/magicubes/balance", token) or {}).get("data") or {}
    if "available_balance" not in data:
        raise RuntimeError("balance response has no available_balance")
    return float(data.get("available_balance") or 0)


def fetch_balance(api_key: str) -> dict:
    """Snapshot for the channel-usage card: balance plus today's ledger."""
    token = resolve_secret(api_key) or ""
    if not token:
        raise RuntimeError("no api_key")
    balance = _balance(token)
    today = datetime.now(_BEIJING).strftime("%Y-%m-%d")
    earned, spent, landed, hint = _today_grant(_ledger(token), today)
    parts = [f"魔粒 {balance:g}", f"今日领 {earned:g}", f"今日用 {spent:g}"]
    if hint:
        parts.append(f"{hint} 过期")
    if not landed:
        parts.append("今日未领")
    return {"plan": "魔搭 ModelScope", "provider": "modelscope",
            "windows": [], "summary": " · ".join(parts)}


def checkin(api_key: str, confirm_seconds: float = 60.0) -> dict:
    """Claim today's magicube grant. Idempotent per Beijing day.

    Returns {"outcome": "done" | "claimed" | "inactive", "detail", "granted",
    "balance", "user"}; raises RuntimeError when the sign-in itself fails.
    "done" = today's grant was already in the ledger (nothing was sent),
    "claimed" = this run's sign-in landed it, "inactive" = the sign-in went
    through but no grant appeared within the confirm window.
    """
    token = resolve_secret(api_key) or ""
    if not token:
        raise RuntimeError("no api_key")
    today = datetime.now(_BEIJING).strftime("%Y-%m-%d")

    earned, spent, landed, _hint = _today_grant(_ledger(token), today)
    if landed:
        return {"outcome": "done", "granted": earned, "spent": spent,
                "balance": _balance(token), "user": "",
                "detail": f"today's grant already claimed ({earned:g})"}

    info = _get("/api/v1/users/login/info", token)
    user = str((info.get("Data") or {}).get("Name") or "")
    if info.get("Code") != 200 or not user:
        raise RuntimeError(f"login/info: {info.get('Message') or info.get('Code')}")
    # The grant lands with the sign-in itself; poll the ledger until it shows.
    deadline = time.time() + confirm_seconds
    wait = 3.0
    while True:
        time.sleep(wait)
        earned, spent, landed, _hint = _today_grant(_ledger(token), today)
        if landed:
            return {"outcome": "claimed", "granted": earned, "spent": spent,
                    "balance": _balance(token), "user": user,
                    "detail": f"claimed {earned:g} magicubes as {user}"}
        if time.time() >= deadline:
            return {"outcome": "inactive", "granted": None, "spent": spent,
                    "balance": None, "user": user,
                    "detail": f"signed in as {user} but no grant appeared yet"}
        wait = 15.0
