import asyncio

from kwikmail.app import MailApp
from kwikmail.demo import DemoBackend
from kwikmail.session import AccountSession
from kwikmail.storage import DraftStore
from kwikmail.widgets import ConfirmDelete, Picker, ReaderScreen


def make_app(tmp_path):
    backend = DemoBackend(count=30)
    session = AccountSession("demo", "Demo", backend, DraftStore(tmp_path / "draft.json"))
    return MailApp([session], "demo"), backend


async def settle(pilot):
    await asyncio.wait_for(pilot.pause(0.25), timeout=5)


async def test_visual_range_extends_reverses_pages_and_clears(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await settle(pilot)
        await pilot.press("t", "V", "j", "j")
        await settle(pilot)
        assert app.selected_uids == {h.uid for h in app.filtered_headers[:3]}
        await pilot.press("k")
        assert app.selected_uids == {h.uid for h in app.filtered_headers[:2]}
        await pilot.press("G")
        assert len(app.selected_uids) == len(app.filtered_headers)
        await pilot.press("g")
        assert app.selected_uids == {app.filtered_headers[0].uid}
        await pilot.press("ctrl+d")
        assert len(app.selected_uids) > 1
        await pilot.press("escape")
        assert not app.selected_uids and app.visual_anchor is None
        await pilot.press("g", "space", "j", "space")
        assert len(app.selected_uids) == 2
        await pilot.press("/", *"Coffee")
        await settle(pilot)
        assert not app.selected_uids


async def test_bulk_move_fuzzy_picker_and_trash(tmp_path):
    app, backend = make_app(tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await settle(pilot)
        await pilot.press("t", "V", "j", "j")
        targets = set(app.selected_uids)
        original = len(backend.items["INBOX"])
        archived = len(backend.items["Archive"])
        await pilot.press("m")
        assert isinstance(app.screen, Picker)
        await pilot.press(*"Archive", "enter")
        await settle(pilot)
        assert len(backend.items["INBOX"]) == original - 3
        assert len(backend.items["Archive"]) == archived + 3
        assert not targets.intersection(h.uid for h in backend.items["INBOX"])
        assert not app.selected_uids
        await pilot.press("d")
        await settle(pilot)
        assert len(backend.items["Trash"]) == 1


async def test_delete_confirmation_and_bulk_read_unread(tmp_path):
    app, backend = make_app(tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await settle(pilot)
        await pilot.press("t", "ctrl+a", "u")
        await settle(pilot)
        assert all(not row.seen for row in backend.items["INBOX"])
        await pilot.press("ctrl+a", "i")
        await settle(pilot)
        assert all(row.seen for row in backend.items["INBOX"])
        await pilot.press("V", "j", "D")
        assert isinstance(app.screen, ConfirmDelete)
        await pilot.press("n")
        assert len(backend.items["INBOX"]) == 30
        await pilot.press("D", "y")
        await settle(pilot)
        assert len(backend.items["INBOX"]) == 28


async def test_reader_trash_and_cancelled_stale_move(tmp_path):
    app, backend = make_app(tmp_path)
    async with app.run_test(size=(130, 40)) as pilot:
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, ReaderScreen)
        await pilot.press("d")
        await settle(pilot)
        assert not isinstance(app.screen, ReaderScreen)
        assert len(backend.items["Trash"]) == 1
        await pilot.press("m")
        app.switch_folder("Archive")
        await pilot.press(*"Trash", "enter")
        await settle(pilot)
        assert len(backend.items["Trash"]) == 1
