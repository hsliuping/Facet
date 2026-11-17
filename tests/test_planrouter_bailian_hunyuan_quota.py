"""Bailian & Hunyuan usage: wallet signing, CLI parsing, plan wiring."""
from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

from facet.gateway import bailian_quota, hunyuan_quota, zhipu_quota
from facet.gateway import server
from facet.gateway.core import load_config
from facet.gateway.server import App


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


def fake_urlopen(payload: dict):
    """_urlopen stub that records every request and serves one payload."""
    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        return _FakeResponse(payload)

    opener.calls = calls
    return opener


def fake_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    """subprocess.run stub carrying the fields fetch_token_plan reads."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=returncode, stdout=stdout,
                                     stderr=stderr)

    run.calls = calls
    return run


class BailianBalanceTests(unittest.TestCase):
    def test_parses_string_data_and_signs_request(self):
        opener = fake_urlopen({"Code": "Success", "Data": json.dumps({
            "AvailableAmount": "123.45", "AvailableCashAmount": "100.00",
            "CreditAmount": 50, "Currency": "CNY"})})
        plan = bailian_quota.fetch_balance("ak-id", "ak-secret",
                                           _urlopen=opener)
        self.assertEqual(plan["plan"], "阿里云百炼")
        self.assertEqual(plan["provider"], "bailian")
        self.assertEqual(plan["windows"], [])
        self.assertIn("可用余额 ¥123.45", plan["summary"])
        self.assertIn("现金 ¥100.00", plan["summary"])
        self.assertIn("信用额度 ¥50.00", plan["summary"])
        # The published URL is a correctly signed RPC query.
        req = opener.calls[0]
        split = urlsplit(req.full_url)
        self.assertEqual(split.netloc, "business.aliyuncs.com")
        params = dict(parse_qsl(split.query))
        self.assertEqual(params["Action"], "QueryAccountBalance")
        self.assertEqual(params["AccessKeyId"], "ak-id")
        self.assertEqual(params["Version"], "2017-12-14")
        unsigned = {k: v for k, v in params.items() if k != "Signature"}
        self.assertEqual(params["Signature"],
                         bailian_quota._sign_rpc(unsigned, "ak-secret"))

    def test_accepts_object_data(self):
        opener = fake_urlopen({"Code": "Success", "Data": {
            "AvailableAmount": "8.00", "AvailableCashAmount": "8.00"}})
        plan = bailian_quota.fetch_balance("ak", "sk", _urlopen=opener)
        self.assertEqual(plan["summary"], "可用余额 ¥8.00")

    def test_error_code_raises(self):
        opener = fake_urlopen({"Code": "InvalidAccessKeyId.NotFound",
                               "Message": "Specified access key is not found."})
        with self.assertRaises(RuntimeError) as ctx:
            bailian_quota.fetch_balance("ak", "sk", _urlopen=opener)
        self.assertIn("access key is not found", str(ctx.exception))

    def test_missing_amount_raises(self):
        opener = fake_urlopen({"Code": "Success", "Data": {}})
        with self.assertRaises(RuntimeError) as ctx:
            bailian_quota.fetch_balance("ak", "sk", _urlopen=opener)
        self.assertIn("余额字段", str(ctx.exception))


class BailianTokenPlanTests(unittest.TestCase):
    def test_windows_sorted_and_percent_scaled(self):
        run = fake_run(stdout=json.dumps({
            "per1MonthPercentage": 0.5, "per1MonthResetTime": 1790000000000,
            "per5HourPercentage": 0.25, "per5HourResetTime": 1780000000000,
            "hasQuotaData": True}))
        plan = bailian_quota.fetch_token_plan(_run=run)
        self.assertEqual(plan["plan"], "百炼 Token Plan")
        self.assertEqual(plan["provider"], "bailian")
        self.assertEqual([w["level"] for w in plan["windows"]],
                         ["five_hour", "monthly"])
        self.assertEqual(plan["windows"][0]["percent"], 25.0)
        self.assertEqual(plan["windows"][0]["reset_ts"], 1780000000)
        self.assertEqual(plan["windows"][1]["percent"], 50.0)
        self.assertNotIn("summary", plan)
        # The console command is exactly the documented one.
        self.assertIn("token-plan", run.calls[0])
        self.assertIn("json", run.calls[0])

    def test_not_logged_in_passes_message_and_hint(self):
        run = fake_run(stdout=json.dumps({"error": {
            "code": 3, "message": "Console session is not logged in or has expired.",
            "hint": "Run `bl auth login --console` first."}}), returncode=3)
        with self.assertRaises(RuntimeError) as ctx:
            bailian_quota.fetch_token_plan(_run=run)
        self.assertIn("Console session", str(ctx.exception))
        self.assertIn("bl auth login --console", str(ctx.exception))

    def test_missing_cli_hint(self):
        def run(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])
        with self.assertRaises(RuntimeError) as ctx:
            bailian_quota.fetch_token_plan(_run=run)
        self.assertIn("bailian-cli", str(ctx.exception))
        self.assertIn("bl auth login --console", str(ctx.exception))

    def test_timeout(self):
        def run(cmd, **kwargs):
            raise bailian_quota.subprocess.TimeoutExpired(cmd, 60.0)
        with self.assertRaises(RuntimeError) as ctx:
            bailian_quota.fetch_token_plan(timeout=60.0, _run=run)
        self.assertIn("超时", str(ctx.exception))

    def test_no_quota_data_summary(self):
        run = fake_run(stdout=json.dumps({"hasQuotaData": False}))
        plan = bailian_quota.fetch_token_plan(_run=run)
        self.assertEqual(plan["windows"], [])
        self.assertEqual(plan["summary"], "该账号未开通 Token Plan 套餐")

    def test_non_json_output_raises_with_exit_code(self):
        run = fake_run(stdout="", stderr="node: command failed", returncode=2)
        with self.assertRaises(RuntimeError) as ctx:
            bailian_quota.fetch_token_plan(_run=run)
        self.assertIn("2", str(ctx.exception))
        self.assertIn("command failed", str(ctx.exception))


class HunyuanTokenPlanTests(unittest.TestCase):
    PAYLOAD = {"Response": {
        "PackageStatus": "ACTIVE", "AutoRenewFlag": 1,
        "StartTime": "2026-04-05 21:06:09",
        "ExpireTime": "2027-04-05 21:06:09",
        "PrepayInquiryKey": "sp_abc_standard",
        "TotalUsed": 120, "TotalCredits": 9360, "CycleCredits": 1560,
        "CurrentCycle": 1, "TotalCycles": 6, "RemainCycles": 6}}

    def test_payload_request_and_snapshot(self):
        opener = fake_urlopen(self.PAYLOAD)
        plan = hunyuan_quota.fetch_token_plan("id", "key", _urlopen=opener)
        self.assertEqual(plan["plan"], "混元 Token Plan · Standard")
        self.assertEqual(plan["provider"], "hunyuan")
        self.assertEqual(plan["end_time"], "2027-04-05")
        self.assertTrue(plan["auto_renew"])
        window = plan["windows"][0]
        self.assertEqual(window["level"], "monthly")
        self.assertEqual(window["quota"], 9360)
        self.assertEqual(window["used"], 120)
        self.assertEqual(window["percent"], 1.28)
        self.assertIn("本周期额度 1560 积分", plan["summary"])
        self.assertIn("第 1/6 周期", plan["summary"])
        self.assertIn("剩 6 周期", plan["summary"])
        req = opener.calls[0]
        headers = {k.lower(): v for k, v in req.headers.items()}
        self.assertEqual(headers["host"], "tokenhub.tencentcloudapi.com")
        self.assertEqual(headers["x-tc-action"],
                         "DescribeTokenPlanPersonalPackage")
        self.assertEqual(headers["x-tc-region"], "ap-guangzhou")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["ProductType"], "personal-hunyuan")

    def test_unpurchased_label_and_no_window(self):
        opener = fake_urlopen({"Response": {
            "PackageStatus": "UNPURCHASED", "PrepayInquiryKey": ""}})
        plan = hunyuan_quota.fetch_token_plan("id", "key", _urlopen=opener)
        self.assertEqual(plan["plan"], "混元 Token Plan")
        self.assertEqual(plan["windows"], [])
        self.assertTrue(plan["summary"].startswith("未购买"))

    def test_api_error_raises(self):
        opener = fake_urlopen({"Response": {"Error": {
            "Code": "AuthFailure.SignatureFailure",
            "Message": "The provided credentials..."}}})
        with self.assertRaises(RuntimeError) as ctx:
            hunyuan_quota.fetch_token_plan("id", "key", _urlopen=opener)
        self.assertIn("AuthFailure.SignatureFailure", str(ctx.exception))


class HunyuanBalanceTests(unittest.TestCase):
    def test_cents_converted_and_billing_headers(self):
        opener = fake_urlopen({"Response": {
            "Balance": 12345, "PresentAccountBalance": 500,
            "OweAmount": 0, "FreezeAmount": 0}})
        plan = hunyuan_quota.fetch_balance("id", "key", _urlopen=opener)
        self.assertEqual(plan["plan"], "腾讯云账户余额")
        self.assertEqual(plan["provider"], "hunyuan")
        self.assertEqual(plan["summary"], "余额 ¥123.45 · 代金券 ¥5.00")
        req = opener.calls[0]
        headers = {k.lower(): v for k, v in req.headers.items()}
        self.assertEqual(headers["host"], "billing.tencentcloudapi.com")
        self.assertEqual(headers["x-tc-action"], "DescribeAccountBalance")
        self.assertEqual(headers["x-tc-version"], "2018-07-09")
        # The billing plane is global: no region header is sent.
        self.assertNotIn("x-tc-region", headers)

    def test_real_balance_fallback(self):
        opener = fake_urlopen({"Response": {"RealBalance": 100.0}})
        plan = hunyuan_quota.fetch_balance("id", "key", _urlopen=opener)
        self.assertEqual(plan["summary"], "余额 ¥1.00")

    def test_missing_balance_raises(self):
        opener = fake_urlopen({"Response": {}})
        with self.assertRaises(RuntimeError) as ctx:
            hunyuan_quota.fetch_balance("id", "key", _urlopen=opener)
        self.assertIn("余额字段", str(ctx.exception))


def quota_config() -> dict:
    """Channels covering every plan/wallet vendor, one row each."""
    def channel(name, provider, mode, key):
        return {"name": name, "protocol": "openai",
                "base_url": "https://example.invalid/v1", "api_key": key,
                "models": {"auto": ""}, "provider": provider, "mode": mode}

    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            channel("百炼Token", "bailian", "agent", "sk-bl"),
            channel("百炼付费", "bailian", "payg", "sk-bl2"),
            channel("混元Token", "hunyuan", "agent", "ak-hy"),
            channel("混元付费", "hunyuan", "payg", "ak-hy2"),
            channel("智谱Coding", "zhipu", "coding", "zp-key"),
            channel("智谱付费", "zhipu", "payg", "zp-key2"),
            channel("DeepSeek", "deepseek", "payg", "ds-key"),
            {"name": "claude-x", "protocol": "anthropic",
             "base_url": "https://example.invalid", "api_key": "sk-ax",
             "models": {"claude-x": "claude-x"}},
        ],
        "routes": [{"name": "default", "protocol": "openai", "channels": [
            "百炼Token", "百炼付费", "混元Token", "混元付费",
            "智谱Coding", "智谱付费", "DeepSeek"]},
            {"name": "default", "protocol": "anthropic",
             "channels": ["claude-x"]}],
    }


class QuotaWiringTests(unittest.TestCase):
    """plan_for / channel_usage dispatch built on the real App fixture."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        json.dump(quota_config(), tmp)
        tmp.close()
        self.path = Path(tmp.name)
        self.app = App(load_config(self.path), config_path=self.path)
        self.app.subscriptions = [
            {"id": "bl", "plan": "token plan", "channels": ["百炼Token"]},
            {"id": "bl-payg", "plan": "payg", "channels": ["百炼付费"]},
            {"id": "hy", "plan": "token plan", "channels": ["混元Token"]},
            {"id": "hy-payg", "plan": "payg", "channels": ["混元付费"]},
            {"id": "zp", "plan": "coding plan", "channels": ["智谱Coding"]},
            {"id": "zp-payg", "plan": "payg", "channels": ["智谱付费"]},
        ]
        cfg = self.app.config
        object.__setattr__(cfg, "credentials", {
            "hunyuan": {"TENCENT_SECRET_ID": "id", "TENCENT_SECRET_KEY": "key"},
            "bailian": {"BAILIAN_ACCESS_KEY_ID": "ak",
                        "BAILIAN_ACCESS_KEY_SECRET": "sk"},
            "zhipu": {"api_key": "zp-key"},
        })

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        for name in ("vendorquota.json", "vendorquota.tmp"):
            (self.path.parent / name).unlink(missing_ok=True)

    def record(self, record_id: str) -> dict:
        return next(r for r in self.app.subscriptions if r["id"] == record_id)

    def test_plan_for_dispatches_each_vendor(self):
        with mock.patch.object(zhipu_quota, "fetch_coding_plan",
                               return_value={"plan": "Coding Plan",
                                             "windows": []}) as zp, \
             mock.patch.object(bailian_quota, "fetch_token_plan",
                               return_value={"plan": "百炼 Token Plan",
                                             "windows": []}) as bl, \
             mock.patch.object(hunyuan_quota, "fetch_token_plan",
                               return_value={"plan": "混元 Token Plan",
                                             "windows": []}) as hy:
            self.assertEqual(self.app.plan_for(self.record("zp"))["plan"],
                             "Coding Plan")
            self.assertEqual(self.app.plan_for(self.record("bl"))["plan"],
                             "百炼 Token Plan")
            self.assertEqual(self.app.plan_for(self.record("hy"))["plan"],
                             "混元 Token Plan")
        zp.assert_called_once_with("zp-key")
        bl.assert_called_once_with()
        hy.assert_called_once_with("id", "key")

    def test_payg_rows_get_no_plan(self):
        with mock.patch.object(zhipu_quota, "fetch_coding_plan") as zp, \
             mock.patch.object(bailian_quota, "fetch_token_plan") as bl, \
             mock.patch.object(hunyuan_quota, "fetch_token_plan") as hy:
            self.assertIsNone(self.app.plan_for(self.record("zp-payg")))
            self.assertIsNone(self.app.plan_for(self.record("bl-payg")))
            self.assertIsNone(self.app.plan_for(self.record("hy-payg")))
        zp.assert_not_called()
        bl.assert_not_called()
        hy.assert_not_called()

    def test_channel_usage_wallet_takes_ak_sk_pair(self):
        snapshot = {"plan": "阿里云百炼", "provider": "bailian",
                    "windows": [], "summary": "可用余额 ¥1.00"}
        fetch = mock.Mock(return_value=snapshot)
        with mock.patch.dict(server.MGMT_WALLET_FETCHERS, {
                "bailian": (fetch, bailian_quota.AK_ENV,
                            bailian_quota.SK_ENV)}):
            out = self.app.channel_usage(self.app.config.channel("百炼付费"))
        fetch.assert_called_once_with("ak", "sk")
        self.assertEqual(out["summary"], "可用余额 ¥1.00")
        # Cached: a second view does not hit the vendor again.
        with mock.patch.dict(server.MGMT_WALLET_FETCHERS, {
                "bailian": (fetch, bailian_quota.AK_ENV,
                            bailian_quota.SK_ENV)}):
            self.app.channel_usage(self.app.config.channel("百炼付费"))
        self.assertEqual(fetch.call_count, 1)

    def test_channel_usage_without_wallet_creds_is_none(self):
        object.__setattr__(self.app.config, "credentials", {})
        self.assertIsNone(
            self.app.channel_usage(self.app.config.channel("混元付费")))

    def test_channel_usage_payg_key_still_uses_data_plane_key(self):
        fetch = mock.Mock(return_value={"plan": "DeepSeek", "windows": []})
        with mock.patch.dict(server.CHANNEL_USAGE_FETCHERS,
                             {"deepseek": fetch}):
            self.app.channel_usage(self.app.config.channel("DeepSeek"))
        fetch.assert_called_once_with("ds-key")


if __name__ == "__main__":
    unittest.main()
