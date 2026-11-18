"""Tests for bundled benchmark data files and the /api/models/compare endpoint."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))

import build_package  # noqa: E402
import validate_benchmarks  # noqa: E402
from facet.gateway import benchmarks as benchmarks_mod  # noqa: E402
from facet.gateway.core import load_config  # noqa: E402
from facet.gateway.server import App, Handler  # noqa: E402

DATA = REPO / "registry"


def load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


class BenchmarkDataTests(unittest.TestCase):
    """Six checks from the design doc (§10), executed on the bundled files."""

    def setUp(self):
        self.benches = load("benchmarks.json")
        self.scores = load("model-scores.json")
        self.coverage = load("model-coverage.json")
        self.registry = load("model-registry.json")

    def test_schemas_are_declared(self):
        self.assertEqual(self.benches.get("schema"), 1)
        self.assertEqual(self.scores.get("schema"), 1)
        self.assertEqual(self.coverage.get("schema"), 1)

    def test_every_score_has_traceable_source_and_setting(self):
        for mid, model in self.scores["models"].items():
            for s in model.get("scores", []):
                with self.subTest(model=mid, bench=s.get("bench")):
                    self.assertTrue(s.get("source_url"), "missing source_url")
                    self.assertTrue(s.get("date"), "missing date")
                    self.assertTrue(s.get("setting"), "missing setting")
                    self.assertIn(s["source_type"], {"vendor", "independent", "aggregator"})

    def test_every_bench_is_in_the_dictionary(self):
        known = set(self.benches["benches"])
        for mid, model in self.scores["models"].items():
            for s in model.get("scores", []):
                self.assertIn(s["bench"], known, f"{mid}: unknown bench {s['bench']}")

    def test_every_setting_is_in_the_whitelist(self):
        for mid, model in self.scores["models"].items():
            for s in model.get("scores", []):
                allowed = self.benches["benches"][s["bench"]]["settings"]
                self.assertIn(s["setting"], allowed,
                              f"{mid}: setting {s['setting']!r} not allowed for {s['bench']}")

    def test_comparable_groups_do_not_mix_units(self):
        groups: dict[tuple, set] = {}
        for model in self.scores["models"].values():
            for s in model.get("scores", []):
                key = (s["bench"], s.get("bench_version", ""), s["setting"], s["harness"])
                groups.setdefault(key, set()).add(s["unit"])
        for key, units in groups.items():
            self.assertEqual(len(units), 1,
                             f"mixed units {units} in comparable group {key}")

    def test_coverage_ids_exist_in_scores_or_registry(self):
        score_keys = set(self.scores["models"])
        registry_keys = set(self.registry["models"])
        for grp in self.coverage["groups"]:
            for item in grp["models"]:
                self.assertTrue(
                    item["id"] in score_keys or item["id"] in registry_keys,
                    f"coverage id {item['id']} not found in scores or registry",
                )
        missing = score_keys - {m["id"] for g in self.coverage["groups"] for m in g["models"]}
        self.assertEqual(missing, set(), f"scores without coverage entry: {sorted(missing)}")


class BenchmarkDataPipelineTests(unittest.TestCase):
    """端到端验证 build_package 与 validate_benchmarks 与 _load 回溯逻辑."""

    def test_build_copies_bench_files_into_data_snapshot(self):
        """--skip-build 后，registry/ 源文件应实体拷贝到 src/facet/data/."""
        rc = build_package.main(["--skip-build"])
        self.assertEqual(rc, 0)
        for fname in ("benchmarks.json", "model-scores.json", "model-coverage.json"):
            src = (REPO / "registry" / fname).read_bytes()
            dst = (REPO / "src" / "facet" / "data" / fname).read_bytes()
            self.assertEqual(src, dst, fname)

    def test_build_fails_when_benchmark_validation_fails(self):
        """当 validate_benchmarks.main() 返回非 0 时，build_package 应返回 1."""
        with mock.patch.object(validate_benchmarks, "main", return_value=1):
            rc = build_package.main(["--skip-build"])
        self.assertEqual(rc, 1)

    def test_build_reports_missing_registry_source(self):
        """registry/ 下缺少 model-registry.json 时应直接返回 1."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(build_package, "REGISTRY", Path(tmp) / "model-registry.json"):
                rc = build_package.main(["--skip-build"])
        self.assertEqual(rc, 1)

    def test_build_reports_missing_bench_sources(self):
        """registry 表存在但三个 benchmark 源缺失时，应返回 1 且不写快照."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            shutil.copyfile(REPO / "registry" / "model-registry.json",
                            tmp_path / "model-registry.json")
            with mock.patch.object(build_package, "REGISTRY", tmp_path / "model-registry.json"):
                rc = build_package.main(["--skip-build"])
        self.assertEqual(rc, 1)

    def test_validator_and_builder_read_registry_sources(self):
        """校验器与构建脚本都以 registry/ 为源头，不得回退到被 gitignore 的 data/。"""
        reg = REPO / "registry"
        self.assertEqual(validate_benchmarks.SCORES, reg / "model-scores.json")
        self.assertEqual(validate_benchmarks.BENCHES, reg / "benchmarks.json")
        self.assertEqual(validate_benchmarks.COVERAGE, reg / "model-coverage.json")
        self.assertEqual(validate_benchmarks.REGISTRY, reg / "model-registry.json")
        self.assertEqual(build_package.REGISTRY, reg / "model-registry.json")
        self.assertEqual(build_package.DATA_DIR, REPO / "src" / "facet" / "data")

    def test_load_prefers_data_then_falls_back_to_registry(self):
        """benchmarks_mod._load 先读 data/，找不到再读 registry/。"""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            reg_dir = Path(tmp) / "registry"
            data_dir.mkdir()
            reg_dir.mkdir()
            (reg_dir / "benchmarks.json").write_text('{"schema":1}', encoding="utf-8")
            (data_dir / "benchmarks.json").write_text('{"schema":2}', encoding="utf-8")
            (reg_dir / "model-scores.json").write_text('{"schema":9}', encoding="utf-8")
            # model-coverage only exists in registry → must fall back
            (reg_dir / "model-coverage.json").write_text('{"schema":7}', encoding="utf-8")
            old_data, old_repo = benchmarks_mod.DATA, benchmarks_mod.REPO_DATA
            benchmarks_mod.DATA, benchmarks_mod.REPO_DATA = data_dir, reg_dir
            try:
                self.assertEqual(benchmarks_mod._load("benchmarks.json", {}), {"schema": 2})
                self.assertEqual(benchmarks_mod._load("model-scores.json", {}), {"schema": 9})
                self.assertEqual(benchmarks_mod._load("model-coverage.json", {}), {"schema": 7})
                self.assertEqual(benchmarks_mod._load("missing.json", {"fallback": True}),
                                 {"fallback": True})
            finally:
                benchmarks_mod.DATA, benchmarks_mod.REPO_DATA = old_data, old_repo


class BenchmarkCompareEndpointTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        config = {
            "_readme": "benchmark endpoint test config",
            "listen": {"host": "127.0.0.1", "port": 0},
            "channels": [
                {
                    "name": "plan-a", "protocol": "openai",
                    "base_url": "http://127.0.0.1:9001",
                    "api_key": "top-secret", "models": {"auto": "model-a"}, "priority": 10,
                },
                {
                    "name": "claude", "protocol": "anthropic",
                    "base_url": "http://127.0.0.1:9003",
                    "api_key": "top-secret", "models": {"auto": "claude-sonnet-4-5"},
                },
            ],
            "routes": [
                {"name": "default", "protocol": "openai", "channels": ["plan-a"]},
                {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
            ],
        }
        self.path.write_text(json.dumps(config), encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(load_config(self.path), config_path=self.path)  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.path.unlink(missing_ok=True)

    def test_compare_endpoint_serves_joined_payload(self):
        req = Request(self.base + "/api/models/compare")
        with urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read())

        self.assertEqual(data["schema"], 1)
        self.assertGreaterEqual(data["totals"]["models"], 20)
        self.assertGreaterEqual(data["totals"]["scores"], 100)
        self.assertGreaterEqual(data["totals"]["benches"], 50)

        opus = next(m for m in data["models"] if m["id"] == "anthropic/claude-opus-5-5")
        self.assertGreater(opus["score_count"], 0)
        for s in opus["scores"]:
            self.assertIn("source_url", s)
            self.assertIn("date", s)
            self.assertIn("setting", s)
        self.assertGreaterEqual(opus["channel_count"], 1)
        self.assertTrue(all("channel" in c and "provider" in c for c in opus["channels"]))
        empty = next(m for m in data["models"] if m["id"] == "xai/grok-4.5")
        self.assertEqual(empty["score_count"], 0)


if __name__ == "__main__":
    unittest.main()
