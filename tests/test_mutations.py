import asyncio
import json
import threading
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import pytest

from kwikmail.backend import ImapBackend, MailboxChanged, MutationError
from kwikmail.config import Account
from kwikmail.demo import DemoBackend
from kwikmail.models import MessageRef
from kwikmail.session import AccountSession
from kwikmail.storage import DraftStore


class MutationClient:
    def __init__(self, capabilities=("UIDPLUS",)):
        self.capabilities = set(capabilities)
        self.rows = {1: set(), 2: {b"\\Deleted"}, 3: {b"\\Seen"}}
        self.listing = [((), "/", "INBOX"), ((b"\\Trash",), "/", "Trash")]
        self.calls = []
        self.validity = 7

    def select_folder(self, folder, readonly):
        self.calls.append(("select", folder, readonly))
        return {b"UIDVALIDITY": self.validity}

    def has_capability(self, name):
        return name in self.capabilities

    def list_folders(self):
        return self.listing

    def fetch(self, uids, fields):
        return {uid: {b"UID": uid, b"X-GM-MSGID": 256 + uid} for uid in uids if uid in self.rows}

    def add_flags(self, uids, flags):
        self.calls.append(("add_flags", uids, flags))
        for uid in uids:
            self.rows[uid].update(flags)

    def remove_flags(self, uids, flags):
        for uid in uids:
            self.rows[uid].difference_update(flags)

    def copy(self, uids, folder):
        self.calls.append(("copy", uids, folder))

    def move(self, uids, folder):
        self.calls.append(("move", uids, folder))
        for uid in uids:
            del self.rows[uid]

    def delete_messages(self, uids):
        self.add_flags(uids, [b"\\Deleted"])

    def uid_expunge(self, uids):
        self.calls.append(("uid_expunge", uids))
        for uid in uids:
            if b"\\Deleted" in self.rows[uid]:
                del self.rows[uid]


def backend(capabilities=("UIDPLUS",), account=None):
    instance = ImapBackend(account or Account("mail.local", "user", "user@example.com"))
    instance.client = MutationClient(capabilities)
    return instance


@pytest.mark.parametrize(
    "method,args",
    [("move", ("Trash",)), ("trash", ()), ("delete_permanently", ()), ("set_seen", (True,))],
)
@pytest.mark.parametrize("validity,uids", [(6, (1,)), (7, (1, 99))])
def test_stale_or_missing_selection_never_partially_mutates(method, args, validity, uids):
    instance = backend()
    with pytest.raises(MailboxChanged):
        getattr(instance, method)("INBOX", validity, uids, *args)
    assert instance.client.rows == {1: set(), 2: {b"\\Deleted"}, 3: {b"\\Seen"}}
    assert all(call[0] == "select" for call in instance.client.calls)


@pytest.mark.parametrize("method,args", [("move", ("Trash",)), ("delete_permanently", ())])
def test_targeted_expunge_preserves_unrelated_deleted_messages(method, args):
    instance = backend()
    instance.bodies[("INBOX", 7, 1)] = object()
    getattr(instance, method)("INBOX", 7, (1,), *args)
    assert 1 not in instance.client.rows
    assert instance.client.rows[2] == {b"\\Deleted"}
    assert ("uid_expunge", (1,)) in instance.client.calls
    assert not instance.bodies


def test_move_uses_native_move_without_uidplus():
    instance = backend(("MOVE",))
    instance.trash("INBOX", 7, (1,))
    assert ("move", (1,), "Trash") in instance.client.calls
    assert all(
        call[0] not in ("copy", "add_flags", "uid_expunge") for call in instance.client.calls
    )


@pytest.mark.parametrize("method,args", [("move", ("Trash",)), ("delete_permanently", ())])
def test_unsafe_servers_rejected_before_mutation(method, args):
    instance = backend(())
    with pytest.raises(MutationError, match="support"):
        getattr(instance, method)("INBOX", 7, (1,), *args)
    assert len(instance.client.rows) == 3
    assert all(call[0] == "select" for call in instance.client.calls)


@pytest.mark.parametrize(
    "listing",
    [
        [((), "/", "INBOX")],
        [((b"\\Trash",), "/", "Trash"), ((b"\\Trash",), "/", "Other")],
        [((b"\\Trash", b"\\Noselect"), "/", "Trash")],
    ],
)
def test_trash_discovery_refuses_missing_ambiguous_or_unselectable(listing):
    instance = backend()
    instance.client.listing = listing
    with pytest.raises(MutationError, match="trash_folder"):
        instance.trash("INBOX", 7, (1,))
    assert not instance.client.calls


def test_explicit_trash_folder_and_read_unread():
    instance = backend(account=Account("local", "user", "user@example.com", trash_folder="INBOX"))
    with pytest.raises(MutationError, match="different"):
        instance.trash("INBOX", 7, (1,))
    instance.set_seen("INBOX", 7, (1, 3), False)
    assert instance.client.rows[3] == set()
    instance.set_seen("INBOX", 7, (1, 3), True)
    assert instance.client.rows[1] == instance.client.rows[3] == {b"\\Seen"}


def test_gmail_password_never_falls_back_to_label_deletion():
    instance = backend(("UIDPLUS", "X-GM-EXT-1"))
    with pytest.raises(MutationError, match="OAuth"):
        instance.delete_permanently("INBOX", 7, (1,))
    assert len(instance.client.rows) == 3
    assert all(call[0] == "select" for call in instance.client.calls)


@pytest.mark.parametrize("status", [204, 403])
def test_gmail_permanent_delete_uses_exact_global_ids(monkeypatch, status):
    account = Account("imap.gmail.com", "user", "user@example.com", provider="gmail")
    instance = backend(("X-GM-EXT-1",), replace(account, auth="oauth2"))
    instance.tokens = SimpleNamespace(access_token=lambda: "test-token")
    calls = []

    class Connection:
        def __init__(self, host, timeout, context):
            assert host == "gmail.googleapis.com"

        def request(self, method, path, body, headers):
            calls.append((method, path, json.loads(body)))
            assert headers["Authorization"] == "Bearer test-token"

        def getresponse(self):
            return SimpleNamespace(status=status)

        def close(self):
            calls.append("close")

    monkeypatch.setattr("kwikmail.backend.HTTPSConnection", Connection)
    if status == 204:
        instance.delete_permanently("INBOX", 7, (1, 3))
    else:
        with pytest.raises(MutationError, match="API access"):
            instance.delete_permanently("INBOX", 7, (1, 3))
    assert calls == [
        ("POST", "/gmail/v1/users/me/messages/batchDelete", {"ids": ["101", "103"]}),
        "close",
    ]
    assert all(call[0] == "select" for call in instance.client.calls)


def test_demo_moves_preserve_bodies_and_allocate_new_uids():
    instance = DemoBackend(6)
    original = instance.body("INBOX", 1, 5)
    instance.trash("INBOX", 1, (5, 6))
    assert [row.uid for row in instance.items["Trash"]] == [2, 1]
    moved = next(row for row in instance.items["Trash"] if row.message_id == original.message_id)
    assert replace(instance.body("Trash", 1, moved.uid), uid=5) == original
    raw = BytesParser(policy=policy.default).parsebytes(instance.raw("Trash", 1, moved.uid))
    assert [part.get_filename() for part in raw.iter_attachments()] == ["invoice.txt"]
    instance.delete_permanently("Trash", 1, (moved.uid,))
    instance.move("INBOX", 1, (4,), "Trash")
    assert instance.items["Trash"][0].uid == 3


async def test_session_rejects_cross_account_and_mixed_mailbox_refs(tmp_path):
    instance = AccountSession("one", "one", DemoBackend(3), DraftStore(tmp_path / "draft.json"))
    ref = MessageRef("one", "INBOX", 1, 3)
    try:
        for invalid in [
            replace(ref, account="two"),
            replace(ref, folder="Sent"),
            replace(ref, validity=2),
        ]:
            with pytest.raises(ValueError):
                await instance.trash((ref, invalid))
        assert len(instance.backend.items["INBOX"]) == 3
        await instance.mailbox("INBOX")
        await instance.set_seen((ref,), False)
        assert instance.status.stats_stale and not instance._stats
        assert not instance.backend.items["INBOX"][0].seen
    finally:
        await instance.close()


async def test_mutation_survives_caller_cancellation_and_serializes(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class PausedDemo(DemoBackend):
        def trash(self, *args):
            entered.set()
            assert release.wait(3)
            super().trash(*args)

    instance = AccountSession("one", "one", PausedDemo(3), DraftStore(tmp_path / "draft.json"))
    ref = MessageRef("one", "INBOX", 1, 3)
    task = asyncio.create_task(instance.trash((ref,)))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        queued = asyncio.create_task(instance.mailbox("INBOX"))
        await asyncio.sleep(0)
        assert not queued.done()
        release.set()
        assert (await queued).page.total == 2
        assert instance.backend.items["Trash"][0].message_id.endswith("demo.local>")
    finally:
        release.set()
        await instance.close()
