import asyncio
import importlib.util
import json
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


async def test_common_suite_checks_rendered_results_and_real_cache_work(monkeypatch):
    from kwikmail.widgets import MailView

    clear_body = MailView.clear_body

    async def delayed_clear(view, text):
        if text == "No messages match.":
            await asyncio.sleep(0.05)
        await clear_body(view, text)

    monkeypatch.setattr(MailView, "clear_body", delayed_clear)
    results = await benchmark.suite(100, [50])
    assert results
    assert [result for result in results if result["status"] != "ok"] == []
    by_name = {result["name"]: result for result in results}
    assert by_name["headers.first_page"]["counts"]["headers"] == 100
    assert by_name["body.cold_to_frame"]["counts"]["bodies"] == 1
    assert by_name["body.cached_to_frame"]["counts"] == {}
    assert by_name["body.cache_eviction"]["work"]["evicted_refetches"] == 1
    assert all(
        result["work"]["pending_preview_workers"] == 0
        for result in results
        if result["name"] == "body.render"
    )
    navigation = by_name["network.navigation_to_frame"]
    assert navigation["work"]["latest_preview_verified"]
    assert isinstance(navigation["work"]["body_pending_at_dispatch"], bool)
    assert navigation["heartbeat_ticks"] > 0
    account = by_name["account.slow_isolation_to_frame"]
    assert account["work"]["old_body_pending_at_frame"]
    assert account["work"]["late_old_body_ignored"]
    assert account["work"]["new_account_preview_verified"]
    assert by_name["account.switch_to_frame"]["counts"]["headers"] == 100
    assert by_name["conversation.group"]["work"]["headers_grouped"] == 100
    assert by_name["conversation.group_to_frame"]["work"]["rows"] == 10
    assert by_name["conversation.expand_to_frame"]["work"]["expanded_members"] == 10
    assert by_name["conversation.filter_to_frame"]["work"]["matching_uid"] == 99
    assert {
        result["query"] for result in results if result["name"] == "fuzzy.input_to_frame"
    } == set(benchmark.QUERIES)


def test_conversation_fixture_has_real_ancestry_and_separate_search_match():
    from kwikmail.mail import parse_header
    from kwikmail.threads import group_threads

    headers = [
        parse_header(uid, benchmark.thread_header(uid, 100), ()) for uid in range(100, 0, -1)
    ]
    groups = group_threads(headers)
    assert len(groups) == 10 and all(len(group.members) == 10 for group in groups)
    assert [header.uid for header in groups[0].members] == list(range(100, 90, -1))
    assert headers[1].subject == "zqxvplmn"
    assert headers[1].references == ("<fixture-91@fixture.invalid>",)
    assert benchmark.raw_header(99, 100) != benchmark.thread_header(99, 100)


def test_report_describes_round_robin_for_three_targets(tmp_path, monkeypatch):
    checkouts = [tmp_path / name for name in ("initial", "before", "after")]
    for checkout in checkouts:
        checkout.mkdir()
        (checkout / "uv.lock").write_text("fixed dependencies")
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(benchmark, "ignored_output", lambda _: output)
    observed = []

    def worker(command, **kwargs):
        observed.append(command[command.index("--checkout") + 1])
        return subprocess.CompletedProcess(command, 0, stdout='{"results": []}', stderr="")

    monkeypatch.setattr(benchmark.subprocess, "run", worker)
    args = [argument for checkout in checkouts for argument in ("--checkout", str(checkout))]
    assert benchmark.main([*args, "--sizes", "100", "--delay-ms", "0"]) == 0
    report = json.loads((output / "benchmark.json").read_text())
    assert report["sample_order"] == "round-robin checkout order"
    assert observed == [str(checkout) for checkout in checkouts] * 5
    assert [[run["target"] for run in target["runs"]] for target in report["targets"]] == [
        [index] * 5 for index in range(3)
    ]
