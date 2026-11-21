"""ModelScope quota fetcher: request shape and the injected timeout/urlopen.

Every vendor fetcher accepts (timeout, _urlopen) so callers can bound the
request and tests can inject responses; this module had hardcoded
urllib.request.urlopen and a fixed 30s timeout, making it the odd one out
and its requests untestable.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta

from facet.gateway import modelscope_quota

_BALANCE_PATH = "/openapi/v1/magicubes/balance"
_LEDGER_PATH = "/openapi/v1/magicubes/transactions"


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(routes: dict):
    """_urlopen stub: routes by path prefix, records the request shape."""
    calls = []

    def opener(req, timeout=None):
        path = req.full_url[len(modelscope_quota._HOST):].split("?")[0]
        for prefix, payload in routes.items():
            if path.startswith(prefix):
                calls.append({"path": path, "timeout": timeout})
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected request: {req.full_url}")

    opener.calls = calls
    return opener


def ledger_rows() -> list:
    """A grant that landed today, expiring tomorrow: counted as available."""
    now = datetime.now(modelscope_quota._BEIJING)
    return [{
        "gmt_created": now.strftime("%Y-%m-%d"),
        "type": "EARN", "total_amount": 200, "rule_key": "daily_active",
        "expire_at": (now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
    }]


def routes_for(balance=88.0, rows=None) -> dict:
    return {_BALANCE_PATH: {"data": {"available_balance": balance}},
            _LEDGER_PATH: {"data": {"records": rows if rows is not None
                                    else ledger_rows()}}}


class FetchBalanceTests(unittest.TestCase):
    def test_summary_reads_both_endpoints_through_the_injected_opener(self):
        opener = opener_for(routes_for())
        plan = modelscope_quota.fetch_balance("ms-token", _urlopen=opener)
        self.assertEqual(plan["provider"], "modelscope")
        self.assertEqual(plan["plan"], "魔搭 ModelScope")
        self.assertIn("魔粒 88", plan["summary"])
        self.assertIn("今日领 200", plan["summary"])
        self.assertEqual([c["path"] for c in opener.calls],
                         [_BALANCE_PATH, _LEDGER_PATH])
        for call in opener.calls:
            self.assertEqual(call["timeout"], 30.0)

    def test_timeout_is_passed_through(self):
        opener = opener_for(routes_for())
        modelscope_quota.fetch_balance("ms-token", timeout=4.5,
                                       _urlopen=opener)
        self.assertEqual({c["timeout"] for c in opener.calls}, {4.5})


class CheckinTests(unittest.TestCase):
    def test_done_path_reads_only_two_endpoints(self):
        # Grants already landed for today: no sign-in request, no polling.
        opener = opener_for(routes_for())
        result = modelscope_quota.checkin("ms-token", _urlopen=opener)
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(result["granted"], 200)
        self.assertEqual([c["path"] for c in opener.calls],
                         [_LEDGER_PATH, _BALANCE_PATH])
        for call in opener.calls:
            self.assertEqual(call["timeout"], 30.0)
