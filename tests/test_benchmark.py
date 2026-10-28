"""Benchmark correctness gates and output safety."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "benchmark", Path(__file__).resolve().parents[1] / "scripts/benchmark.py"
)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_fixture_is_deterministic_and_sequence_fetch_is_bounded():
    first = benchmark.FixtureClient(10000)
    second = benchmark.FixtureClient(10000)
    assert first.headers == second.headers and first.payloads == second.payloads
    first.use_uid = False
    rows = first.fetch("9901:10000", ["BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)]"])
    assert list(rows) == list(range(9901, 10001))
    assert first.counts["headers"] == 100 and first.counts["bytes"] > 0
    first.use_uid = True
    with pytest.raises(AssertionError, match="bounded sequence"):
        first.fetch("1:10000", [])


async def test_failed_correctness_is_counted_and_excluded_from_timings():
    recorder = benchmark.Recorder()

    async def incorrect():
        raise AssertionError("incorrect result")

    await recorder.measure("incorrect", incorrect)
    result = benchmark.summarize([{"results": recorder.results}])[0]
    assert result["failures"] == 1 and result["samples"] == 0
    assert result["median_ms"] is None
    assert "incorrect result" in recorder.results[0]["error"]


def test_rejects_too_few_samples_and_tracked_output():
    with pytest.raises(SystemExit):
        benchmark.main(["--samples", "4"])
    with pytest.raises(subprocess.CalledProcessError):
        benchmark.ignored_output(Path(__file__).resolve().parents[1] / "docs/results")


async def test_common_suite_checks_rendered_results_and_real_cache_work():
    results = await benchmark.suite(100, [50])
    assert results and all(result["status"] == "ok" for result in results), results
    by_name = {result["name"]: result for result in results}
    assert by_name["headers.first_page"]["counts"]["headers"] == 100
    assert by_name["body.cold_to_frame"]["counts"]["bodies"] == 1
    assert by_name["body.cached_to_frame"]["counts"] == {}
    assert by_name["body.cache_eviction"]["work"]["evicted_refetches"] == 1
    navigation = by_name["network.navigation_to_frame"]
    assert navigation["work"]["latest_preview_verified"]
    assert isinstance(navigation["work"]["body_pending_at_dispatch"], bool)
    assert navigation["heartbeat_ticks"] > 0
    assert {
        result["query"] for result in results if result["name"] == "fuzzy.input_to_frame"
    } == set(benchmark.QUERIES)
