"""TokenHub management-plane free-model rows: signing, pagination, filtering."""
from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from facet.gateway import tokenhub_quota as tq


def catalog_models() -> list[dict]:
    """Catalog fixture shaped like DescribeModelList's ModelSet."""
    return [
        # Token-billed free chat models (语言 + 多模态理解) → kept.
        {"ModelId": "deepseek-v4", "ModelType": "Text", "Tags": ["文本生成"],
         "FreeTrialInfo": {"CapacitySize": 1000000, "Unit": "token"}},
        {"ModelId": "hy-vision-2.0-instruct", "ModelType": "Vision",
         "Tags": ["视觉理解"],
         "FreeTrialInfo": {"CapacitySize": 1000000, "Unit": "token"}},
        # Credits-billed generation (Unit 不是 token) → dropped.
        {"ModelId": "hy-image-2.0", "ModelType": "Multimodal",
         "Tags": ["图像生成"],
         "FreeTrialInfo": {"CapacitySize": 50, "Unit": "张"}},
        # Generation tags with a token unit still → dropped.
        {"ModelId": "kling-video-v4", "ModelType": "Multimodal",
         "Tags": ["视频生成"],
         "FreeTrialInfo": {"CapacitySize": 1000000, "Unit": "token"}},
        # Speech/Embedding never serve /chat/completions → dropped.
        {"ModelId": "hy-tts", "ModelType": "Speech", "Tags": [],
         "FreeTrialInfo": {"CapacitySize": 1000000, "Unit": "token"}},
        {"ModelId": "bge-x", "ModelType": "Embedding", "Tags": [],
         "FreeTrialInfo": {"CapacitySize": 1000000, "Unit": "token"}},
        # Paid-only model without any trial pack → dropped.
        {"ModelId": "deepseek-v4-max", "ModelType": "Text", "Tags": []},
    ]


class _FakeResponse:
    """Minimal urlopen-compatible response carrying one JSON payload."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(pages: list[dict]):
    """_urlopen stub serving one canned response per call, in order."""
    calls = {"n": 0}

    def opener(req, timeout=None):
        body = pages[min(calls["n"], len(pages) - 1)]
        calls["n"] += 1
        return _FakeResponse(body)

    opener.calls = calls
    return opener


class RowsTests(unittest.TestCase):
    def test_keeps_token_billed_chat_models(self):
        rows = tq.rows_from_models(catalog_models(), today="2026-11-16")
        self.assertEqual([r["model"] for r in rows],
                         ["deepseek-v4", "hy-vision-2.0-instruct"])
        row = rows[0]
        self.assertEqual(row["type"], "Text")
        self.assertEqual(row["remaining"], 1000000)
        self.assertEqual(row["total"], 1000000)
        self.assertEqual(row["consumed"], 0)
        # 90-day trial horizon synthesized at the fixed "today".
        self.assertEqual(row["expires"], "2027-02-14")

    def test_dedupes_and_ignores_non_dict(self):
        rows = tq.rows_from_models(
            [catalog_models()[0], catalog_models()[0], "junk", None],
            today="2026-11-16")
        self.assertEqual([r["model"] for r in rows], ["deepseek-v4"])

    def test_zero_capacity_not_emitted(self):
        model = dict(catalog_models()[0])
        model["FreeTrialInfo"] = {"CapacitySize": 0, "Unit": "token"}
        self.assertEqual(tq.rows_from_models([model]), [])


class ListModelsTests(unittest.TestCase):
    def pages_until_total_covered_fixture(self) -> list[dict]:
        return [
            {"Response": {"ModelSet": catalog_models()[:2], "TotalCount": 3}},
            {"Response": {"ModelSet": catalog_models()[2:3], "TotalCount": 3}},
        ]

    def test_pages_until_total_covered(self):
        opener = fake_urlopen(self.pages_until_total_covered_fixture())
        models = tq.list_models("ak", "sk", "ap-guangzhou", _urlopen=opener)
        self.assertEqual(len(models), 3)
        self.assertEqual(opener.calls["n"], 2)

    def test_stops_on_empty_page(self):
        opener = fake_urlopen([{"Response": {"ModelSet": [],
                                             "TotalCount": 99}}])
        self.assertEqual(
            tq.list_models("ak", "sk", "ap-guangzhou", _urlopen=opener), [])

    def test_api_error_raises_runtime_error(self):
        opener = fake_urlopen([{"Response": {"Error": {
            "Code": "AuthFailure", "Message": "签名错误"}}}])
        with self.assertRaises(RuntimeError) as ctx:
            tq.list_models("ak", "sk", "ap-guangzhou", _urlopen=opener)
        self.assertIn("AuthFailure", str(ctx.exception))


class SignTests(unittest.TestCase):
    def test_header_shape_and_scope(self):
        header = tq.sign_request("AKIDtest", "secret", "DescribeModelList",
                                 '{"Limit":100}', 1760000000)
        self.assertTrue(header.startswith("TC3-HMAC-SHA256 "))
        self.assertIn("Credential=AKIDtest/2025-10-09/tokenhub/tc3_request",
                      header)
        self.assertIn("SignedHeaders=content-type;host;x-tc-action", header)
        sig = header.rsplit("Signature=", 1)[1]
        self.assertEqual(len(sig), 64)
        int(sig, 16)  # must be valid hex

    def test_signature_depends_on_body_and_key(self):
        base = tq.sign_request("ak", "sk1", "DescribeModelList", "{}",
                               1760000000)
        self.assertNotEqual(
            base, tq.sign_request("ak", "sk2", "DescribeModelList", "{}",
                                  1760000000))
        self.assertNotEqual(
            base, tq.sign_request("ak", "sk1", "DescribeModelList",
                                  '{"x":1}', 1760000000))


class CliTests(unittest.TestCase):
    @staticmethod
    def clean_env(extra: dict) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in (tq.ID_ENV, tq.KEY_ENV)}
        env.update(extra)
        return env

    def test_missing_creds_exit_2_with_hint(self):
        with mock.patch.dict(os.environ, self.clean_env({}), clear=True):
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                code = tq.main(["--output", "json"])
        self.assertEqual(code, 2)
        self.assertEqual(buf_out.getvalue().strip(), "")
        self.assertIn("TENCENT_SECRET_ID", buf_err.getvalue())
        self.assertIn("访问管理", buf_err.getvalue())

    def test_success_prints_rows(self):
        with mock.patch.dict(os.environ,
                             self.clean_env({tq.ID_ENV: "ak",
                                             tq.KEY_ENV: "sk"}), clear=True):
            with mock.patch.object(tq, "list_models",
                                   return_value=catalog_models()):
                buf_out, buf_err = io.StringIO(), io.StringIO()
                with redirect_stdout(buf_out), redirect_stderr(buf_err):
                    code = tq.main(["--output", "json"])
        self.assertEqual(code, 0)
        rows = json.loads(buf_out.getvalue())
        self.assertEqual([r["model"] for r in rows],
                         ["deepseek-v4", "hy-vision-2.0-instruct"])

    def test_api_failure_exit_1(self):
        with mock.patch.dict(os.environ,
                             self.clean_env({tq.ID_ENV: "ak",
                                             tq.KEY_ENV: "sk"}), clear=True):
            with mock.patch.object(tq, "list_models",
                                   side_effect=RuntimeError("HTTP 401")):
                buf_out, buf_err = io.StringIO(), io.StringIO()
                with redirect_stdout(buf_out), redirect_stderr(buf_err):
                    code = tq.main([])
        self.assertEqual(code, 1)
        self.assertIn("TokenHub", buf_err.getvalue())
