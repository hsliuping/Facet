#!/usr/bin/env python3
"""Daily ModelScope (魔搭) 魔粒 check-in.

Mechanism — ModelScope publishes no check-in button API; the daily grant
is a side effect of a real website sign-in:

  * GET https://www.modelscope.cn/api/v1/users/login/info carrying the
    browser session cookie (Cookie: m_session_id=ms-..., a browser UA and
    a Referer are enough, no CSRF token) counts as today's sign-in. The
    first hit of each Beijing day grants daily_active 200 + aliyun_bindlogin
    50 魔粒; both expire 24h after they land, so a morning claim is fully
    usable. Repeating the call the same day grants nothing more.
  * Balance:  GET /openapi/v1/magicubes/balance
  * Ledger:   GET /openapi/v1/magicubes/transactions?page=1&page_size=50
    EARN rows come newest-first; one page reaches back about a fortnight.

The ledger is read around the touch to tell "claimed now" from "already
claimed today": outcomes are claimed / done / inactive / failed.

The run waits for a per-day deterministic minute inside 08:30-10:00 Beijing
(hash of date+token), so the traffic looks like a person opening the site
over breakfast rather than a cron at 00:00. Schedule the task daily at
08:30 and let the script sleep to its own minute; --now skips the wait.

Risk note: ModelScope's user agreement discourages third-party automation
of the site (and of 魔粒 earning in particular). This script only touches
login/info once a day plus two read-only quota endpoints, but the judgement
about your own (real-name verified) account is yours.

Usage:
    python tools/modelscope_checkin.py             # scheduled form (waits)
    python tools/modelscope_checkin.py --now       # claim right now
    python tools/modelscope_checkin.py --status    # read-only: balance/today
    python tools/modelscope_checkin.py --token ms-xxxx

Token resolution: --token > MODELSCOPE_TOKEN env > the "ms-" api_key of the
modelscope channel in data/router.json / router.example.json /
router.template.json. stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SITE = "https://www.modelscope.cn"
LOGIN_INFO = SITE + "/api/v1/users/login/info"
BALANCE = SITE + "/openapi/v1/magicubes/balance"
LEDGER = SITE + "/openapi/v1/magicubes/transactions?page=1&page_size=50"
LOG_PATH = REPO / "data" / "modelscope-checkin.jsonl"

CST = timezone(timedelta(hours=8))  # Beijing: the vendor's day boundary
WINDOW_START = 8 * 60 + 30           # 08:30, minutes since midnight
WINDOW_END = 10 * 60                  # 10:00
NOON = 12 * 60
RETRY_SLEEP = 1800.0                  # re-read once after 30 min (pre-noon)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_CONFIG_CANDIDATES = ("data/router.json", "router.example.json",
                      "router.template.json")


def find_token() -> str | None:
    env = (os.environ.get("MODELSCOPE_TOKEN") or "").strip()
    if env:
        return env
    for rel in _CONFIG_CANDIDATES:
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for channel in raw.get("channels") or []:
            key = str(channel.get("api_key") or "")
            if key.startswith("ms-"):
                return key
    return None


def get(url: str, token: str) -> tuple[int, dict | list | None]:
    """GET with the session cookie + Bearer (the ms- token is both roles)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": SITE + "/",
        "Accept": "application/json",
        "Cookie": f"m_session_id={token}",
        "Authorization": f"Bearer {token}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, None
    try:
        return 200, json.loads(body)
    except ValueError:
        return 200, None


def _rows(data) -> list[dict]:
    """Dig the transaction list out of whatever envelope the API returns."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return []
    for value in data.values():
        rows = _rows(value)
        if rows:
            return rows
    return []


def _day_of(row: dict) -> str | None:
    """Beijing date of a ledger row, from any plausible timestamp field."""
    for field in ("gmt_created", "gmt_create", "create_time", "created_at",
                  "time", "date", "timestamp"):
        value = row.get(field)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            if value > 1e12:
                value /= 1000.0
            return datetime.fromtimestamp(value, CST).strftime("%Y-%m-%d")
        text = str(value)
        m = re.match(r"\d{4}-\d{2}-\d{2}", text)
        if m:
            return m.group(0)
        try:
            ts = float(text)
        except ValueError:
            continue
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d")
    return None


def today_grants(token: str) -> dict[str, float] | None:
    """rule/type -> amount for today's EARN rows; None if unreadable."""
    status, data = get(LEDGER, token)
    if status != 200 or data is None:
        return None
    day = datetime.now(CST).strftime("%Y-%m-%d")
    grants: dict[str, float] = {}
    for row in _rows(data):
        if str(row.get("type") or "").upper() != "EARN":
            continue
        row_day = _day_of(row)
        if row_day is None:
            return None  # shape changed; do not guess
        if row_day != day:
            continue
        name = str(row.get("rule_key") or row.get("rule") or row.get("reason")
                   or row.get("source") or "earn")
        try:
            amount = row.get("total_amount")
            if amount is None:
                amount = row.get("amount") or 0
            grants[name] = grants.get(name, 0.0) + abs(float(amount))
        except (TypeError, ValueError):
            grants[name] = grants.get(name, 0.0)
    return grants


def balance(token: str):
    status, data = get(BALANCE, token)
    if status != 200 or data is None:
        return None
    if isinstance(data, (int, float, str)):
        return data
    inner = data.get("data") if isinstance(data, dict) else None
    if isinstance(inner, dict):
        for key in ("available_balance", "total_balance", "balance"):
            if isinstance(inner.get(key), (int, float)):
                return inner[key]
    # Envelope shape unknown: return the (small) payload verbatim for display.
    return data


def target_minute(token: str) -> int:
    """Deterministic in-window minute for today; stable across retries."""
    day = datetime.now(CST).strftime("%Y-%m-%d")
    seed = hashlib.sha256(f"{day}|{token[-8:]}".encode()).digest()
    span = max(1, WINDOW_END - WINDOW_START)
    return WINDOW_START + seed[0] % span


def wait_for_window(token: str) -> None:
    now = datetime.now(CST)
    minutes = now.hour * 60 + now.minute
    target = target_minute(token)
    if minutes >= target or minutes >= WINDOW_END:
        return
    delay = (target - minutes) * 60 - now.second
    print(f"[checkin] sleeping {delay / 60:.0f} min to the "
          f"{target // 60:02d}:{target % 60:02d} window minute", flush=True)
    time.sleep(delay)


def log_line(record: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # logging must never break the run


def run(token: str, now_mode: bool) -> int:
    if not now_mode:
        wait_for_window(token)
    before = today_grants(token)
    status, _ = get(LOGIN_INFO, token)  # the touch = the sign-in
    if status not in (200, 201):
        print(f"[checkin] failed: login/info HTTP {status or 'network'}",
              flush=True)
        log_line({"ts": datetime.now(CST).isoformat(), "outcome": "failed",
                  "http": status})
        return 1
    time.sleep(3.0)  # give the grant a moment to land in the ledger
    after = today_grants(token)
    outcome, detail = "inactive", {}
    if after is None or before is None:
        outcome = "unknown"  # ledger unreadable; the touch itself was fine
    else:
        new = {k: v for k, v in after.items() if v > before.get(k, 0.0)}
        if new:
            outcome, detail = "claimed", new
        elif after:
            outcome, detail = "done", after
        elif datetime.now(CST).hour < 12:
            print("[checkin] no grant yet; retrying in 30 min", flush=True)
            time.sleep(RETRY_SLEEP)
            after = today_grants(token) or {}
            new = {k: v for k, v in after.items() if before is None or
                   v > before.get(k, 0.0)}
            if new:
                outcome, detail = "claimed", new
            elif after:
                outcome, detail = "done", after
    bal = balance(token)
    print(f"[checkin] {outcome} {json.dumps(detail, ensure_ascii=False)} "
          f"| balance: {bal}", flush=True)
    log_line({"ts": datetime.now(CST).isoformat(), "outcome": outcome,
              "grants": detail, "balance": bal})
    return 0 if outcome in ("claimed", "done") else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--token", default=None)
    ap.add_argument("--now", action="store_true",
                    help="skip the morning-window wait and run immediately")
    ap.add_argument("--status", action="store_true",
                    help="read-only: balance and today's grants, no touch")
    args = ap.parse_args()
    token = args.token or find_token()
    if not token:
        print("[checkin] no ms- token: set MODELSCOPE_TOKEN or configure the "
              "modelscope channel in the router config", file=sys.stderr)
        return 1
    if args.status:
        print(json.dumps({"balance": balance(token),
                          "today": today_grants(token)}, ensure_ascii=False))
        return 0
    return run(token, args.now)


if __name__ == "__main__":
    sys.exit(main())
