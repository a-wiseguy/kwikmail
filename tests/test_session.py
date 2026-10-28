import asyncio
import smtplib
import threading
from dataclasses import replace

import pytest

from kwikmeel.config import Account
from kwikmeel.demo import DemoBackend
from kwikmeel.models import Draft, MessageRef
from kwikmeel.session import AccountSession
from kwikmeel.storage import DraftStore


def session(tmp_path, key="personal", backend=None):
    return AccountSession(
        key, key.title(), backend or DemoBackend(8), DraftStore(tmp_path / key / "draft.json")
    )


async def started(event):
    assert await asyncio.to_thread(event.wait, 2), "blocking operation did not start"


class PausedBackend(DemoBackend):
    def __init__(self):
        super().__init__(8)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def folders(self):
        self.calls.append("folders")
        self.entered.set()
        assert self.release.wait(3), "test release timed out"
        return super().folders()

    def page(self, *args):
        self.calls.append("page")
        return super().page(*args)

    def close(self):
        self.calls.append("close")


async def test_cancelled_work_keeps_admission_and_other_account_progresses(tmp_path):
    backend = PausedBackend()
    first, second = session(tmp_path, backend=backend), session(tmp_path, "work")
    task = asyncio.create_task(first.folders())
    await started(backend.entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    queued = asyncio.create_task(first.mailbox("INBOX"))
    snapshot = await asyncio.wait_for(second.mailbox("INBOX"), 1)
    assert snapshot.page.headers and backend.calls == ["folders"]
    backend.release.set()
    assert (await queued).page.headers
    assert backend.calls == ["folders", "page"]
    await first.close()
    await second.close()


async def test_cancelled_queued_operation_never_enters_backend(tmp_path):
    backend = PausedBackend()
    account = session(tmp_path, backend=backend)
    active = asyncio.create_task(account.folders())
    await started(backend.entered)
    queued = asyncio.create_task(account.mailbox("INBOX"))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    backend.release.set()
    await active
    await account.close()
    assert backend.calls == ["folders", "close"]


async def test_close_waits_for_cancelled_worker(tmp_path):
    backend = PausedBackend()
    account = session(tmp_path, backend=backend)
    active = asyncio.create_task(account.folders())
    await started(backend.entered)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    closing = asyncio.create_task(account.close())
    await asyncio.sleep(0)
    assert not closing.done() and backend.calls == ["folders"]
    backend.release.set()
    await closing
    assert backend.calls == ["folders", "close"]
    assert account.status.connection == "closed"
    with pytest.raises(RuntimeError, match="closed"):
        await account.folders()


async def test_message_identity_and_drafts_remain_account_bound(tmp_path):
    first, second = (
        session(tmp_path),
        session(
            tmp_path,
            "work",
            DemoBackend(8, Account("work.local", "work@example.com", "work@example.com")),
        ),
    )
    first.drafts.save(Draft(subject="personal"))
    second.drafts.save(Draft(subject="work"))
    ref = MessageRef("personal", "INBOX", 1, 8)
    assert (await first.body(ref)).to == "you@example.com"
    with pytest.raises(ValueError, match="different account"):
        await second.body(ref)
    assert (await second.body(replace(ref, account="work"))).to == "work@example.com"
    assert first.drafts.load().subject == "personal"
    assert second.drafts.load().subject == "work"
    with pytest.raises(AttributeError):
        first.backend = second.backend
    with pytest.raises(AttributeError):
        first.account = second.account
    await first.close()
    await second.close()


async def test_failed_stats_preserve_checkpoint_and_reconnect_transport(tmp_path):
    class Backend(DemoBackend):
        fail = False
        closed = 0

        def stats(self, folder, previous=None):
            if self.fail:
                raise OSError("private-server-response")
            return super().stats(folder, previous)

        def close(self):
            self.closed += 1

    backend = Backend(8)
    account = session(tmp_path, backend=backend)
    original = await account.refresh_inbox()
    assert original.new_count is None
    backend.fail = True
    with pytest.raises(RuntimeError, match="IMAP: connection failed"):
        await account.refresh_inbox()
    assert account.status.inbox == original
    assert account.status.last_sync == original.checked_at
    assert account.status.stats_stale and backend.closed == 1
    assert "private-server-response" not in account.status.last_error
    backend.fail = False
    backend.items["INBOX"].append(replace(backend.items["INBOX"][0], uid=12))
    fresh = await account.refresh_inbox()
    assert fresh.new_count == 1 and not account.status.stats_stale
    await account.reconnect()
    assert account.status.connection == "unknown" and backend.closed == 2
    await account.close()


class PausedSend(DemoBackend):
    def __init__(self):
        super().__init__(8)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.appended = []

    def send(self, draft):
        self.entered.set()
        assert self.release.wait(3), "test send release timed out"
        return super().send(draft)

    def append_sent(self, raw):
        self.appended.append(raw)


async def test_send_snapshot_survives_cancellation_and_preserves_newer_draft(tmp_path):
    backend = PausedSend()
    account = session(tmp_path, backend=backend)
    original = Draft("friend@example.com", subject="confirmed", body="original")
    account.drafts.save(original)
    sending = asyncio.create_task(account.send(original))
    await started(backend.entered)
    original.subject = "changed in memory"
    newer = Draft("another@example.com", subject="newer")
    account.drafts.save(newer)
    with pytest.raises(RuntimeError, match="already being sent"):
        await account.send(newer)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    assert account.status.send_state == "sending"
    assert (await account.mailbox("INBOX")).page.headers
    backend.release.set()
    await account.close()
    assert backend.sent[0].subject == "confirmed"
    assert account.drafts.load() == newer
    assert len(backend.appended) == 1
    assert account.status.send_state == "accepted"


@pytest.mark.parametrize("failure", ["append", "clear", "newer", "none"])
async def test_acceptance_survives_finalization_failure(tmp_path, failure):
    class Backend(DemoBackend):
        def append_sent(self, raw):
            if failure == "append":
                raise OSError("private append error")
            if failure == "newer":
                account.drafts.save(Draft(subject="newer"))

    class Store(DraftStore):
        def clear(self):
            if failure == "clear":
                raise OSError("private cleanup error")
            super().clear()

    account = AccountSession("personal", "Personal", Backend(), Store(tmp_path / "draft.json"))
    draft = Draft("friend@example.com", subject="confirmed")
    account.drafts.save(draft)
    result = await account.send(draft)
    assert result.accepted and not result.error
    assert account.status.send_state == "accepted"
    assert len(result.warnings) == (1 if failure == "none" else 2)
    if failure == "newer":
        assert account.drafts.load().subject == "newer"
    elif failure == "clear":
        assert account.drafts.load() == draft
    else:
        assert account.drafts.load().empty
    await account.close()


@pytest.mark.parametrize(
    "error,uncertain",
    [
        (OSError("secret transport detail"), True),
        (smtplib.SMTPAuthenticationError(535, b"secret auth detail"), False),
        (smtplib.SMTPRecipientsRefused({"friend@example.com": (550, b"no")}), False),
    ],
)
async def test_submission_failure_retains_draft_and_sanitizes_error(tmp_path, error, uncertain):
    class Backend(DemoBackend):
        def send(self, draft):
            raise error

        def append_sent(self, raw):
            pytest.fail("failed SMTP must not append")

    account = session(tmp_path, backend=Backend())
    draft = Draft("friend@example.com", subject="unsent")
    result = await account.send(draft)
    assert not result.accepted and "secret" not in result.error
    assert ("may have reached" in result.error) == uncertain
    assert account.drafts.load() == draft
    assert account.status.send_state == "failed"
    await account.close()


async def test_mailbox_stats_are_server_counts_not_search_count(tmp_path):
    account = session(tmp_path)
    snapshot = await account.mailbox("INBOX", query="migration")
    assert snapshot.page.total == 1
    assert snapshot.stats.total == 8 and snapshot.stats.unread == 2
    inbox = account.status.inbox
    await account.mailbox("Sent")
    assert account.status.inbox == inbox
    report = await account.check_connections()
    assert report.imap.ok and report.smtp.ok
    await account.close()
