"""Repeatable UI exercise, screenshot export, and local performance measurements."""

import argparse
import asyncio
import json
import os
import statistics
import tempfile
import time
from pathlib import Path

from rapidfuzz import fuzz, process

from kwikmeel.app import MailApp
from kwikmeel.demo import DemoBackend
from kwikmeel.storage import DraftStore
from kwikmeel.widgets import ReaderScreen

ROOT = Path(__file__).resolve().parents[1]


async def main(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    # Export the intended color theme even when this script runs in a no-color CI shell.
    os.environ.pop("NO_COLOR", None)
    backend = DemoBackend(count=10_000)
    choices = {header.uid: header.searchable for header in backend.items["INBOX"]}
    samples = []
    for _ in range(30):
        started = time.perf_counter()
        result = process.extract("backup", choices, scorer=fuzz.WRatio, limit=500, score_cutoff=40)
        samples.append((time.perf_counter() - started) * 1000)
        assert result and "backup" in result[0][0]
    with tempfile.TemporaryDirectory() as tmp:
        app = MailApp(backend, DraftStore(Path(tmp) / "draft.json"))
        started = time.perf_counter()
        async with app.run_test(size=(150, 45)) as pilot:
            deadline = time.perf_counter() + 5
            while not app.headers:
                assert time.perf_counter() < deadline, "Demo inbox did not load"
                await asyncio.sleep(0.01)
            first_page_ms = (time.perf_counter() - started) * 1000
            await pilot.pause(0.4)
            app.save_screenshot("demo.svg", path=output)
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ReaderScreen)
            await pilot.press("escape", "f")
            await pilot.pause(0.1)
            app.save_screenshot("picker.svg", path=output)
            await pilot.press("escape", "c")
            await pilot.press(*"friend@example.com", "tab", "tab", *"Hello from kwikmeel", "tab")
            from textual.widgets import TextArea

            app.screen.query_one(TextArea).load_text(
                "# Small tools, good mail\n\nHey,\n\nThe new terminal workflow is ready.\n\n"
                "- IMAP inbox with fuzzy search\n- Markdown reader and composer\n"
                "- Your own server, your own mail\n\n**See you in the terminal.**"
            )
            await pilot.press("ctrl+p")
            await pilot.pause(0.2)
            app.save_screenshot("compose.svg", path=output)
            await pilot.press("escape")
    report = {
        "fuzzy_headers": len(choices),
        "fuzzy_samples": len(samples),
        "fuzzy_median_ms": round(statistics.median(samples), 2),
        "fuzzy_p95_ms": round(sorted(samples)[int(len(samples) * 0.95) - 1], 2),
        "demo_first_100_headers_ms": round(first_page_ms, 2),
        "note": "Local demo/headless measurements; excludes real network latency.",
    }
    (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".agent-work/verification")
    asyncio.run(main(parser.parse_args().output))
