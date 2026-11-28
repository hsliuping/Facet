"""First start: seed config generation and CLI integration."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from facet.gateway.cli import main
from facet.gateway.core import load_config, parse_config
from facet.gateway.seed import (build_seed, load_seed_channels,
                                print_seed_banner, write_seed)


class SeedConfigTests(unittest.TestCase):
    def test_seed_is_a_valid_config(self):
        config = parse_config(build_seed())
        generated = load_seed_channels()
        generated_names = {c["name"] for c in generated["channels"]}
        by_name = {c.name: c for c in config.channels}
        # 渠道 = 账本生成的渠道 + 两个协议占位渠道，无多余、无缺失
        self.assertEqual(
            {c.name for c in config.channels},
            generated_names | {"anthropic-template", "openai-embeddings"},
        )
        for name in sorted(generated_names | {
                "anthropic-template", "openai-embeddings"}):
            self.assertFalse(by_name[name].enabled, name)
        # 生成渠道的结构不变量：env 形态 key、合法 tier、models.auto 有值
        for doc in generated["channels"]:
            self.assertTrue(doc["api_key"].startswith("env:"), doc["name"])
            channel = by_name[doc["name"]]
            self.assertIn(channel.tier, ("free", "paid"), doc["name"])
            self.assertIsNotNone(channel.models.get("auto"), doc["name"])
        # 真实账本锚点：teleai 开库余额 11 亿（per_model 1 亿 + 活动包 10 亿）
        xing = by_name["teleai"].model_quota["xing4.1-preview"]
        self.assertEqual(xing.tokens, 1100000000)
        self.assertEqual(xing.period, "total")
        # 路由链与生成数据一致；embedding / anthropic 走协议占位渠道。
        # parse_config 已把 $free 宏展开（种子渠道全部未启用时展开为空）。
        self.assertEqual(
            sorted(config.routes),
            ["anthropic:default", "openai:cheap", "openai:default",
             "openai:embedding", "openai:free"])
        for route_name in ("default", "cheap", "free"):
            expected = tuple(n for n in generated["routes"][route_name]
                             if n != "$free")
            self.assertEqual(
                config.routes[f"openai:{route_name}"].channels, expected)
        self.assertEqual(config.routes["anthropic:default"].channels,
                         ("anthropic-template",))
        self.assertEqual(config.routes["openai:embedding"].channels,
                         ("openai-embeddings",))
        # 结构不变量：free 链上除 $free 外全是 tier=free 渠道
        free_names = [c.name for c in config.channels
                      if c.name in config.routes["openai:free"].channels
                      and c.name != "$free"]
        self.assertTrue(free_names)
        for name in free_names:
            self.assertEqual(by_name[name].tier, "free", name)
        # 控制台「模型接入」页从 raw 配置读 models.branded：种子显式写出，
        # 新装的机器上这页才有名字→链映射，而不是只有兜底文案。
        seed = build_seed()
        self.assertEqual(
            seed["models"]["branded"],
            {"facet-auto": "default", "facet-cheap": "cheap",
             "facet-free": "free", "facet-agent": "default",
             "facet-embedding": "embedding"})
        self.assertEqual(config.models.branded["facet-embedding"], "embedding")
        self.assertEqual(config.defaults.maintenance_model, "auto")

    def test_write_seed_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "router.json"
            write_seed(path)
            self.assertTrue(path.is_file())
            load_config(path)  # the written file parses
            path.write_text("{}", encoding="utf-8")
            write_seed(path)  # must not overwrite an existing file
            self.assertEqual(path.read_text(encoding="utf-8"), "{}")


class CliSeedTests(unittest.TestCase):
    def _fake_server(self):
        server = mock.MagicMock()
        server.server_address = ("127.0.0.1", 8787)
        server.app = types.SimpleNamespace(subscriptions=[],
                                           stop_background=lambda: None)
        server.serve_forever.side_effect = KeyboardInterrupt
        return server

    def _run_serve(self, argv):
        out = io.StringIO()
        with mock.patch("facet.gateway.cli.make_server",
                        return_value=self._fake_server()):
            with contextlib.redirect_stdout(out):
                code = main(argv)
        return code, out.getvalue()

    def test_missing_config_is_seeded_and_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "router.json"
            code, output = self._run_serve(["--config", str(config_path)])
            self.assertEqual(code, 0)
            self.assertIn("首启引导", output)
            self.assertNotIn("ovhcloud", output)
            config = load_config(config_path)  # seeded file parses
            self.assertEqual(
                {c.name for c in config.channels if c.enabled},
                set())

    def test_existing_config_is_not_touched(self):
        config = {
            "listen": {"host": "127.0.0.1", "port": 0},
            "channels": [
                {"name": "c1", "protocol": "openai",
                 "base_url": "https://example.invalid/v1",
                 "api_key": "sk-x", "models": {"auto": "wire-a"}},
                {"name": "c2", "protocol": "anthropic",
                 "base_url": "https://example.invalid",
                 "api_key": "sk-y", "models": {"auto": "claude-x"}},
            ],
            "routes": [
                {"name": "default", "protocol": "openai", "channels": ["c1"]},
                {"name": "default", "protocol": "anthropic",
                 "channels": ["c2"]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "router.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            code, output = self._run_serve(["--config", str(config_path)])
            self.assertEqual(code, 0)
            self.assertNotIn("首启引导", output)
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")), config)

    def test_check_does_not_seed(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "router.json"
            with contextlib.redirect_stderr(err):
                code = main(["--config", str(config_path), "--check"])
            self.assertEqual(code, 2)
            self.assertIn("config error", err.getvalue())
            self.assertFalse(config_path.exists())  # no side effect

    def test_seed_has_no_admin_password(self):
        """build_seed() leaves listen.admin_key unset: the console asks for
        one on first open instead of shipping a known default."""
        self.assertFalse(build_seed()["listen"].get("admin_key"))

    def test_written_seed_keeps_no_admin_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            write_seed(path)
            self.assertFalse(load_config(path).listen.admin_key)

    def test_seed_banner_announces_the_password_setup(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            print_seed_banner("router.json")
        text = out.getvalue()
        self.assertIn("管理密码未设置", text)
        self.assertIn("管理控制台", text)


if __name__ == "__main__":
    unittest.main()
