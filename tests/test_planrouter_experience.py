"""体验批（E1/E2/E4/E5/E7/E8/E9）与清理批（C2/C4/C5）：口径统一与死代码清理."""
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from facet.gateway.core import parse_config, parse_free_quota, parse_model_quota
from facet.gateway import dsh as dsh_mod

SRC = Path(__file__).resolve().parents[1] / "src" / "facet" / "gateway"


def _raw():
    return {
        "listen": {"host": "127.0.0.1", "port": 8787},
        "channels": [
            {"name": "测试", "protocol": "openai",
             "base_url": "http://127.0.0.1:9", "models": {"auto": "m"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["测试"]},
            {"name": "default", "protocol": "anthropic", "channels": ["测试"]},
        ],
    }


# --------------------------------------------------------------- E1: DSH 启动失败


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


class _FakeApp:
    def __init__(self, exc):
        self.exc = exc

    def _authorized_admin(self, h):
        return True

    def start_dsh_web(self, port):
        raise self.exc


class DshStartErrorShapeTests(unittest.TestCase):
    def _post(self, app):
        from facet.gateway import ui
        h = _FakeHandler()
        ui.handle(app, h, "POST", "/api/integrations/dsh/web", b"{}")
        return h, json.loads(h.wfile.getvalue())

    def test_dsh_error_uses_error_message_shape(self):
        h, body = self._post(_FakeApp(dsh_mod.DshError("facet-web 端口 8788 已被占用")))
        self.assertEqual(h.status, 503)
        self.assertEqual(body, {"ok": False,
                                "error": {"message": "facet-web 端口 8788 已被占用"}})

    def test_value_error_branch_keeps_error_message_shape(self):
        h, body = self._post(_FakeApp(ValueError("port out of range")))
        self.assertEqual(h.status, 400)
        self.assertEqual(body, {"error": {"message": "port out of range"}})


# --------------------------------------------------------------- E2: repr(exc) 清理


class RelayErrorDetailStyleTests(unittest.TestCase):
    def test_server_no_longer_reprs_exceptions_for_details(self):
        src = (SRC / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("repr(exc)[:300]", src)


# --------------------------------------------------------------- E4: 校验消息中文化


class ValidationMessageTests(unittest.TestCase):
    def test_config_shape_messages_are_chinese(self):
        with self.assertRaisesRegex(ValueError, "config 必须是 JSON 对象"):
            parse_config([])
        with self.assertRaisesRegex(ValueError, "至少需要一个渠道"):
            raw = _raw()
            raw["channels"] = []
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "defaults.track_stream_usage 必须是 true 或 false"):
            raw = _raw()
            raw["defaults"] = {"track_stream_usage": "false"}
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "必须设置 listen.api_key"):
            raw = _raw()
            raw["listen"] = {"host": "0.0.0.0", "port": 8787}
            parse_config(raw)

    def test_channel_messages_are_chinese(self):
        with self.assertRaisesRegex(ValueError, "channel 测试：protocol 必须是"):
            raw = _raw()
            raw["channels"][0]["protocol"] = "grpc"
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "channel 测试：base_url 必须是 http"):
            raw = _raw()
            raw["channels"][0]["base_url"] = "ftp://x"
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "channel 测试：creds 必须是对象"):
            raw = _raw()
            raw["channels"][0]["creds"] = ["x"]  # 非空且不是对象才触发校验
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "channel 测试：tier 必须是"):
            raw = _raw()
            raw["channels"][0]["tier"] = "vip"
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "channel 测试：必须提供 models 映射"):
            raw = _raw()
            raw["channels"][0]["models"] = {}
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "channel 测试：vision 列表不能为空"):
            raw = _raw()
            raw["channels"][0]["vision"] = []
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "channel 测试：vision_model 与 vision: false 冲突"):
            raw = _raw()
            raw["channels"][0]["vision"] = False
            raw["channels"][0]["vision_model"] = "v"
            parse_config(raw)

    def test_route_messages_are_chinese(self):
        with self.assertRaisesRegex(ValueError, "route default：未知渠道"):
            raw = _raw()
            raw["routes"][0]["channels"] = ["不存在"]
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "缺少 openai:default 路由"):
            raw = _raw()
            raw["routes"] = []
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "路由重复"):
            raw = _raw()
            raw["routes"].append(dict(raw["routes"][0]))
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "route default：lb 必须是"):
            raw = _raw()
            raw["routes"][0]["lb"] = "random"
            parse_config(raw)

    def test_quota_messages_are_chinese(self):
        with self.assertRaisesRegex(ValueError, "free_quota 必须是对象"):
            parse_free_quota([1])
        with self.assertRaisesRegex(ValueError, "free_quota.tokens 必须是非负整数"):
            parse_free_quota({"tokens": -1})
        with self.assertRaisesRegex(ValueError, "free_quota.period 必须是"):
            parse_free_quota({"tokens": 1, "period": "year"})
        with self.assertRaisesRegex(ValueError, "free_quota 至少要填 tokens 或 requests 之一"):
            parse_free_quota({"period": "month"})
        with self.assertRaisesRegex(ValueError, "free_quota.expires 必须是日期"):
            parse_free_quota({"tokens": 1, "expires": "not-a-date"})
        with self.assertRaisesRegex(ValueError, "model_quota 必须是对象"):
            parse_model_quota("x", [])
        with self.assertRaisesRegex(ValueError, "model_quota 的键必须是模型名"):
            parse_model_quota("x", {"": {"tokens": 1}})

    def test_models_policy_messages_are_chinese(self):
        with self.assertRaisesRegex(ValueError, "models 策略必须是 JSON 对象"):
            raw = _raw()
            raw["models"] = "nope"
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "models.branded 必须是对象"):
            raw = _raw()
            raw["models"] = {"branded": ["x"]}  # 非空且不是对象才触发校验
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "保留的内部模型名"):
            raw = _raw()
            raw["models"] = {"branded": {"auto:custom": "default"}}
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "models.fallback_unknown 必须是 true 或 false"):
            raw = _raw()
            raw["models"] = {"fallback_unknown": "yes"}
            parse_config(raw)
        with self.assertRaisesRegex(ValueError, "models.fallback_route 必须是路由名"):
            raw = _raw()
            raw["models"] = {"fallback_route": "  "}
            parse_config(raw)


# --------------------------------------------------------------- E7: 探测兜底提示


class ProbeUnexpectedErrorHintTests(unittest.TestCase):
    def test_unexpected_exception_reports_clean_error_and_hint(self):
        from facet.gateway import probe
        raw = {"name": "测试", "protocol": "openai",
               "base_url": "http://127.0.0.1:9", "models": {"auto": "m"}}
        with mock.patch.object(probe, "open_with_proxy", side_effect=RuntimeError("boom")):
            out = probe.probe_channel(raw)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "boom")
        self.assertIn("base_url", out["hint"])
        self.assertIn("代理", out["hint"])
        self.assertIn("超时", out["hint"])


# ------------------------------------------------------- E5/E8/E9: 控制台文案


class ConsoleCopyTests(unittest.TestCase):
    def setUp(self):
        self.js = (SRC / "static" / "app.js").read_text(encoding="utf-8")

    def test_request_labels_cover_cooldown_status(self):
        # E5: cooldown 状态（上游额度预占切换等）此前直出英文键名。
        self.assertIn("cooldown: '额度冷却'", self.js)

    def test_reason_labels_map_machine_reasons(self):
        # E5: bad_request / relay_error / transient_* 等机器原因直出英文。
        self.assertIn("REASON_LABELS", self.js)
        for key in ("bad_request", "relay_error", "transient_internal",
                    "transient_network", "upstream-quota-exhausted"):
            self.assertIn(key, self.js)
        self.assertIn("REASON_LABELS[r] || r", self.js)
        self.assertIn("reasonText(e.reason)", self.js)

    def test_unauth_hints_point_to_left_menu(self):
        # E8: 管理入口在左侧菜单，不在右上角。
        self.assertNotIn("右上角「API Key」", self.js)
        self.assertNotIn("右上角「管理密钥」", self.js)
        self.assertIn("左侧菜单", self.js)

    def test_channel_counters_use_thousand_separators(self):
        # E9: 请求/成功/失败三数与 Tokens 一样使用千分位。
        self.assertIn(
            "${Number(c.total_requests).toLocaleString()} / "
            "${Number(c.served_requests).toLocaleString()} / "
            "${Number(c.failed_requests).toLocaleString()}", self.js)


# --------------------------------------------------------------- 清理批


class CleanupTests(unittest.TestCase):
    def test_no_dead_embedding_import_in_server(self):
        # C2: server.py 不再引用 EMBEDDING_MODEL，死导入应删除。
        src = (SRC / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("EMBEDDING_MODEL", src)

    def test_freealign_output_parser_has_distinct_name(self):
        # C4: 与 core.parse_free_quota 同名易混，freealign 侧改为 *_output_rows。
        from facet.gateway import freealign
        self.assertTrue(hasattr(freealign, "parse_free_quota_output_rows"))
        self.assertFalse(hasattr(freealign, "parse_free_quota_output"))
        self.assertEqual(freealign.parse_free_quota_output_rows('{"a": 1}'), [])
        self.assertEqual(freealign.parse_free_quota_output_rows("not json"), [])

    def test_inbound_shape_log_is_env_gated(self):
        # C5: [inbound] 每请求一行改为 FACET_INBOUND_DEBUG 门控。
        from facet.gateway import server
        body = {"model": "m",
                "messages": [{"role": "user", "content": "hi"}]}

        def capture(env):
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"FACET_INBOUND_DEBUG": env}):
                with mock.patch.object(sys, "stdout", buf):
                    server._log_inbound_shape(body, 100)
            return buf.getvalue()

        self.assertEqual(capture(""), "")
        self.assertIn("[inbound]", capture("1"))


if __name__ == "__main__":
    unittest.main()
