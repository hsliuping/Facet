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
from facet.gateway.seed import (build_seed, print_seed_banner, write_seed)


class SeedConfigTests(unittest.TestCase):
    def test_seed_is_a_valid_config(self):
        config = parse_config(build_seed())
        by_name = {c.name: c for c in config.channels}
        self.assertEqual(
            {c.name for c in config.channels},
            {"nvidia-nim", "google-ai-studio", "z-ai-glm", "siliconflow",
             "teleai-xing", "scnet", "anthropic-template", "openai-embeddings"},
        )
        for name in ("nvidia-nim", "google-ai-studio",
                     "z-ai-glm", "siliconflow", "teleai-xing", "scnet",
                     "anthropic-template", "openai-embeddings"):
            self.assertFalse(by_name[name].enabled, name)
        # one-time allowance packs survive the config round-trip
        xing = by_name["teleai-xing"].model_quota["Xing4.0-29B"]
        self.assertEqual(xing.tokens, 100000000)
        self.assertEqual(xing.period, "total")
        scnet = by_name["scnet"].model_quota["DeepSeek-R1-Distill-Qwen-7B"]
        self.assertEqual(scnet.tokens, 10000000)
        self.assertIsNone(scnet.expires)
        self.assertEqual(config.routes["openai:default"].channels,
                         ("nvidia-nim", "siliconflow"))
        self.assertEqual(config.routes["anthropic:default"].channels,
                         ("anthropic-template",))
        # 与测试环境一致的 5 条默认路由链：facet-cheap / facet-free /
        # facet-embedding 这些品牌模型名首启就该有真实落点，而不是回落到 default。
        self.assertEqual(
            sorted(config.routes),
            ["anthropic:default", "openai:cheap", "openai:default",
             "openai:embedding", "openai:free"])
        self.assertEqual(
            config.routes["openai:cheap"].channels,
            ("nvidia-nim", "google-ai-studio", "z-ai-glm", "siliconflow",
             "teleai-xing", "scnet"))
        self.assertEqual(
            config.routes["openai:free"].channels,
            ("nvidia-nim", "google-ai-studio", "z-ai-glm", "siliconflow"))
        self.assertEqual(config.routes["openai:embedding"].channels,
                         ("openai-embeddings",))
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
