"""facet-ark-quota: V4 signing, row mapping, paging and CLI contract."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from planrouter import ark_quota
from planrouter import freealign
from planrouter import providers as providers_mod


AK, SK = "AKLTfixture", "SKfixture"
BODY = '{"WithFreeUsage":true,"PageNumber":1,"PageSize":100}'
QUERY = "Action=ListModelActivations&Version=2024-01-01"
HORIZON = (date.today() + timedelta(days=ark_quota.EXPIRE_DAYS)).isoformat()

EXPECTED_MODELS = ["kimi-k3-1-pro-261024", "deepseek-v4-1-flash-economy-261017"]


def ark_items() -> list:
    return [
        # Initial grant partially consumed → 3,800,000 left.
        {"FoundationModelName": "kimi-k3-1-pro-261024", "State": "Available",
         "InitialInferenceFreeUsage": {"Total": 5_000_000, "Consumed": 1_200_000},
         "FreeResourcePackItems": []},
        # Initial grant exhausted; reward pack carries the remaining tokens.
        {"FoundationModelName": "deepseek-v4-1-flash-economy-261017",
         "State": "Available",
         "InitialInferenceFreeUsage": {"Total": 1000, "Consumed": 1000},
         "FreeResourcePackItems": [
             {"Type": "FreeInference", "Total": 5_000_000, "Consumed": 2_000_000,
              "Reclaimed": 500_000, "SyncTime": "2026-11-08T16:00:00Z"},
             # DataPermission packs grant no inference tokens.
             {"Type": "DataPermission", "Total": 1, "Consumed": 0,
              "Reclaimed": 0, "SyncTime": "2026-11-08T16:00:00Z"},
         ]},
        # Deactivated model → dropped.
        {"FoundationModelName": "doubao-seed-evolving", "State": "Unavailable",
         "InitialInferenceFreeUsage": {"Total": 5_000_000, "Consumed": 0}},
        # Everything consumed/reclaimed → dropped.
        {"FoundationModelName": "doubao-1.5-pro-32k", "State": "Available",
         "InitialInferenceFreeUsage": {"Total": 500_000, "Consumed": 500_000},
         "FreeResourcePackItems": [
             {"Type": "FreeInference", "Total": 500_000, "Consumed": 500_000,
              "Reclaimed": 100_000}]},
    ]


def expected_rows() -> list[dict]:
    return [
        {"model": "kimi-k3-1-pro-261024", "type": "Text",
         "remaining": 3_800_000, "expires": HORIZON},
        {"model": "deepseek-v4-1-flash-economy-261017", "type": "Text",
         "remaining": 2_500_000, "expires": HORIZON},
    ]


class _FakeResp:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CanonicalQueryTests(unittest.TestCase):
    def test_sorts_and_uri_encodes(self):
        self.assertEqual(
            ark_quota.canonical_query(
                {"Version": "2024-01-01", "Action": "ListModelActivations"}),
            QUERY)
        self.assertEqual(ark_quota.canonical_query({"b": "a b", "a": 1}),
                         "a=1&b=a%20b")


class SignTests(unittest.TestCase):
    def test_authorization_header_shape(self):
        auth = ark_quota.sign_request(AK, SK, BODY, "20240710T042925Z", QUERY)
        self.assertTrue(auth.startswith(
            f"HMAC-SHA256 Credential={AK}/20240710/cn-beijing/ark/request, "
            "SignedHeaders=host;x-content-sha256;x-date, Signature="))
        self.assertRegex(auth.rsplit("Signature=", 1)[1], r"^[0-9a-f]{64}$")

    def test_deterministic_and_input_sensitive(self):
        base = ark_quota.sign_request(AK, SK, BODY, "20240710T042925Z", QUERY)
        self.assertEqual(base, ark_quota.sign_request(
            AK, SK, BODY, "20240710T042925Z", QUERY))
        self.assertNotEqual(base, ark_quota.sign_request(
            AK, SK, BODY, "20240711T042925Z", QUERY))
        self.assertNotEqual(base, ark_quota.sign_request(
            AK, SK, BODY + " ", "20240710T042925Z", QUERY))
        self.assertNotEqual(base, ark_quota.sign_request(
            "AKLTother", SK, BODY, "20240710T042925Z", QUERY))

    def test_matches_spec_recomputation(self):
        """Cross-check against a step-by-step V4 recomputation from the doc."""
        x_date = "20240710T042925Z"
        hashed_body = hashlib.sha256(BODY.encode()).hexdigest()
        # CanonicalHeaders ends with '\n', so the template's own separator
        # forms a blank line before SignedHeaders (documented behaviour).
        canonical_request = (
            "POST\n/\n" + QUERY + "\n"
            f"host:{ark_quota.HOST}\n"
            f"x-content-sha256:{hashed_body}\n"
            f"x-date:{x_date}\n"
            "\n"
            "host;x-content-sha256;x-date\n" + hashed_body)
        string_to_sign = "\n".join([
            "HMAC-SHA256", x_date, "20240710/cn-beijing/ark/request",
            hashlib.sha256(canonical_request.encode()).hexdigest()])
        key = hmac.new(SK.encode(), b"20240710", hashlib.sha256).digest()
        for part in (b"cn-beijing", b"ark", b"request"):
            key = hmac.new(key, part, hashlib.sha256).digest()
        expected = hmac.new(key, string_to_sign.encode(),
                            hashlib.sha256).hexdigest()
        auth = ark_quota.sign_request(AK, SK, BODY, x_date, QUERY)
        self.assertTrue(auth.endswith(f"Signature={expected}"))


class RowsTests(unittest.TestCase):
    def test_maps_usable_models_in_api_order(self):
        self.assertEqual(ark_quota.rows_from_items(ark_items()),
                         expected_rows())

    def test_rows_satisfy_the_freealign_contract(self):
        rows = ark_quota.rows_from_items(ark_items())
        self.assertEqual(freealign.free_models_from_rows(rows),
                         EXPECTED_MODELS)

    def test_empty_and_malformed_items(self):
        self.assertEqual(ark_quota.rows_from_items([]), [])
        self.assertEqual(ark_quota.rows_from_items([None, 1, {}]), [])


class PagingTests(unittest.TestCase):
    @staticmethod
    def _urlopen(calls: list, bodies: list, urls: list):
        big = [{"FoundationModelName": f"m{i}", "State": "Available"}
               for i in range(ark_quota.PAGE_SIZE)]
        def urlopen(req, timeout=None):
            urls.append(req.full_url)
            body = json.loads(req.data.decode("utf-8"))
            bodies.append(body)
            calls.append(int(body["PageNumber"]))
            return _FakeResp({"Items": big if body["PageNumber"] == 1 else
                              [{"FoundationModelName": "tail",
                                "State": "Available"}]})
        return urlopen

    def test_stops_on_short_batch_and_carries_body(self):
        calls, bodies, urls = [], [], []
        items = ark_quota.list_items(
            "AK", "SK", _urlopen=self._urlopen(calls, bodies, urls))
        self.assertEqual(calls, [1, 2])
        self.assertEqual(items[-1]["FoundationModelName"], "tail")
        self.assertEqual(bodies[0]["WithFreeUsage"], True)
        self.assertEqual(bodies[0]["PageSize"], ark_quota.PAGE_SIZE)
        # Action/Version travel in the query string.
        self.assertEqual(
            urllib.parse.urlparse(urls[0]).query,
            "Action=ListModelActivations&Version=2024-01-01")

    def test_headers_carry_signature_and_date(self):
        captured = {}
        def spy(req, timeout=None):
            if not captured:
                captured.update(req.headers)
            # Short first page → single request, headers stay page-1's.
            return _FakeResp({"Items": [{"FoundationModelName": "tail",
                                         "State": "Available"}]})
        ark_quota.list_items(AK, SK, _urlopen=spy)
        headers = {k.lower(): v for k, v in captured.items()}
        self.assertTrue(headers["authorization"].startswith(
            f"HMAC-SHA256 Credential={AK}/"))
        self.assertRegex(headers["x-date"], r"^\d{8}T\d{6}Z$")
        self.assertEqual(headers["host"], ark_quota.HOST)
        self.assertEqual(headers["x-content-sha256"],
                         hashlib.sha256(
                             json.dumps({"WithFreeUsage": True,
                                         "PageNumber": 1,
                                         "PageSize": ark_quota.PAGE_SIZE},
                                        separators=(",", ":"),
                                        ensure_ascii=False)
                             .encode("utf-8")).hexdigest())

    def test_api_error_raises(self):
        def urlopen(req, timeout=None):
            return _FakeResp({"ResponseMetadata": {"Error": {
                "Code": "SignatureDoesNotMatch", "Message": "bad"}}})
        with self.assertRaises(RuntimeError):
            ark_quota.list_items(AK, SK, _urlopen=urlopen)

    def test_missing_items_raises(self):
        with self.assertRaises(RuntimeError):
            ark_quota.extract_items({"Result": {"Total": 0}})
        self.assertEqual(ark_quota.extract_items(
            {"Result": {"Items": [1]}}), [1])


class CliTests(unittest.TestCase):
    def run_main(self, argv=None, env=None, list_items=None):
        out, err = io.StringIO(), io.StringIO()
        if env is None:
            env = {ark_quota.AK_ENV: AK, ark_quota.SK_ENV: SK}
        if argv is None:
            argv = ["--output", "json"]
        with mock.patch.dict(os.environ, env):
            patches = []
            if list_items is not None:
                patches.append(mock.patch.object(
                    ark_quota, "list_items", list_items))
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    rc = ark_quota.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_success_prints_freealign_rows(self):
        rc, out, err = self.run_main(
            list_items=lambda *a, **kw: ark_items())
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out), expected_rows())

    def test_output_feeds_the_freealign_sweep(self):
        rc, out, _ = self.run_main(list_items=lambda *a, **kw: ark_items())
        self.assertEqual(
            freealign.free_models_from_rows(
                freealign.parse_free_quota_output(out)),
            EXPECTED_MODELS)

    def test_missing_credentials_exit_2_with_stderr_hint(self):
        rc, out, err = self.run_main(
            env={ark_quota.AK_ENV: "", ark_quota.SK_ENV: "   "})
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn(ark_quota.AK_ENV, err)
        self.assertIn(ark_quota.SK_ENV, err)

    def test_api_failure_exit_1_with_stderr_detail(self):
        def broken(*args, **kwargs):
            raise RuntimeError("HTTP 401：SignatureDoesNotMatch")
        rc, out, err = self.run_main(list_items=broken)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("方舟免费额度查询失败", err)
        self.assertIn("SignatureDoesNotMatch", err)


class RegistryTests(unittest.TestCase):
    def test_builtin_registry_carries_the_cli_command(self):
        volc = next(p for p in providers_mod.load_providers()
                    if p.get("id") == "volcengine")
        self.assertEqual(volc["modes"]["payg"]["free_quota_cmd"],
                         ["facet-ark-quota", "--output", "json"])

    def test_overlay_missing_cmd_inherits_builtin(self):
        overlay = {"providers": [{
            "id": "volcengine", "name": "火山引擎方舟",
            "modes": {"payg": {
                "label": "按量付费", "protocol": "openai",
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "tier": "free", "models": [],
            }},
        }]}
        with tempfile.TemporaryDirectory() as config_dir:
            Path(config_dir, "providers.json").write_text(
                json.dumps(overlay, ensure_ascii=False), encoding="utf-8")
            merged = providers_mod.load_providers(config_dir)
        volc = next(p for p in merged if p.get("id") == "volcengine")
        self.assertEqual(volc["modes"]["payg"]["free_quota_cmd"],
                         ["facet-ark-quota", "--output", "json"])


if __name__ == "__main__":
    unittest.main()
