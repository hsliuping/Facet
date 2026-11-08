"""Zero-key first start: seed config generation and CLI integration."""
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

from planrouter.cli import main
from planrouter.core import load_config, parse_config
from planrouter.seed import build_seed, write_seed


class SeedConfigTests(unittest.TestCase):
    def test_seed_is_a_valid_config(self):
        config = parse_config(build_seed())
        by_name = {c.name: c for c in config.channels}
        self.assertEqual(
            {c.name for c in config.channels},
            {"pollinations", "nvidia-nim", "google-ai-studio", "z-ai-glm",
             "anthropic-template"},
        )
        pollinations = by_name["pollinations"]
        self.assertTrue(pollinations.enabled)
        self.assertEqual(pollinations.tier, "free")
        self.assertEqual(pollinations.rate.rpm, 4)
        self.assertEqual(pollinations.models.get("auto"), "openai")
        for name in ("nvidia-nim", "google-ai-studio", "z-ai-glm",
                     "anthropic-template"):
            self.assertFalse(by_name[name].enabled, name)
        self.assertEqual(config.routes["openai:default"].channels,
                         ("pollinations",))
        self.assertEqual(config.routes["anthropic:default"].channels,
                         ("anthropic-template",))
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
        with mock.patch("planrouter.cli.make_server",
                        return_value=self._fake_server()):
            with contextlib.redirect_stdout(out):
                code = main(argv)
        return code, out.getvalue()

    def test_missing_config_is_seeded_and_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "router.json"
            code, output = self._run_serve(["--config", str(config_path)])
            self.assertEqual(code, 0)
            self.assertIn("零密钥首启", output)
            self.assertIn("pollinations", output)
            config = load_config(config_path)  # seeded file parses
            self.assertEqual(
                {c.name for c in config.channels if c.enabled},
                {"pollinations"})

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
            self.assertNotIn("零密钥首启", output)
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


if __name__ == "__main__":
    unittest.main()
