"""Exercise demo workflows and export wide, narrow, and error screens."""

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from kwikmeel.app import MailApp
from kwikmeel.config import Account
from kwikmeel.demo import DemoBackend
from kwikmeel.session import AccountSession
from kwikmeel.storage import DraftStore
from kwikmeel.widgets import ReaderScreen

ROOT = Path(__file__).resolve().parents[1]


async def main(output: Path):
    subprocess.run(["git", "check-ignore", "--quiet", str(output.resolve())], cwd=ROOT, check=True)
    output.mkdir(parents=True, exist_ok=True)
    os.environ.pop("NO_COLOR", None)
    backend = DemoBackend(count=10_000)
    with tempfile.TemporaryDirectory() as tmp:
        sessions = [
            AccountSession(
                "personal", "Personal demo", backend, DraftStore(Path(tmp) / "personal.json")
            ),
            AccountSession(
                "work",
                "Work demo",
                DemoBackend(
                    account=Account("demo.local", "work@example.com", "work@example.com", "Work")
                ),
                DraftStore(Path(tmp) / "work.json"),
            ),
        ]
        app = MailApp(sessions, "personal")
        started = time.perf_counter()
        async with app.run_test(size=(150, 45)) as pilot:
            deadline = time.perf_counter() + 5
            while not app.headers:
                assert time.perf_counter() < deadline, "Demo inbox did not load"
                await asyncio.sleep(0.01)
            first_page_ms = (time.perf_counter() - started) * 1000
            await pilot.pause(0.4)
            app.save_screenshot("demo.svg", path=output)
            await pilot.press("down", "e")
            await pilot.pause(0.2)
            app.save_screenshot("conversations.svg", path=output)
            await pilot.press("g")
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ReaderScreen)
            app.save_screenshot("reader.svg", path=output)
            await pilot.press("r")
            await pilot.pause(0.1)
            app.save_screenshot("reply.svg", path=output)
            await pilot.press("escape", "escape", "a")
            await pilot.pause(0.1)
            await pilot.press("R")
            await pilot.pause(0.3)
            app.save_screenshot("accounts.svg", path=output)
            await pilot.press("ctrl+t")
            await pilot.pause(0.3)
            app.save_screenshot("diagnostics.svg", path=output)
            await pilot.press("escape", "escape", "f")
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
            await pilot.press("ctrl+x")
            app.save_screenshot("confirm.svg", path=output)
            await pilot.press("escape", "escape")

        narrow = MailApp(
            [
                AccountSession(
                    "personal",
                    "Personal demo",
                    DemoBackend(),
                    DraftStore(Path(tmp) / "narrow.json"),
                )
            ],
            "personal",
        )
        async with narrow.run_test(size=(80, 30)) as pilot:
            await pilot.pause(0.4)
            narrow.save_screenshot("narrow.svg", path=output)
            await pilot.press("a", "R")
            await pilot.pause(0.2)
            narrow.save_screenshot("narrow-accounts.svg", path=output)
            await pilot.press("ctrl+e")
            await pilot.pause(0.1)
            narrow.save_screenshot("narrow-compose.svg", path=output)
            await pilot.press("ctrl+x")
            narrow.save_screenshot("narrow-confirm.svg", path=output)
            await pilot.press("escape", "escape", "escape")

        class OfflineDemo(DemoBackend):
            def folders(self):
                raise OSError("Demo server unavailable")

        offline = MailApp(
            [
                AccountSession(
                    "offline", "Offline demo", OfflineDemo(), DraftStore(Path(tmp) / "offline.json")
                )
            ],
            "offline",
        )
        async with offline.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.4)
            offline.save_screenshot("connection-error.svg", path=output)
            await pilot.press("a")
            offline.save_screenshot("account-error.svg", path=output)
    report = {
        "demo_first_100_headers_ms": round(first_page_ms, 2),
        "note": "Local demo/headless run. Use scripts/benchmark.py for repeated timings.",
    }
    (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".agent-work/verification")
    asyncio.run(main(parser.parse_args().output))
