import asyncio
import time

from textual.widgets import Input, Static, TextArea

from kwikmeel.app import MailApp
from kwikmeel.composer import Composer
from kwikmeel.demo import DemoBackend
from kwikmeel.session import AccountSession
from kwikmeel.storage import DraftStore
from kwikmeel.widgets import ConfirmSend, MailView, Picker, ReaderScreen


def make_app(tmp_path, backend=None):
    return MailApp(
        [
            AccountSession(
                "demo", "Demo", backend or DemoBackend(), DraftStore(tmp_path / "draft.json")
            )
        ],
        "demo",
    )


async def settled(pilot, seconds=0.25):
    await asyncio.wait_for(pilot.pause(seconds), timeout=5)


async def test_navigation_filter_read_folders_and_paging(tmp_path):
    app = make_app(tmp_path)
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
    app = MailApp([AccountSession("demo", "Demo", backend, store)], "demo")
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
    app = MailApp([AccountSession("demo", "Demo", backend, store)], "demo")
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
        assert "Sent copy could not be saved" in str(
            app.screen.query_one("#compose-status", Static).render()
        )
        assert store.load().empty and app.screen.delivered
        await pilot.press("ctrl+x")
        assert len(backend.sent) == 1


async def test_network_wait_does_not_block_ui_and_latest_preview_wins(tmp_path):
    class SlowBackend(DemoBackend):
        def body(self, folder, validity, uid):
            time.sleep(0.3)
            return super().body(folder, validity, uid)

    app = make_app(tmp_path, SlowBackend())
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
    app = make_app(tmp_path)
    async with app.run_test(size=(80, 30)) as pilot:
        await settled(pilot)
        assert not app.query_one("#preview", MailView).display
        await pilot.press("c")
        app.screen.query_one("#editor", TextArea).load_text("# Test")
        await pilot.press("ctrl+p")
        assert app.screen.query_one("#compose-preview").display
        await pilot.press("tab")
        assert isinstance(app.focused, Input)


async def test_reply_recovery_and_existing_draft_choices(tmp_path):
    from kwikmeel.models import Draft

    app = make_app(tmp_path)
    session = app.session
    original = Draft(to="unfinished@example.com", subject="Keep me", body="Notes")
    session.drafts.save(original)
    async with app.run_test(size=(140, 42)) as pilot:
        await settled(pilot)
        await pilot.press("enter")
        await settled(pilot)
        reader = app.screen
        assert isinstance(reader, ReaderScreen)
        assert reader.session is session
        await pilot.press("r")
        assert isinstance(app.screen, Picker)
        await pilot.press("escape")
        assert session.drafts.load() == original
        await pilot.press("r", "down", "enter")
        assert isinstance(app.screen, Composer)
        assert app.screen.initial.subject.startswith("Re:")
        assert app.screen.initial.in_reply_to == reader.body.message_id
        app.screen.query_one("#subject", Input).value = "Edited reply"
        await pilot.press("ctrl+s")
        saved = session.drafts.load()
        assert saved.subject == "Edited reply"
        assert saved.in_reply_to == reader.body.message_id
        assert saved.references == app.screen.initial.references
        await pilot.press("escape", "r", "enter")
        assert app.screen.initial == saved
        await pilot.press("escape", "w", "down", "enter")
        assert app.screen.initial.subject.startswith("Fwd:")
        assert not app.screen.initial.in_reply_to
        assert "Forwarded message" in app.screen.initial.body


async def test_arrows_navigate_panes_fields_and_keep_editor_cursor(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(150, 45)) as pilot:
        await settled(pilot)
        assert app.focused is app.table
        await pilot.press("left")
        assert app.focused is app.folder_list
        await pilot.press("right")
        assert app.focused is app.table
        await pilot.press("right")
        assert app.focused is app.preview.query_one(".message-scroll")
        await pilot.press("left", "c", *"abc@example.com", "left")
        composer = app.screen
        to = composer.query_one("#to", Input)
        assert app.focused is to and to.cursor_position == len(to.value) - 1
        await pilot.press("down")
        assert app.focused.id == "cc"
        await pilot.press("down")
        assert app.focused.id == "subject"
        await pilot.press("down")
        editor = composer.query_one(TextArea)
        assert app.focused is editor
        editor.load_text("first\nsecond")
        editor.move_cursor((1, 3))
        await pilot.press("up", "left")
        assert app.focused is editor
        assert editor.cursor_location == (0, 2)
        await pilot.press("alt+up")
        assert app.focused.id == "subject"
        await pilot.press("escape", "enter")
        await settled(pilot)
        assert isinstance(app.screen, ReaderScreen)
        await pilot.press("down", "up", "escape")
        assert app.focused is app.table


async def test_narrow_arrows_skip_hidden_panes(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(80, 30)) as pilot:
        await settled(pilot)
        await pilot.press("left", "right")
        assert app.focused is app.table
        assert not app.preview.display


def two_accounts(tmp_path, first=None, second=None):
    from kwikmeel.config import Account

    return [
        AccountSession(
            "personal", "Personal", first or DemoBackend(), DraftStore(tmp_path / "personal.json")
        ),
        AccountSession(
            "work",
            "Work",
            second
            or DemoBackend(
                account=Account("demo.local", "work@example.com", "work@example.com", "Work")
            ),
            DraftStore(tmp_path / "work.json"),
        ),
    ]


async def test_accounts_stats_diagnostics_and_confirmed_test_email(tmp_path):
    from kwikmeel.widgets import AccountPicker

    sessions = two_accounts(tmp_path)
    app = MailApp(sessions, "personal")
    async with app.run_test(size=(150, 45)) as pilot:
        await settled(pilot)
        await pilot.press("a")
        picker = app.screen
        assert isinstance(picker, AccountPicker)
        await settled(pilot)
        assert all("total 240" in value for value in picker.choices.values())
        assert "new unknown" in picker.choices["work"]
        await pilot.press("R")
        await settled(pilot)
        assert "new 0" in picker.choices["work"]
        await pilot.press("down", "ctrl+t")
        await settled(pilot)
        assert isinstance(app.screen, ReaderScreen)
        assert "IMAP authentication passed" in app.screen.body.markdown
        assert "SMTP authentication passed" in app.screen.body.markdown
        assert not sessions[1].backend.sent
        await pilot.press("escape", "ctrl+e")
        assert isinstance(app.screen, Composer)
        assert app.screen.session is sessions[1]
        assert app.screen.draft().to == "work@example.com"
        assert not sessions[1].backend.sent
        await pilot.press("ctrl+x")
        confirmation = app.screen
        assert isinstance(confirmation, ConfirmSend)
        assert "work@example.com" in confirmation.sender
        assert confirmation.recipients == "work@example.com"
        await pilot.press("escape")
        assert not sessions[1].backend.sent
        await pilot.press("ctrl+x", "y")
        await settled(pilot)
        assert len(sessions[1].backend.sent) == 1
        assert not sessions[0].backend.sent
        await pilot.press("escape", "enter")
        await settled(pilot)
        assert app.active_key == "work"
        assert app.server_query == app.filter_query == ""


async def test_slow_account_refresh_does_not_block_switching(tmp_path):
    from kwikmeel.widgets import AccountPicker

    class SlowStats(DemoBackend):
        def stats(self, *args):
            time.sleep(0.35)
            return super().stats(*args)

    sessions = two_accounts(tmp_path, second=SlowStats())
    app = MailApp(sessions, "personal")
    async with app.run_test(size=(130, 40)) as pilot:
        await settled(pilot)
        await pilot.press("a", "R")
        assert isinstance(app.screen, AccountPicker)
        await pilot.press("escape", "j", "j")
        assert app.table.cursor_row == 2
        await settled(pilot, 0.5)
        assert app.active_key == "personal"


async def test_composer_keeps_its_account_after_active_account_switch(tmp_path):
    sessions = two_accounts(tmp_path)
    app = MailApp(sessions, "personal")
    async with app.run_test(size=(130, 40)) as pilot:
        await settled(pilot)
        await pilot.press("c", *"friend@example.com")
        composer = app.screen
        app.switch_account("work")
        await settled(pilot)
        assert composer.session is sessions[0]
        await pilot.press("ctrl+x", "y")
        await settled(pilot)
        assert len(sessions[0].backend.sent) == 1
        assert not sessions[1].backend.sent
        assert sessions[0].drafts.load().empty
        assert sessions[1].drafts.load().empty


async def test_account_switch_discards_late_success_error_and_loading_cleanup(tmp_path):
    from dataclasses import replace

    from kwikmeel.models import MailboxSnapshot

    class DelayedSession(AccountSession):
        async def mailbox(self, *args):
            result = await super().mailbox(*args)
            if self.delayed:
                self.started.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    await self.release.wait()
                if self.fail:
                    raise RuntimeError("old account request failed")
                page = replace(
                    result.page, headers=[replace(result.page.headers[0], subject="STALE")]
                )
                return MailboxSnapshot(page, result.stats)
            return result

    first, other = two_accounts(tmp_path)
    delayed = DelayedSession(first.key, first.label, first.backend, first.drafts)
    delayed.delayed = False
    delayed.fail = False
    delayed.started, delayed.release = asyncio.Event(), asyncio.Event()
    app = MailApp([delayed, other], "personal")
    async with app.run_test(size=(130, 40)) as pilot:
        await settled(pilot)
        delayed.delayed = True
        app.load_folder()
        await asyncio.wait_for(delayed.started.wait(), 2)
        app.switch_account("work")
        await settled(pilot)
        app.switch_account("personal")
        delayed.delayed = False
        await settled(pilot)
        delayed.release.set()
        await settled(pilot)
        assert all(header.subject != "STALE" for header in app.headers)
        assert not app.loading
        assert "old account request failed" not in str(app.status_widget.render())
        delayed.started.clear()
        delayed.release.clear()
        delayed.delayed = delayed.fail = True
        app.load_folder()
        await asyncio.wait_for(delayed.started.wait(), 2)
        app.switch_account("work")
        await settled(pilot)
        delayed.release.set()
        await settled(pilot)
        assert app.active_key == "work" and app.headers and not app.loading
        assert "old account request failed" not in str(app.status_widget.render())


async def test_thread_search_paging_merge_and_selected_uid(tmp_path):
    from dataclasses import replace

    from kwikmeel.models import MailHeader

    backend = DemoBackend(count=12)
    rows = backend.items["INBOX"]
    backend.account = replace(backend.account, page_size=10)
    backend.items["INBOX"] = [
        replace(row, message_id=f"<m{row.uid}@example.com>", references=()) for row in rows
    ]
    backend.items["INBOX"][0] = replace(
        backend.items["INBOX"][0], references=("<missing@example.com>",)
    )
    backend.items["INBOX"][2] = replace(
        backend.items["INBOX"][2],
        subject="Unique matching child",
        references=("<m11@example.com>",),
    )
    backend.items["INBOX"][-1] = replace(
        backend.items["INBOX"][-1], references=("<missing@example.com>", "<m11@example.com>")
    )
    app = make_app(tmp_path, backend)
    async with app.run_test(size=(150, 45)) as pilot:
        await settled(pilot)
        assert len(app.headers) == 10
        assert len(app.filtered_headers) == 9
        app.table.move_cursor(row=1)
        assert app.selected().uid == 11
        await pilot.press("e", "down")
        assert app.selected().uid == 10
        await pilot.press("L")
        await settled(pilot)
        assert len(app.headers) == 12
        assert app.selected().uid == 10
        assert any(row.header.uid == 10 and row.depth == 1 for row in app.thread_rows)
        app.filter_query = "Unique matching child"
        app.render_rows()
        assert app.filtered_headers[0].uid == 10
        assert isinstance(app.selected(), MailHeader)
        assert "matches" in str(app.info_widget.render())
        await pilot.press("t")
        assert not app.grouping and app.selected().uid == 10


async def test_old_preview_and_reader_cannot_mount_after_account_roundtrip(tmp_path):
    from dataclasses import replace

    from kwikmeel.models import MessageRef

    class DelayedBodySession(AccountSession):
        async def body(self, ref):
            body = await super().body(ref)
            if self.delay_uid == ref.uid:
                self.started.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    await self.release.wait()
                if self.fail:
                    raise RuntimeError("stale reader failure")
                return replace(body, subject="STALE BODY")
            return body

    first, second = two_accounts(tmp_path)
    delayed = DelayedBodySession(first.key, first.label, first.backend, first.drafts)
    delayed.delay_uid = None
    delayed.fail = False
    delayed.started, delayed.release = asyncio.Event(), asyncio.Event()
    app = MailApp([delayed, second], "personal")
    async with app.run_test(size=(140, 42)) as pilot:
        await settled(pilot)
        delayed.delay_uid = app.selected().uid
        app.read_message(MessageRef("personal", app.folder, app.validity, delayed.delay_uid))
        await asyncio.wait_for(delayed.started.wait(), 2)
        app.switch_account("work")
        await settled(pilot)
        app.switch_account("personal")
        delayed.delay_uid = None
        await settled(pilot)
        delayed.release.set()
        await settled(pilot)
        assert len(app.screen_stack) == 1
        assert "STALE BODY" not in str(app.preview.query_one(Static).render())
        assert app.selected().subject in str(app.preview.query_one(Static).render())
        delayed.started.clear()
        delayed.release.clear()
        delayed.delay_uid = app.selected().uid
        ticket = app.ticket("preview")
        app.load_preview(
            ticket, MessageRef("personal", app.folder, app.validity, delayed.delay_uid)
        )
        await asyncio.wait_for(delayed.started.wait(), 2)
        app.switch_account("work")
        delayed.fail = True
        await settled(pilot)
        delayed.release.set()
        await settled(pilot)
        assert "stale reader failure" not in str(app.preview.query_one(Static).render())
        assert app.selected().subject in str(app.preview.query_one(Static).render())


async def test_failed_inbox_refresh_preserves_counts_and_marks_picker_stale(tmp_path):
    from kwikmeel.widgets import AccountPicker

    class FailingStats(DemoBackend):
        fail = False

        def stats(self, *args):
            if self.fail:
                raise OSError("Inbox statistics unavailable")
            return super().stats(*args)

    backend = FailingStats()
    app = make_app(tmp_path, backend)
    async with app.run_test(size=(130, 40)) as pilot:
        await settled(pilot)
        last_sync = app.session.status.last_sync
        backend.fail = True
        await pilot.press("a", "R")
        await settled(pilot)
        assert isinstance(app.screen, AccountPicker)
        text = app.screen.choices["demo"]
        assert "total 240" in text and "stale" in text and "error" in text
        assert app.session.status.last_sync == last_sync


async def test_grouped_search_ranks_exact_older_members_and_groups_first(tmp_path):
    from dataclasses import replace

    backend = DemoBackend(count=20)
    rows = backend.items["INBOX"]
    backend.items["INBOX"] = [
        replace(row, message_id=f"<s{row.uid}@example.com>", references=()) for row in rows
    ]
    headers = backend.items["INBOX"]
    headers[0] = replace(headers[0], subject="Budget planning for next year")
    headers[1] = replace(headers[1], subject="Budget", references=(headers[0].message_id,))
    headers[2] = replace(headers[2], subject="Budget report")
    headers[3] = replace(headers[3], subject="Annual budget estimate")
    app = make_app(tmp_path, backend)
    async with app.run_test(size=(150, 45)) as pilot:
        await settled(pilot)
        app.filter_query = "Budget"
        app.render_rows()
        assert app.filtered_headers[0].uid == headers[1].uid
        assert app.filtered_headers[0].subject == "Budget"
        assert app.thread_rows[0].count == 2
        assert app.filtered_headers[1].uid == headers[0].uid
        assert app.selected().uid == headers[0].uid
        app.expanded.clear()
        app.render_rows(headers[1].uid)
        assert app.filtered_headers[0].uid == headers[1].uid
        assert app.filtered_headers[1].uid == headers[2].uid


async def test_account_details_show_protocol_endpoints_and_sender(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(140, 42)) as pilot:
        await settled(pilot)
        await pilot.press("a", "ctrl+d")
        assert isinstance(app.screen, ReaderScreen)
        detail = app.screen.body.markdown
        assert "demo.local:993" in detail and "demo.local:587" in detail
        assert "tls" in detail and "starttls" in detail
        assert app.session.account.address in detail
        assert "Send state" in detail


async def test_reader_date_uses_local_timezone(tmp_path, monkeypatch):
    from kwikmeel.models import MailBody

    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    time.tzset()
    try:
        app = make_app(tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            await settled(pilot)
            app.invalidate_selection()
            await app.preview.show_body(
                MailBody(
                    1,
                    "Timezone check",
                    "sender@example.com",
                    "you@example.com",
                    "Wed, 28 Oct 2026 12:34:00 +0000",
                    "# Local time",
                )
            )
            assert "Wed, 28 Oct 2026 13:34 CET" in str(app.preview.query_one(Static).render())
    finally:
        monkeypatch.undo()
        time.tzset()


async def test_failed_mailbox_shows_unknown_total_and_preserves_error_preview(tmp_path):
    class OfflineBackend(DemoBackend):
        def folders(self):
            raise OSError("Server unavailable")

    app = make_app(tmp_path, OfflineBackend())
    async with app.run_test(size=(100, 35)) as pilot:
        await settled(pilot)
        assert "unknown total" in str(app.info_widget.render())
        assert "unknown unread" in str(app.info_widget.render())
        assert "inbox unknown" in str(app.topbar_widget.render())
        assert "Mailbox unavailable" in str(app.preview.query_one(Static).render())
        app.render_rows()
        await settled(pilot)
        assert "Mailbox unavailable" in str(app.preview.query_one(Static).render())
        assert "Inbox zero" not in str(app.preview.query_one(Static).render())
