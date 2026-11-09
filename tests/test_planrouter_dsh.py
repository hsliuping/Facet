"""DSH integration: profile wiring detection and launch preconditions."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from planrouter import dsh


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

    def _profile(self, name="planrouter-web", patch_text=None):
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
        self.assertIn("同一台宿主机", state["note"])

    def test_status_explains_a_missing_profile(self):
        with mock.patch.object(dsh, "find_cli", return_value={
                "path": "C:/npm/dsh.cmd", "bin": "", "version": ""}):
            state = dsh.status()
        self.assertTrue(state["installed"])
        self.assertFalse(state["profile_exists"])
        self.assertIn("dsh --profile planrouter-web", state["note"])

    def test_start_web_refuses_without_the_client(self):
        with mock.patch.object(dsh, "find_cli", return_value={
                "path": "", "bin": "", "version": ""}):
            with self.assertRaises(dsh.DshError):
                dsh.start_web()

    def test_start_web_refuses_without_the_profile(self):
        with mock.patch.object(dsh, "find_cli", return_value={
                "path": "C:/npm/dsh.cmd", "bin": "", "version": ""}):
            with self.assertRaises(dsh.DshError):
                dsh.start_web()


class DshUrlTest(unittest.TestCase):
    def test_startup_line_yields_the_tokenised_url(self):
        line = "dsh web: http://127.0.0.1:3080/?token=abc123"
        self.assertEqual(dsh.URL_LINE.search(line).group(1),
                         "http://127.0.0.1:3080/?token=abc123")

    def test_unrelated_lines_are_ignored(self):
        self.assertIsNone(dsh.URL_LINE.search("dsh: loaded 12 plugins"))


if __name__ == "__main__":
    unittest.main()
