"""Vendor management credentials: parse, mask, merge and persist.

The console editor stores IAM-style credentials (e.g. Volcengine AK/SK for
the free-quota CLI) in the top-level `credentials` section of router.json:
{provider: {ENV_NAME: value}} where value is plaintext or an `env:VAR`
reference. The editor round-trips masked values as "***", so saving must
inherit stored values instead of erasing them — the same rules as the
per-channel api_key.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from facet.gateway.core import parse_config
from facet.gateway.server import App, merge_secrets


def base_config() -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "channels": [
            {"name": "ark-free", "provider": "volcengine", "mode": "payg",
             "tier": "free",
             "base_url": "https://ark.example/v3", "api_key": "k",
             "models": {"auto": "m"}},
            {"name": "claude", "protocol": "anthropic",
             "base_url": "https://claude.example/v1", "api_key": "k",
             "models": {"claude-x": "claude-x"}},
        ],
        "routes": [{"name": "default", "protocol": "openai",
                    "channels": ["ark-free"]},
                   {"name": "default", "protocol": "anthropic",
                    "channels": ["claude"]}],
        "credentials": {"volcengine": {"VOLC_ACCESSKEY": "ak-plain",
                                       "VOLC_SECRETKEY": "sk-plain"}},
    }


class ParseCredentialsTests(unittest.TestCase):
    def test_plaintext_and_env_resolution(self):
        raw = base_config()
        raw["credentials"]["volcengine"]["VOLC_ACCESSKEY"] = "env:VOLC_AK"
        with mock.patch.dict(os.environ, {"VOLC_AK": "ak-from-env"}):
            config = parse_config(raw)
        self.assertEqual(config.credentials["volcengine"], {
            "VOLC_ACCESSKEY": "ak-from-env",
            "VOLC_SECRETKEY": "sk-plain",
        })

    def test_missing_env_var_dropped(self):
        raw = base_config()
        raw["credentials"]["volcengine"]["VOLC_ACCESSKEY"] = "env:NO_SUCH_VAR_XYZ"
        with mock.patch.dict(os.environ, {}, clear=True):
            config = parse_config(raw)
        self.assertEqual(config.credentials["volcengine"],
                         {"VOLC_SECRETKEY": "sk-plain"})

    def test_malformed_entries_tolerated(self):
        raw = base_config()
        raw["credentials"] = {
            "volcengine": "not-a-dict",           # vendor entry dropped
            "bailian": {"BL_KEY": 123,            # non-str dropped
                        "OK_KEY": "  spaced  "},
        }
        config = parse_config(raw)
        self.assertEqual(config.credentials, {"bailian": {"OK_KEY": "spaced"}})

    def test_section_not_a_dict_ignored(self):
        raw = base_config()
        raw["credentials"] = ["nope"]
        self.assertEqual(parse_config(raw).credentials, {})

    def test_absent_section_empty(self):
        raw = base_config()
        raw.pop("credentials")
        self.assertEqual(parse_config(raw).credentials, {})


class ChannelCredsParseTests(unittest.TestCase):
    """Channel-bound creds (channel = account) parse onto Channel.creds."""

    def test_parsed_stripped_and_stringified(self):
        raw = base_config()
        raw["channels"][0]["creds"] = {"VOLC_ACCESSKEY": "  ch-ak  ",
                                       "VOLC_SECRETKEY": 12345}
        config = parse_config(raw)
        self.assertEqual(config.channel("ark-free").creds,
                         {"VOLC_ACCESSKEY": "ch-ak", "VOLC_SECRETKEY": "12345"})

    def test_absent_or_empty(self):
        raw = base_config()
        self.assertEqual(parse_config(raw).channel("ark-free").creds, {})
        raw["channels"][0]["creds"] = {}
        self.assertEqual(parse_config(raw).channel("ark-free").creds, {})

    def test_non_dict_rejected(self):
        raw = base_config()
        raw["channels"][0]["creds"] = "ak"
        with self.assertRaisesRegex(ValueError, "creds 必须是对象"):
            parse_config(raw)


class MergeCredentialsTests(unittest.TestCase):
    def test_masked_and_empty_inherit_stored(self):
        old = base_config()
        new = base_config()
        new["credentials"]["volcengine"] = {
            "VOLC_ACCESSKEY": "***", "VOLC_SECRETKEY": ""}
        merged = merge_secrets(new, old)
        self.assertEqual(merged["credentials"]["volcengine"], {
            "VOLC_ACCESSKEY": "ak-plain", "VOLC_SECRETKEY": "sk-plain"})

    def test_explicit_value_wins(self):
        old = base_config()
        new = base_config()
        new["credentials"]["volcengine"]["VOLC_ACCESSKEY"] = "ak-new"
        merged = merge_secrets(new, old)
        self.assertEqual(merged["credentials"]["volcengine"]["VOLC_ACCESSKEY"],
                         "ak-new")

    def test_env_reference_passes_through(self):
        old = base_config()
        new = base_config()
        new["credentials"]["volcengine"]["VOLC_ACCESSKEY"] = "env:VOLC_ACCESSKEY"
        merged = merge_secrets(new, old)
        self.assertEqual(merged["credentials"]["volcengine"]["VOLC_ACCESSKEY"],
                         "env:VOLC_ACCESSKEY")

    def test_stale_tab_inherits_whole_section(self):
        old = base_config()
        new = base_config()
        del new["credentials"]
        merged = merge_secrets(new, old)
        self.assertEqual(merged["credentials"], old["credentials"])

    def test_unconfigured_value_removed_and_empty_vendor_dropped(self):
        old = base_config()
        new = base_config()
        new["credentials"] = {"volcengine": {"VOLC_ACCESSKEY": "***",
                                             "VOLC_SECRETKEY": "sk-new"},
                              "newvendor": {"X": "***"}}  # nothing stored
        merged = merge_secrets(new, old)
        self.assertEqual(merged["credentials"], {
            "volcengine": {"VOLC_ACCESSKEY": "ak-plain",
                           "VOLC_SECRETKEY": "sk-new"},
        })


class MergeChannelCredsTests(unittest.TestCase):
    """Channel creds follow the same masked-inheritance rules as api_key."""

    @staticmethod
    def _with_creds(config: dict, creds) -> dict:
        if creds is None:
            config["channels"][0].pop("creds", None)
        else:
            config["channels"][0]["creds"] = creds
        return config

    def test_masked_and_empty_inherit_stored(self):
        old = self._with_creds(base_config(), {"VOLC_ACCESSKEY": "ch-ak",
                                               "VOLC_SECRETKEY": "ch-sk"})
        new = self._with_creds(base_config(), {"VOLC_ACCESSKEY": "***",
                                               "VOLC_SECRETKEY": ""})
        merged = merge_secrets(new, old)
        self.assertEqual(merged["channels"][0]["creds"],
                         {"VOLC_ACCESSKEY": "ch-ak", "VOLC_SECRETKEY": "ch-sk"})

    def test_explicit_value_wins(self):
        old = self._with_creds(base_config(), {"VOLC_ACCESSKEY": "ch-ak",
                                               "VOLC_SECRETKEY": "ch-sk"})
        new = self._with_creds(base_config(), {"VOLC_ACCESSKEY": "***",
                                               "VOLC_SECRETKEY": "ch-sk-new"})
        merged = merge_secrets(new, old)
        self.assertEqual(merged["channels"][0]["creds"],
                         {"VOLC_ACCESSKEY": "ch-ak",
                          "VOLC_SECRETKEY": "ch-sk-new"})

    def test_absent_section_inherits_whole(self):
        """A stale console tab omitting creds must not erase the stored pair."""
        old = self._with_creds(base_config(), {"VOLC_ACCESSKEY": "ch-ak"})
        new = self._with_creds(base_config(), None)
        merged = merge_secrets(new, old)
        self.assertEqual(merged["channels"][0]["creds"],
                         {"VOLC_ACCESSKEY": "ch-ak"})

    def test_unconfigured_value_removed(self):
        old = self._with_creds(base_config(), None)
        new = self._with_creds(base_config(), {"VOLC_ACCESSKEY": "***"})
        merged = merge_secrets(new, old)
        self.assertNotIn("creds", merged["channels"][0])

    def test_other_channels_untouched(self):
        old = self._with_creds(base_config(), {"VOLC_ACCESSKEY": "ch-ak"})
        new = self._with_creds(base_config(), {"VOLC_ACCESSKEY": "ch-ak"})
        merged = merge_secrets(new, old)
        self.assertNotIn("creds", merged["channels"][1])


class AppCredentialsTests(unittest.TestCase):
    """public_config masking + apply_config persistence over a real file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "router.json"
        self.path.write_text(json.dumps(base_config()), encoding="utf-8")
        self.app = App(parse_config(base_config()), config_path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_public_config_masks_plaintext_keeps_env_refs(self):
        visible = self.app.public_config()
        self.assertEqual(visible["credentials"]["volcengine"],
                         {"VOLC_ACCESSKEY": "***", "VOLC_SECRETKEY": "***"})
        self.assertEqual(visible["channels"][0]["api_key"], "***")

    def test_public_config_masks_channel_creds(self):
        raw = dict(self.app.raw)
        raw["channels"][0]["creds"] = {"VOLC_ACCESSKEY": "ch-ak",
                                       "VOLC_SECRETKEY": "env:VOLC_SK"}
        self.app.apply_config(raw)
        visible = self.app.public_config()
        self.assertEqual(visible["channels"][0]["creds"],
                         {"VOLC_ACCESSKEY": "***",
                          "VOLC_SECRETKEY": "env:VOLC_SK"})

    def test_apply_config_persists_channel_creds(self):
        raw = dict(self.app.raw)
        raw["channels"][0]["creds"] = {"VOLC_ACCESSKEY": "ch-ak",
                                       "VOLC_SECRETKEY": "ch-sk"}
        self.app.apply_config(raw)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["channels"][0]["creds"],
                         {"VOLC_ACCESSKEY": "ch-ak", "VOLC_SECRETKEY": "ch-sk"})
        self.assertEqual(self.app.config.channel("ark-free").creds,
                         {"VOLC_ACCESSKEY": "ch-ak", "VOLC_SECRETKEY": "ch-sk"})

    def test_apply_config_masks_and_inherits_then_persists(self):
        raw = dict(self.app.raw)
        raw["credentials"] = {"volcengine": {"VOLC_ACCESSKEY": "***",
                                             "VOLC_SECRETKEY": "sk-rotated"}}
        self.app.apply_config(raw)
        self.assertEqual(self.app.raw["credentials"]["volcengine"], {
            "VOLC_ACCESSKEY": "ak-plain", "VOLC_SECRETKEY": "sk-rotated"})
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["credentials"]["volcengine"], {
            "VOLC_ACCESSKEY": "ak-plain", "VOLC_SECRETKEY": "sk-rotated"})
        # Resolved values flow into the sweep's subprocess injection.
        self.assertEqual(self.app.config.credentials["volcengine"], {
            "VOLC_ACCESSKEY": "ak-plain", "VOLC_SECRETKEY": "sk-rotated"})


class ConfigVersionTests(unittest.TestCase):
    """乐观锁版本只锁可见配置：密钥变化不得触发版本漂移。

    首装向导的撞版本 bug：控制台 GET 之后设置管理密码，listen.admin_key
    从「无」变「有（掩码 ***）」，掩码式哈希必变，向导保存时拿旧版本直接
    被拒。哈希应剔除密钥本身，只锁可见配置。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "router.json"
        self.path.write_text(json.dumps(base_config()), encoding="utf-8")
        self.app = App(parse_config(base_config()), config_path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_setting_admin_key_keeps_config_version(self):
        before = self.app._compute_config_version(self.app.raw)
        self.app.rotate_admin_key("sk-admin-secret", old_key="")
        self.assertEqual(self.app._compute_config_version(self.app.raw), before)

    def test_version_ignores_secrets_and_tracks_visible_changes(self):
        version = self.app._compute_config_version
        before = version(self.app.raw)
        # 换密钥/新增渠道凭据都不动版本（含「无→有」的键出现场景）。
        raw = dict(self.app.raw)
        raw["channels"][0]["api_key"] = "rotated-plain-key"
        raw["channels"][0]["creds"] = {"VOLC_ACCESSKEY": "ch-ak"}
        self.app.apply_config(raw)
        self.assertEqual(version(self.app.raw), before)
        # 可见配置（档位）变化才推进版本。
        raw = dict(self.app.raw)
        raw["channels"][0]["tier"] = "paid"
        self.app.apply_config(raw)
        self.assertNotEqual(version(self.app.raw), before)

    def test_version_ignores_wizard_flag(self):
        """向导状态是服务端自持状态：完成向导不得使页面持有的版本过期，
        否则向导后的第一次保存必撞 version mismatch。"""
        before = self.app._compute_config_version(self.app.raw)
        self.app.raw.setdefault("setup", {})["wizard_done"] = True
        self.assertEqual(self.app._compute_config_version(self.app.raw), before)

    def test_apply_config_preserves_wizard_flag(self):
        """保存配置不得覆盖向导状态：控制台回放的 setup 段永远是旧值，
        采纳它会 wizard_done 被打回 False，向导反复弹出。"""
        self.app.raw["setup"] = {"wizard_done": True}
        raw = json.loads(json.dumps(base_config()))
        raw["setup"] = {"wizard_done": False}  # 模拟前端回放旧状态
        self.app.apply_config(raw)
        self.assertTrue(self.app.raw["setup"]["wizard_done"])
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(disk["setup"]["wizard_done"])


class AppDuplicateChannelTests(unittest.TestCase):
    """复制渠道：端点/密钥/映射整体克隆，新名去重后紧跟源渠道落盘。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "router.json"
        self.path.write_text(json.dumps(base_config()), encoding="utf-8")
        self.app = App(parse_config(base_config()), config_path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_copies_key_models_and_persists_after_source(self):
        result = self.app.duplicate_channel("ark-free")
        self.assertEqual(result, {"ok": True, "name": "ark-free-copy"})
        names = [c["name"] for c in self.app.raw["channels"]]
        self.assertEqual(names, ["ark-free", "ark-free-copy", "claude"])
        clone = self.app.raw["channels"][1]
        self.assertEqual(clone["api_key"], "k")  # real key copied, not "***"
        self.assertEqual(clone["base_url"], "https://ark.example/v3")
        self.assertEqual(clone["models"], {"auto": "m"})
        # The clone is not silently added to route chains.
        self.assertEqual(self.app.raw["routes"][0]["channels"], ["ark-free"])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([c["name"] for c in saved["channels"]], names)

    def test_duplicate_dedupes_repeated_names(self):
        self.app.duplicate_channel("ark-free")
        result = self.app.duplicate_channel("ark-free")
        self.assertEqual(result["name"], "ark-free-copy-2")

    def test_duplicate_unknown_channel_raises(self):
        with self.assertRaises(ValueError):
            self.app.duplicate_channel("nope")


class RouteChannelDedupeTests(unittest.TestCase):
    """同一条链不允许重复渠道：运行时 parse_config 去重（调度不重复尝试），
    存储层 apply_config 去重（落盘文件不残留脏数据）。重复渠道会让 failover
    反复撞同一个已失败的渠道，必须在两层都消除。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "router.json"
        self.path.write_text(json.dumps(base_config()), encoding="utf-8")
        self.app = App(parse_config(base_config()), config_path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _raw_with_openai_default(self, channels):
        """只改 openai:default 链的 channels，保留 base_config 其余路由
        （anthropic:default 是必需路由，不能丢）。"""
        raw = json.loads(json.dumps(base_config()))
        for r in raw["routes"]:
            if r["protocol"] == "openai" and r["name"] == "default":
                r["channels"] = channels
        return raw

    def test_apply_config_dedupes_route_channels_on_persist(self):
        raw = self._raw_with_openai_default(
            ["ark-free", "ark-free", "claude", "ark-free"])
        self.app.apply_config(raw)
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        openai_default = next(r for r in disk["routes"]
                              if r["protocol"] == "openai" and r["name"] == "default")
        # 保序去重，保留首次出现的位置；自动补的 $free 不落盘。
        self.assertEqual(openai_default["channels"], ["ark-free", "claude"])

    def test_parse_config_runtime_dedupes(self):
        raw = self._raw_with_openai_default(["ark-free", "ark-free"])
        cfg = parse_config(raw)
        route = cfg.routes["openai:default"]
        # 运行时调度列表里 ark-free 只出现一次（default 链会自动补 $free 尾部）。
        self.assertEqual(route.channels.count("ark-free"), 1)

    def test_dedup_keeps_first_occurrence_order(self):
        raw = self._raw_with_openai_default(["claude", "ark-free", "claude"])
        self.app.apply_config(raw)
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        openai_default = next(r for r in disk["routes"]
                              if r["protocol"] == "openai" and r["name"] == "default")
        self.assertEqual(openai_default["channels"], ["claude", "ark-free"])


if __name__ == "__main__":
    unittest.main()
