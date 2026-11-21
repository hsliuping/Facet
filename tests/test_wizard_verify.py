"""向导端到端验收（/api/setup/verify-call）— 第一阶段项 1。

覆盖四层：
1. audit.RequestLog 的偏移快照/增量读取（attempt 轨迹采集的地基）；
2. ui.py 端点的参数透传与错误信封；
3. App.verify_setup_call 的协议选择、命中解析、轨迹采集与错误翻译；
4. 静态断言防回归。
"""
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from facet.gateway import ui
from facet.gateway.audit import RequestLog
from facet.gateway.core import parse_config
from facet.gateway.server import App

SRC = Path(__file__).resolve().parents[1] / "src" / "facet" / "gateway"


def _raw():
    return {
        "listen": {"host": "127.0.0.1", "port": 8787},
        "channels": [
            {"name": "测试", "protocol": "openai",
             "base_url": "http://127.0.0.1:9", "models": {"auto": "m1"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["测试"]},
            # 校验要求双协议 default 路由都存在；渠道协议不匹配时调度会跳过，
            # 不产生候选，正好覆盖「只有 openai 渠道」的典型向导产物。
            {"name": "default", "protocol": "anthropic", "channels": ["测试"]},
        ],
    }


def _anthropic_raw():
    return {
        "listen": {"host": "127.0.0.1", "port": 8787},
        "channels": [
            {"name": "cla", "protocol": "anthropic",
             "base_url": "http://127.0.0.1:9", "models": {"auto": "m2"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["cla"]},
            {"name": "default", "protocol": "anthropic", "channels": ["cla"]},
        ],
    }


class _FakeHandler:
    headers = {}

    def __init__(self):
        self.status = None
        self.sent_headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass


class _FakeResponse:
    def __init__(self, headers=None, body=b"{}"):
        self.headers = headers or {}
        self._body = body
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ----------------------------------------------------------- 1: audit 偏移增量


class AuditOffsetTests(unittest.TestCase):
    def _log(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return RequestLog(Path(td.name) / "requests.jsonl")

    def test_offset_snapshot_and_entries_since(self):
        log = self._log()
        log.record(status="ok", channel="a")
        log.record(status="error", channel="b")
        base = log.offset_snapshot()
        entries = log.entries_since(0)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["channel"], "a")
        self.assertEqual(entries[1]["channel"], "b")
        self.assertEqual(log.entries_since(base), [])
        log.record(status="ok", channel="c")
        since = log.entries_since(base)
        self.assertEqual(len(since), 1)
        self.assertEqual(since[0]["channel"], "c")

    def test_partial_trailing_line_ignored(self):
        log = self._log()
        log.record(status="ok", channel="a")
        # 模拟并发追加到一半的残缺行
        with log.path.open("ab") as fh:
            fh.write(b'{"stat')
        entries = log.entries_since(0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["channel"], "a")


# ----------------------------------------------------------- 2: ui 端点信封


class _StubApp:
    def __init__(self, result=None, exc=None):
        self._result = result if result is not None else {"ok": True}
        self._exc = exc
        self.calls = []

    def _authorized_admin(self, h):
        return True

    def verify_setup_call(self, model=None, key=None):
        self.calls.append({"model": model, "key": key})
        if self._exc:
            raise self._exc
        return self._result


class VerifyCallEndpointTests(unittest.TestCase):
    def _post(self, app, raw=b'{"model":"auto","key":"k1"}'):
        h = _FakeHandler()
        ui.handle(app, h, "POST", "/api/setup/verify-call", raw)
        return h, json.loads(h.wfile.getvalue())

    def test_endpoint_passes_model_and_key(self):
        app = _StubApp()
        h, body = self._post(app)
        self.assertEqual(h.status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(app.calls, [{"model": "auto", "key": "k1"}])

    def test_endpoint_defaults_when_body_empty(self):
        app = _StubApp()
        h, _ = self._post(app, b"")
        self.assertEqual(h.status, 200)
        self.assertEqual(app.calls, [{"model": None, "key": None}])

    def test_endpoint_value_error_is_400(self):
        h, body = self._post(_StubApp(exc=ValueError("model 不合法")))
        self.assertEqual(h.status, 400)
        self.assertEqual(body, {"error": {"message": "model 不合法"}})

    def test_endpoint_oserror_is_500(self):
        h, body = self._post(_StubApp(exc=OSError("socket gone")))
        self.assertEqual(h.status, 500)
        self.assertIn("error", body)

    def test_endpoint_rejects_get(self):
        h = _FakeHandler()
        ui.handle(_StubApp(), h, "GET", "/api/setup/verify-call", b"")
        self.assertEqual(h.status, 404)


# ----------------------------------------------------------- 3: 核心逻辑


class VerifySetupCallTests(unittest.TestCase):
    def _app(self, raw=None, audit=True):
        kw = {}
        if audit:
            td = tempfile.TemporaryDirectory()
            self.addCleanup(td.cleanup)
            kw["audit_file"] = Path(td.name) / "requests.jsonl"
        return App(parse_config(raw or _raw()), **kw)

    def test_no_candidate_reports_status_and_hints(self):
        raw = _raw()
        raw["channels"][0]["enabled"] = False
        app = self._app(raw)
        result = app.verify_setup_call()
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "no_candidate")
        self.assertIn("openai:测试", result["channel_status"])
        self.assertEqual(result["channel_status"]["openai:测试"], "disabled")
        self.assertTrue(result["hints"])
        self.assertEqual(result["attempts"], [])

    def test_openai_success_hit_and_attempts(self):
        app = self._app()
        seen_urls = []

        def fake_urlopen(req, timeout=None):
            seen_urls.append(req.full_url)
            # 回环调用由网关真实处理时会写审计：这里模拟同一条轨迹。
            app.audit.record(api="chat", protocol="openai", model="auto",
                             route="default", channel="测试", wire_model="m1",
                             attempt=0, status="ok", http_status=200, ms=12.0)
            return _FakeResponse(
                headers={"X-Routed-Via": "测试/m1", "X-Failover-Count": "0"},
                body=b'{"choices":[{"message":{"content":"hi"}}]}')

        with mock.patch("facet.gateway.server.urlopen", side_effect=fake_urlopen):
            result = app.verify_setup_call(key="wizard-key")
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "ok")
        self.assertEqual(result["protocol"], "openai")
        self.assertEqual(result["route"], "openai:default")
        self.assertEqual(result["hit"], {"channel": "测试", "wire_model": "m1"})
        self.assertEqual(result["failover"], 0)
        self.assertTrue(result["planned"])
        self.assertEqual(result["planned"][0]["channel"], "测试")
        self.assertEqual(result["planned"][0]["wire_model"], "m1")
        self.assertTrue(result["attempts"])
        self.assertEqual(result["attempts"][0]["channel"], "测试")
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertIn("/v1/chat/completions", seen_urls[0])

    def test_anthropic_fallback_uses_messages_path(self):
        app = self._app(_anthropic_raw())
        seen_urls = []

        def fake_urlopen(req, timeout=None):
            seen_urls.append(req.full_url)
            app.audit.record(api="chat", protocol="anthropic", model="auto",
                             route="default", channel="cla", wire_model="m2",
                             attempt=0, status="ok", http_status=200, ms=9.0)
            return _FakeResponse(
                headers={"X-Routed-Via": "cla/m2", "X-Failover-Count": "0"},
                body=b'{"content":[{"type":"text","text":"hi"}]}')

        with mock.patch("facet.gateway.server.urlopen", side_effect=fake_urlopen):
            result = app.verify_setup_call()
        self.assertTrue(result["ok"])
        self.assertEqual(result["protocol"], "anthropic")
        self.assertIn("/v1/messages", seen_urls[0])
        self.assertEqual(result["hit"], {"channel": "cla", "wire_model": "m2"})

    def test_http_error_translated_with_hints(self):
        app = self._app()
        err = HTTPError("http://127.0.0.1:8787/v1/chat/completions", 429,
                        "Too Many Requests", Message(),
                        io.BytesIO(b'{"error":{"message":"rate limited"}}'))
        with mock.patch("facet.gateway.server.urlopen", side_effect=err):
            result = app.verify_setup_call()
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "call_failed")
        self.assertEqual(result["error"]["http_status"], 429)
        self.assertTrue(result["hints"])

    def test_url_error_translated_with_hints(self):
        app = self._app()
        with mock.patch("facet.gateway.server.urlopen",
                        side_effect=URLError("connection refused")):
            result = app.verify_setup_call()
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "call_failed")
        self.assertTrue(result["hints"])

    def test_custom_model_forwarded_in_body(self):
        app = self._app()
        bodies = []

        def fake_urlopen(req, timeout=None):
            bodies.append(json.loads(req.data.decode("utf-8")))
            return _FakeResponse(headers={"X-Routed-Via": "测试/m1"})

        with mock.patch("facet.gateway.server.urlopen", side_effect=fake_urlopen):
            result = app.verify_setup_call(model="m1", key="k")
        self.assertTrue(result["ok"])
        self.assertEqual(bodies[0]["model"], "m1")

    def test_request_carries_bearer_key(self):
        app = self._app()
        headers = []

        def fake_urlopen(req, timeout=None):
            headers.append(dict(req.headers.items()))
            return _FakeResponse(headers={"X-Routed-Via": "测试/m1"})

        with mock.patch("facet.gateway.server.urlopen", side_effect=fake_urlopen):
            app.verify_setup_call(key="wizard-key")
        flat = {k.lower(): v for k, v in headers[0].items()}
        self.assertEqual(flat.get("authorization"), "Bearer wizard-key")


# ----------------------------------------------------------- 4: 防回归


class VerifyWiringTests(unittest.TestCase):
    def test_server_defines_verify_setup_call(self):
        src = (SRC / "server.py").read_text(encoding="utf-8")
        self.assertIn("def verify_setup_call", src)

    def test_ui_routes_verify_call(self):
        src = (SRC / "ui.py").read_text(encoding="utf-8")
        self.assertIn("/api/setup/verify-call", src)

    def test_audit_supports_offset_increment(self):
        src = (SRC / "audit.py").read_text(encoding="utf-8")
        self.assertIn("def offset_snapshot", src)
        self.assertIn("def entries_since", src)

    def test_wizard_has_verify_step(self):
        src = (SRC / "static" / "setup.js").read_text(encoding="utf-8")
        self.assertIn("verify-call", src)


if __name__ == "__main__":
    unittest.main()
