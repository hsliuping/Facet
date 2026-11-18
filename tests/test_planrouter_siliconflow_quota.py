"""facet-siliconflow-quota: free-model rows and usage-card parsing."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from facet.gateway import siliconflow_quota as sfq
from facet.gateway.core import FREE_RESERVE_DEFAULT_TOKENS
from facet.gateway.freealign import free_models_from_rows


class RowsTests(unittest.TestCase):
    def test_rows_pass_the_free_align_filters(self):
        rows = sfq.rows_from_models()
        self.assertEqual([r["model"] for r in rows], list(sfq.FREE_MODELS))
        for row in rows:
            self.assertEqual(row["type"], "Text")
            # The sentinel allowance must survive both the sweep's reserve
            # margin and the console probe path (reserve=0).
            self.assertGreater(row["remaining"], FREE_RESERVE_DEFAULT_TOKENS)
            self.assertGreaterEqual(row["expires"], "2026-01-01")
        self.assertEqual(
            free_models_from_rows(rows, reserve=FREE_RESERVE_DEFAULT_TOKENS),
            list(sfq.FREE_MODELS))

    def test_cli_prints_bare_json_array(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sfq.main(["--output", "json"])
        self.assertEqual(code, 0)
        rows = json.loads(out.getvalue())
        self.assertEqual([r["model"] for r in rows], list(sfq.FREE_MODELS))


class FetchBalanceTests(unittest.TestCase):
    @staticmethod
    def _urlopen_with(payload):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        return lambda req, timeout: Resp()

    def test_summary_reports_gift_charge_total(self):
        payload = {"code": 20000, "status": True, "message": "OK",
                   "data": {"id": "u1", "name": "alice",
                            "balance": "0.88", "chargeBalance": "88.00",
                            "totalBalance": "88.88"}}
        with mock.patch.object(sfq.urllib.request, "urlopen",
                               self._urlopen_with(payload)):
            with mock.patch.object(sfq, "resolve_secret", lambda v: "sk-test"):
                report = sfq.fetch_balance("sk-test")
        self.assertEqual(report["provider"], "siliconflow")
        self.assertEqual(report["windows"], [])
        self.assertIn("赠金 ¥0.88", report["summary"])
        self.assertIn("充值 ¥88", report["summary"])
        self.assertIn("合计 ¥88.88", report["summary"])

    def test_total_falls_back_to_gift_plus_charge(self):
        payload = {"code": 20000, "data": {"balance": "1.50",
                                           "chargeBalance": "2.50"}}
        with mock.patch.object(sfq.urllib.request, "urlopen",
                               self._urlopen_with(payload)):
            with mock.patch.object(sfq, "resolve_secret", lambda v: "sk-test"):
                report = sfq.fetch_balance("sk-test")
        self.assertIn("合计 ¥4", report["summary"])

    def test_missing_token_raises(self):
        with mock.patch.object(sfq, "resolve_secret", lambda v: ""):
            with self.assertRaises(RuntimeError):
                sfq.fetch_balance("")

    def test_api_error_raises(self):
        payload = {"code": 40100, "message": "Invalid token"}
        with mock.patch.object(sfq.urllib.request, "urlopen",
                               self._urlopen_with(payload)):
            with mock.patch.object(sfq, "resolve_secret", lambda v: "sk-bad"):
                with self.assertRaises(RuntimeError):
                    sfq.fetch_balance("sk-bad")


if __name__ == "__main__":
    unittest.main()
