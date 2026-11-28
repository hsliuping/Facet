"""Shared pytest fixtures.

App.__init__ points the benchmark loader at a module-level override path
(next to the test's temp config). Without a reset, one test's App leaks
that global into later tests — e.g. BenchmarkDataPipelineTests reading
"model-scores.json" got real scores merged in and the assertion broke.
"""
import pytest

from facet.gateway import benchmarks as bm


@pytest.fixture(autouse=True)
def _reset_benchmark_override():
    bm.set_override_path(None)
    yield
    bm.set_override_path(None)
