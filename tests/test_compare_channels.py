"""模型对比页渠道列：只统计启用且已配 Key 的真实渠道。

旧行为把「渠道」列匹配到内置注册表条目（厂商变体计数），显示「N 个渠道
可调用」却与用户的渠道无关；现在由 App.compare_channel_rows 提供实时行，
overlay_channels 每次请求叠加进 payload——停用 / 未配 Key / 占位符 ``***``
渠道一律不计，通配符渠道可调任意模型。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from facet.gateway import benchmarks as bm
from facet.gateway.core import load_config, parse_config
from facet.gateway.server import App


def raw_config(url: str) -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            # 显式键带厂商命名空间
            {"name": "exact", "protocol": "openai", "base_url": url,
             "api_key": "sk-1",
             "models": {"anthropic/claude-opus-5-5": "wire-a", "auto": "a"}},
            # 显式键为裸模型名
            {"name": "bare", "protocol": "openai", "base_url": url,
             "api_key": "sk-1", "models": {"claude-opus-5-5": "wire-b"}},
            # 显式键带 :free 后缀（归一化后应与注册表基础名相等）
            {"name": "suffixed", "protocol": "openai", "base_url": url,
             "api_key": "sk-1",
             "models": {"openrouter/anthropic/claude-opus-5-5:free": "wire-f"}},
            # 通配符透传任意模型名
            {"name": "wildcard", "protocol": "openai", "base_url": url,
             "api_key": "sk-1", "models": {"*": "*", "auto": "a"}},
            # 以下三个一律不可调用：停用 / 未配 Key / 掩码导出占位符
            {"name": "disabled", "protocol": "openai", "enabled": False,
             "base_url": url, "api_key": "sk-1",
             "models": {"claude-opus-5-5": "wire-x"}},
            {"name": "nokey", "protocol": "openai", "base_url": url,
             "models": {"claude-opus-5-5": "wire-y"}},
            {"name": "placeholder", "protocol": "openai", "base_url": url,
             "api_key": "***", "models": {"claude-opus-5-5": "wire-z"}},
            {"name": "claude", "protocol": "anthropic", "base_url": url,
             "api_key": "sk-1", "models": {"auto": "claude-auto"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai",
             "channels": ["exact", "bare", "suffixed", "wildcard"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
        ],
    }


class CompareChannelRowsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = parse_config(raw_config("http://127.0.0.1:9000"))
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        json.dump(raw_config("http://127.0.0.1:9000"), tmp)
        tmp.close()
        self.config_path = Path(tmp.name)
        self.app = App(load_config(self.config_path),
                       config_path=self.config_path)

    def tearDown(self):
        self.config_path.unlink(missing_ok=True)
        Path(str(self.config_path) + ".tmp").unlink(missing_ok=True)

    def test_rows_only_enabled_keyed_channels(self):
        rows = self.app.compare_channel_rows()
        self.assertEqual({r[0] for r in rows},
                         {"exact", "bare", "suffixed", "wildcard", "claude"})

    def test_wildcard_row_flagged(self):
        rows = {r[0]: r for r in self.app.compare_channel_rows()}
        self.assertTrue(rows["wildcard"][3])
        self.assertFalse(rows["exact"][3])

    def test_explicit_bases_normalized(self):
        rows = {r[0]: r for r in self.app.compare_channel_rows()}
        norm = bm.norm_base("anthropic/claude-opus-5-5")
        for name in ("exact", "bare", "suffixed"):
            self.assertIn(norm, rows[name][2], name)

    def test_overlay_counts_live_channels(self):
        payload = {"models": [{"id": "anthropic/claude-opus-5-5"}]}
        bm.overlay_channels(payload, self.app.compare_channel_rows())
        entry = payload["models"][0]
        self.assertEqual(entry["channel_count"], 4)
        self.assertEqual({c["channel"] for c in entry["channels"]},
                         {"exact", "bare", "suffixed", "wildcard"})

    def test_unrelated_model_only_wildcard(self):
        payload = {"models": [{"id": "openai/gpt-5.6"}]}
        bm.overlay_channels(payload, self.app.compare_channel_rows())
        entry = payload["models"][0]
        self.assertEqual(entry["channel_count"], 1)
        self.assertEqual(entry["channels"][0]["channel"], "wildcard")


if __name__ == "__main__":
    unittest.main()
