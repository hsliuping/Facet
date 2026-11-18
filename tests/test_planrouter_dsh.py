"""DSH integration: profile wiring detection and launch preconditions."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from facet.gateway import dsh
from facet.gateway.core import parse_config
from facet.gateway.server import App


class DshProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"DSH_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        runs = mock.patch.object(dsh, "_runs", {})
        runs.start()
        self.addCleanup(runs.stop)

    def _profile(self, name="facet-web", patch_text=None):
        directory = self.home / "profiles" / name
        directory.mkdir(parents=True)
        (directory / "package.json").write_text("{}", encoding="utf-8")
        if patch_text is not None:
            (directory / "cordis.patch.yml").write_text(patch_text,
                                                        encoding="utf-8")
        return directory

    def test_status_reads_the_effective_gateway_url(self):
        self._profile(patch_text=(
            "baseURL: http://127.0.0.1:8787/v1\n"
            "baseURL: http://127.0.0.1:8010/v1\n"))
        with mock.patch.object(dsh, "find_cli", return_value={
                "path": "C:/npm/dsh.cmd", "bin": "C:/npm/dsh.js",
                "version": "0.2.2-rc.1"}):
            state = dsh.status()
        self.assertTrue(state["installed"])
        self.assertTrue(state["profile_exists"])
        self.assertEqual(state["base_url"], "http://127.0.0.1:8010/v1")
        self.assertFalse(state["running"])
        self.assertEqual(state["version"], "0.2.2-rc.1")
        self.assertEqual(state["note"], "")

    def test_status_explains_a_missing_client(self):
        self._profile()
        with mock.patch.object(dsh, "find_cli", return_value={
                "path": "", "bin": "", "version": ""}):
            state = dsh.status()
        self.assertFalse(state["installed"])
        self.assertIn("同一台电脑", state["note"])

    def test_status_explains_a_missing_profile(self):
        with mock.patch.object(dsh, "find_cli", return_value={
                "path": "C:/npm/dsh.cmd", "bin": "", "version": ""}):
            state = dsh.status()
        self.assertTrue(state["installed"])
        self.assertFalse(state["profile_exists"])
        self.assertIn("facet-web", state["note"])
        self.assertIn("自动创建", state["note"])

    def test_start_web_refuses_without_the_client(self):
        with mock.patch.object(dsh, "find_cli", return_value={
                "path": "", "bin": "", "version": ""}):
            with self.assertRaises(dsh.DshError):
                dsh.start_web()

    def test_start_web_seeds_a_missing_profile(self):
        # A fresh install has no profile: start_web must create it pointing at
        # the gateway instead of refusing with "run dsh --profile facet-web".
        cli = {"path": "C:/npm/dsh.cmd",
               "bin": "C:/npm/lib/node_modules/@deepseek-ai/dsh/lib/bin.js",
               "version": "0.2.2-rc.1"}
        fake_proc = mock.Mock()
        # Exit immediately with no URL: start_web must still have seeded the
        # profile files; the runtime failure is out of scope here.
        fake_proc.poll.return_value = 1
        fake_proc.returncode = 1
        fake_proc.stdout = iter([])
        with mock.patch.object(dsh, "find_cli", return_value=cli), \
                mock.patch.object(dsh, "_node", return_value="C:/node/node.exe"), \
                mock.patch.object(dsh.subprocess, "Popen", return_value=fake_proc):
            dsh.start_web()
        directory = self.home / "profiles" / "facet-web"
        self.assertTrue((directory / "package.json").is_file())
        manifest = json.loads(
            (directory / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["dsh"]["profile"]["bundles"],
                         dsh._PROFILE_BUNDLES)
        patch = (directory / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertIn("baseURL: http://127.0.0.1:8787/v1", patch)
        self.assertIn("apiKeyEnv: ROUTER_API_KEY", patch)

    def test_start_web_clears_a_stale_dsh_and_retries(self):
        # Exiting Facet used to leave the node child holding 127.0.0.1:3080;
        # the next launch must kill that orphan and retry instead of failing.
        cli = {"path": "C:/npm/dsh.cmd",
               "bin": "C:/npm/lib/node_modules/@deepseek-ai/dsh/lib/bin.js",
               "version": "0.2.2-rc.1"}
        dead = mock.Mock()
        dead.poll.return_value = 1
        dead.returncode = 1
        dead.pid = 100
        live = mock.Mock()
        live.poll.return_value = None
        live.pid = 101
        launches = [
            {"proc": dead, "url": "", "error": "", "job": None,
             "log": ["Error: listen EADDRINUSE: address already in use "
                     "127.0.0.1:3080"]},
            {"proc": live, "url": "http://127.0.0.1:3080/?token=t",
             "error": "", "job": None, "log": []},
        ]
        with mock.patch.object(dsh, "find_cli", return_value=cli), \
                mock.patch.object(dsh, "_launch", side_effect=launches) as launch, \
                mock.patch.object(dsh, "_release_port", return_value=True) as release:
            result = dsh.start_web(api_key="k")
        self.assertTrue(result["ok"])
        release.assert_called_once_with(3080)
        self.assertEqual(launch.call_count, 2)

    def test_start_web_keeps_the_conflict_when_the_port_stays_taken(self):
        cli = {"path": "C:/npm/dsh.cmd", "bin": "", "version": ""}
        dead = mock.Mock()
        dead.poll.return_value = 1
        dead.returncode = 1
        launches = [{"proc": dead, "url": "", "error": "", "job": None,
                     "log": ["Error: listen EADDRINUSE: address already in use "
                             "127.0.0.1:3080"]}]
        with mock.patch.object(dsh, "find_cli", return_value=cli), \
                mock.patch.object(dsh, "_launch", side_effect=launches) as launch, \
                mock.patch.object(dsh, "_release_port", return_value=False):
            result = dsh.start_web(api_key="k")
        self.assertFalse(result["ok"])
        self.assertIn("端口 3080", result["error"])
        self.assertEqual(launch.call_count, 1)

    def test_ensure_profile_upgrades_the_seeded_bundle_list(self):
        # Profiles seeded by an earlier release still carry the bare base +
        # web-app tuple: a Facet upgrade must fill in the bundles itself.
        directory = self.home / "profiles" / "facet-web"
        directory.mkdir(parents=True)
        manifest = {"name": "dsh-profile-facet-web", "private": True,
                    "dependencies": {},
                    "dsh": {"profile": {"bundles": dsh._SEEDED_BUNDLES_V1}}}
        (directory / "package.json").write_text(json.dumps(manifest),
                                                encoding="utf-8")
        (directory / "cordis.patch.yml").write_text("keep: true\n",
                                                    encoding="utf-8")
        dsh.ensure_profile()
        data = json.loads(
            (directory / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(data["dsh"]["profile"]["bundles"],
                         dsh._PROFILE_BUNDLES)
        self.assertEqual((directory / "cordis.patch.yml").read_text(
            encoding="utf-8"), "keep: true\n")

    def test_ensure_profile_keeps_a_hand_edited_bundle_list(self):
        directory = self.home / "profiles" / "facet-web"
        directory.mkdir(parents=True)
        manifest = {"name": "dsh-profile-facet-web",
                    "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}}}
        (directory / "package.json").write_text(json.dumps(manifest),
                                                encoding="utf-8")
        (directory / "cordis.patch.yml").write_text("keep: true\n",
                                                    encoding="utf-8")
        dsh.ensure_profile()
        data = json.loads(
            (directory / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(data["dsh"]["profile"]["bundles"],
                         ["@deepseek-ai/dsh-base"])


class DshModelsTest(unittest.TestCase):
    """The picker is fed from the profile's static list, not GET /v1/models."""

    PATCH = (
        "- id: agent-default-model\n"
        "  config:\n"
        "    provider: facet\n"
        "    model: facet-auto\n"
        "- id: llm-pi-ai\n"
        "  config:\n"
        "    providers:\n"
        "      facet:\n"
        "        apiKeyEnv: ROUTER_API_KEY\n"
        "        api: openai-completions\n"
        "        baseURL: http://127.0.0.1:8010/v1\n"
        "        models:\n"
        "          - id: facet-auto\n"
        "          - id: facet-cheap\n"
        "- id: ui-settings-general\n"
        "  config:\n"
        "    welcomeNoticeVersion: 2026-09-28.1\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"DSH_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def _profile(self, patch_text=PATCH):
        directory = self.home / "profiles" / "facet-web"
        directory.mkdir(parents=True)
        (directory / "package.json").write_text("{}", encoding="utf-8")
        (directory / "cordis.patch.yml").write_text(patch_text, encoding="utf-8")
        return directory

    def test_sync_adds_backend_models_beside_auto_modes(self):
        directory = self._profile()
        models = [
            {"id": "facet-auto", "mode": "auto", "owned_by": "facet"},
            {"id": "deepseek-v4-pro", "mode": "exact", "owned_by": "deepseek"},
        ]
        self.assertTrue(dsh.sync_models(models))
        text = (directory / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertIn('"自动 · 默认链"', text)
        self.assertIn('"deepseek-v4-pro · deepseek"', text)
        # The rest of the patch layer survives the rewrite.
        self.assertIn("baseURL: http://127.0.0.1:8010/v1", text)
        self.assertIn("- id: ui-settings-general", text)
        self.assertIn("model: facet-auto", text)

    def test_sync_keeps_ids_dsh_only_accepts_quoted(self):
        directory = self._profile()
        models = [{"id": "cohere/north-mini-code:free", "mode": "exact",
                   "owned_by": "free-tier"}]
        self.assertTrue(dsh.sync_models(models))
        self.assertIn('id: "cohere/north-mini-code:free"',
                      (directory / "cordis.patch.yml").read_text(encoding="utf-8"))

    def test_repeated_sync_is_idempotent(self):
        directory = self._profile()
        models = [{"id": "facet-auto", "mode": "auto", "owned_by": "facet"}]
        dsh.sync_models(models)
        once = (directory / "cordis.patch.yml").read_text(encoding="utf-8")
        dsh.sync_models(models)
        self.assertEqual((directory / "cordis.patch.yml").read_text(encoding="utf-8"),
                         once)

    def test_missing_profile_is_ignored(self):
        self.assertFalse(dsh.sync_models([{"id": "facet-auto", "mode": "auto"}]))


class DshCatalogTest(unittest.TestCase):
    """The picker offers the branded auto ids plus real backends, no aliases."""

    def _app(self):
        config = {
            "listen": {"host": "127.0.0.1", "port": 0},
            "channels": [{"name": "ark-free", "base_url": "https://ark.example/v3",
                          "api_key": "k",
                          "models": {"m": "m-wire", "dead": "dead-wire",
                                     "ok": "ok-wire"}},
                         {"name": "claude", "protocol": "anthropic",
                          "base_url": "https://claude.example/v1", "api_key": "k",
                          "models": {"claude-x": "claude-x"}}],
            "routes": [{"name": "default", "protocol": "openai",
                        "channels": ["ark-free"]},
                       {"name": "cheap", "protocol": "openai",
                        "channels": ["ark-free"]},
                       {"name": "default", "protocol": "anthropic",
                        "channels": ["claude"]}],
        }
        return App(parse_config(config))

    def test_router_internal_aliases_stay_out_of_the_picker(self):
        ids = [item["id"] for item in self._app().dsh_models()]
        self.assertEqual(ids[:2], ["facet-auto", "facet-cheap"])
        self.assertIn("m", ids)
        for alias in ("auto", "auto:cheap", "facet:cheap"):
            self.assertNotIn(alias, ids)

    def test_failed_probe_models_are_hidden_but_passing_ones_stay(self):
        app = self._app()
        # Probe results are keyed by the upstream id, not the exposed key.
        app.maintenance_pool["tests"] = {"dead-wire": {"ok": False, "error": "404"},
                                         "ok-wire": {"ok": True}}
        ids = [item["id"] for item in app.dsh_models()]
        self.assertNotIn("dead", ids)
        self.assertIn("ok", ids)

    def test_free_tier_models_need_a_passing_probe(self):
        config = {
            "listen": {"host": "127.0.0.1", "port": 0},
            "channels": [{"name": "nim", "base_url": "https://nim.example/v1",
                          "api_key": "k", "tier": "free",
                          "models": {"untested": "untested-wire",
                                     "checked": "checked-wire"}},
                         {"name": "claude", "protocol": "anthropic",
                          "base_url": "https://claude.example/v1", "api_key": "k",
                          "models": {"claude-x": "claude-x"}}],
            "routes": [{"name": "default", "protocol": "openai",
                        "channels": ["nim"]},
                       {"name": "cheap", "protocol": "openai",
                        "channels": ["nim"]},
                       {"name": "default", "protocol": "anthropic",
                        "channels": ["claude"]}],
        }
        app = App(parse_config(config))
        app.maintenance_pool["tests"] = {"checked-wire": {"ok": True}}
        ids = [item["id"] for item in app.dsh_models()]
        self.assertNotIn("untested", ids)
        self.assertIn("checked", ids)
        # Paid channels are not probed, so an untested id has to stay usable.
        self.assertIn("claude-x", ids)


class DshCommandTest(unittest.TestCase):
    CLI = {"path": "/usr/local/bin/dsh",
           "bin": "/usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js"}

    def _command(self, env: dict) -> list[str]:
        with mock.patch.object(dsh, "_node", return_value="/usr/local/bin/node"), \
                mock.patch.dict(os.environ, env):
            return dsh._command(self.CLI, "facet-web", None)

    def test_container_launch_binds_the_interface_and_advertises_the_host_url(self):
        command = self._command({"DSH_WEB_HOST": "172.17.0.2",
                                 "DSH_WEB_PUBLIC_URL": "http://127.0.0.1:3081/"})
        self.assertEqual(command[:2], ["/usr/local/bin/node", self.CLI["bin"]])
        self.assertEqual(command[command.index("--host") + 1], "172.17.0.2")
        self.assertEqual(command[command.index("--public-url") + 1],
                         "http://127.0.0.1:3081/")
        # The fence has to trust the advertised authority, not the bind one.
        self.assertEqual(command[command.index("--trusted-host") + 1],
                         "127.0.0.1:3081")

    def test_host_launch_adds_no_bind_flags(self):
        command = self._command({"DSH_WEB_HOST": "", "DSH_WEB_PUBLIC_URL": ""})
        self.assertNotIn("--host", command)
        self.assertNotIn("--public-url", command)
        self.assertNotIn("--trusted-host", command)


class DshUrlTest(unittest.TestCase):
    def test_startup_line_yields_the_tokenised_url(self):
        line = "dsh web: http://127.0.0.1:3080/?token=abc123"
        self.assertEqual(dsh.URL_LINE.search(line).group(1),
                         "http://127.0.0.1:3080/?token=abc123")

    def test_unrelated_lines_are_ignored(self):
        self.assertIsNone(dsh.URL_LINE.search("dsh: loaded 12 plugins"))

    def test_port_conflict_is_explained_and_keeps_the_port(self):
        entry = {"log": ["Error: listen EADDRINUSE: address already in use "
                         "127.0.0.1:3080"]}
        reason = dsh._failure_reason(entry, 1)
        self.assertIn("端口 3080", reason)
        self.assertEqual(dsh.PORT_IN_USE.search(reason).group(1), "3080")


if __name__ == "__main__":
    unittest.main()
