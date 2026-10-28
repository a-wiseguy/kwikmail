import asyncio
import time

from textual.widgets import Input, Static, TextArea

from kwikmeel.app import MailApp
from kwikmeel.composer import Composer
from kwikmeel.demo import DemoBackend
from kwikmeel.storage import DraftStore
from kwikmeel.widgets import ConfirmSend, MailView, Picker, ReaderScreen


async def settled(pilot, seconds=0.25):
    await asyncio.wait_for(pilot.pause(seconds), timeout=5)


async def test_navigation_filter_read_folders_and_paging(tmp_path):
    app = MailApp(DemoBackend(), DraftStore(tmp_path / "draft.json"))
    async with app.run_test(size=(150, 45)) as pilot:
        await settled(pilot)
        assert len(app.headers) == 100
        assert not app.headers[0].seen  # Preview must not mark read.
        await pilot.press("j")
        assert app.table.cursor_row == 1
        await pilot.press("g", "enter")
        await settled(pilot)
        assert isinstance(app.screen, ReaderScreen)
        assert app.headers[0].seen
        await pilot.press("escape", "/", "b", "a", "c", "k", "u", "p")
        await settled(pilot)
        assert "Backup" in app.filtered_headers[0].subject
        await pilot.press("enter", "escape", "L")
        await settled(pilot)
        assert len(app.headers) == 200
        assert len({header.uid for header in app.headers}) == 200
        await pilot.press("f", "a", "r", "c", "enter")
        await settled(pilot)
        assert app.folder == "Archive" and len(app.headers) == 38
        await pilot.press("s", "m", "i", "g", "r", "a", "t", "i", "o", "n", "enter")
        await settled(pilot)
        assert app.server_query == "migration"
        assert all("migration" in row.subject for row in app.headers)
        await pilot.press("escape", "ctrl+p")
        assert isinstance(app.screen, Picker)


async def test_compose_autosave_recover_confirm_and_send(tmp_path):
    backend = DemoBackend()
    store = DraftStore(tmp_path / "draft.json")
    app = MailApp(backend, store)
    async with app.run_test(size=(130, 42)) as pilot:
        await settled(pilot)
        await pilot.press("c")
        assert isinstance(app.screen, Composer)
        await pilot.press(*"friend@example.com", "tab", "tab", *"Hello", "tab", *"# Hi")
        await settled(pilot, 0.7)
        assert store.load().to == "friend@example.com"
        assert store.load().subject == "Hello"
        assert store.load().body == "# Hi"
        await pilot.press("escape", "c")
        assert app.screen.query_one(TextArea).text == "# Hi"
        await pilot.press("ctrl+x")
        assert isinstance(app.screen, ConfirmSend)
        await pilot.press("escape")
        assert not backend.sent
        await pilot.press("ctrl+x", "y")
        await settled(pilot)
        assert len(backend.sent) == 1
        assert store.load().empty
        assert "Demo only" in str(app.screen.query_one("#compose-status", Static).render())
        await pilot.press("ctrl+x")
        assert len(backend.sent) == 1
        await pilot.press("escape")
        assert len(app.screen_stack) == 1


async def test_failed_send_keeps_draft_and_sent_copy_failure_cannot_resend(tmp_path):
    class FailingBackend(DemoBackend):
        fail_send = True

        def send(self, draft):
            if self.fail_send:
                raise OSError("Connection lost")
            return super().send(draft)

        def append_sent(self, raw):
            raise OSError("Sent folder missing")

    backend = FailingBackend()
    store = DraftStore(tmp_path / "draft.json")
    app = MailApp(backend, store)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("c", *"friend@example.com", "ctrl+x", "y")
        await settled(pilot)
        assert not store.load().empty
        assert not app.screen.busy and not app.screen.delivered
        assert "verify delivery" in str(app.screen.query_one("#compose-status", Static).render())
        backend.fail_send = False
        await pilot.press("ctrl+x", "y")
        await settled(pilot)
        assert "Sent copy failed" in str(app.screen.query_one("#compose-status", Static).render())
        assert store.load().empty and app.screen.delivered
        await pilot.press("ctrl+x")
        assert len(backend.sent) == 1


async def test_network_wait_does_not_block_ui_and_latest_preview_wins(tmp_path):
    class SlowBackend(DemoBackend):
        def body(self, folder, validity, uid):
            time.sleep(0.3)
            return super().body(folder, validity, uid)

    app = MailApp(SlowBackend(), DraftStore(tmp_path / "draft.json"))
    async with app.run_test(size=(120, 35)) as pilot:
        await settled(pilot, 0.1)
        await pilot.press("j", "j", "j", "f")
        assert isinstance(app.screen, Picker)
        await pilot.press("escape")
        await settled(pilot, 0.8)
        selected = app.selected()
        meta = str(app.preview.query_one(Static).render())
        assert selected.subject in meta
        await pilot.press("f", "d", "r", "a", "f", "t", "enter")
        await settled(pilot)
        assert not app.headers
        assert "Inbox zero" in str(app.preview.query_one(Static).render())


async def test_compact_layout_and_preview_toggle(tmp_path):
    app = MailApp(DemoBackend(), DraftStore(tmp_path / "draft.json"))
    async with app.run_test(size=(80, 30)) as pilot:
        await settled(pilot)
        assert not app.query_one("#preview", MailView).display
        await pilot.press("c")
        app.screen.query_one("#editor", TextArea).load_text("# Test")
        await pilot.press("ctrl+p")
        assert app.screen.query_one("#compose-preview").display
        await pilot.press("tab")
        assert isinstance(app.focused, Input)
