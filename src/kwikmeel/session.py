"""Account-bound async admission for blocking mail and draft finalization."""

import asyncio
import smtplib
import ssl
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from typing import TypeVar

from .backend import MailboxChanged, connection_error
from .config import Account
from .models import (
    ConnectionReport,
    Draft,
    MailBody,
    MailboxSnapshot,
    MailboxStats,
    MessageRef,
    SendOutcome,
    SessionStatus,
)
from .storage import DraftStore

T = TypeVar("T")


class AccountSession:
    def __init__(self, key: str, label: str, backend, drafts: DraftStore):
        self.key = key
        self.label = label
        self._backend = backend
        self._drafts = drafts
        self._account = backend.account
        self._status = SessionStatus()
        self._stats: dict[str, MailboxStats] = {}
        self._imap_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kwikmeel-imap")
        self._smtp_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kwikmeel-smtp")
        self._imap_lock = asyncio.Lock()
        self._smtp_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._send_task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None

    @property
    def backend(self):
        return self._backend

    @property
    def drafts(self) -> DraftStore:
        return self._drafts

    @property
    def account(self) -> Account:
        return self._account

    @property
    def status(self) -> SessionStatus:
        return self._status

    def _open(self) -> None:
        if self._close_task is not None:
            raise RuntimeError("Account session is closed")

    def _track(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)

        def done(task):
            self._tasks.discard(task)
            if not task.cancelled():
                task.exception()

        task.add_done_callback(done)
        return task

    async def _run(
        self,
        lock: asyncio.Lock,
        operation: Callable[[], T],
        *,
        protocol: str,
        remember: Callable[[T], None] | None = None,
        finalizing: bool = False,
    ) -> T:
        if not finalizing:
            self._open()
        await lock.acquire()
        try:
            if not finalizing:
                self._open()
        except BaseException:
            lock.release()
            raise

        backend = self.backend

        def blocking():
            try:
                return operation()
            except Exception:
                if protocol == "IMAP":
                    with suppress(Exception):
                        backend.close()
                raise

        async def admitted():
            try:
                executor = self._imap_executor if protocol == "IMAP" else self._smtp_executor
                result = await asyncio.get_running_loop().run_in_executor(executor, blocking)
                if protocol == "IMAP":
                    self._status = replace(self._status, connection="connected", last_error="")
                if remember:
                    remember(result)
                return result
            except Exception as error:
                message = (
                    str(error)
                    if isinstance(error, MailboxChanged)
                    else connection_error(error, protocol)
                )
                if protocol == "IMAP":
                    self._status = replace(
                        self._status, connection="error", last_error=message, stats_stale=True
                    )
                if isinstance(error, MailboxChanged):
                    raise
                raise RuntimeError(message) from error
            finally:
                lock.release()

        return await asyncio.shield(self._track(admitted()))

    def _remember(self, folder: str, stats: MailboxStats) -> None:
        self._stats[folder] = stats
        if folder == self.account.inbox:
            self._status = replace(
                self._status,
                inbox=stats,
                last_sync=stats.checked_at,
                stats_stale=False,
                last_error="",
            )

    def _check_ref(self, ref: MessageRef) -> None:
        if ref.account != self.key:
            raise ValueError("Message belongs to a different account")

    async def folders(self) -> list[str]:
        return await self._run(self._imap_lock, self.backend.folders, protocol="IMAP")

    async def mailbox(self, folder, end=None, query="", validity=None) -> MailboxSnapshot:
        backend = self.backend

        def fetch():
            page = backend.page(folder, end, query, validity)
            stats = backend.stats(folder, self._stats.get(folder))
            if page.validity != stats.validity:
                raise MailboxChanged("Mailbox changed during loading. Refresh to retry.")
            return MailboxSnapshot(page, stats)

        return await self._run(
            self._imap_lock,
            fetch,
            protocol="IMAP",
            remember=lambda snapshot: self._remember(folder, snapshot.stats),
        )

    async def body(self, ref: MessageRef) -> MailBody:
        self._check_ref(ref)
        backend = self.backend
        return await self._run(
            self._imap_lock,
            lambda: backend.body(ref.folder, ref.validity, ref.uid),
            protocol="IMAP",
        )

    async def mark_seen(self, ref: MessageRef) -> None:
        self._check_ref(ref)
        backend = self.backend

        def marked(_):
            if ref.folder == self.account.inbox:
                self._status = replace(self._status, stats_stale=True)

        await self._run(
            self._imap_lock,
            lambda: backend.mark_seen(ref.folder, ref.validity, ref.uid),
            protocol="IMAP",
            remember=marked,
        )

    async def refresh_inbox(self) -> MailboxStats:
        backend, folder = self.backend, self.account.inbox
        return await self._run(
            self._imap_lock,
            lambda: backend.stats(folder, self._stats.get(folder)),
            protocol="IMAP",
            remember=lambda stats: self._remember(folder, stats),
        )

    async def reconnect(self) -> None:
        def reset(_):
            self._status = replace(self._status, connection="unknown", stats_stale=True)

        await self._run(self._imap_lock, self.backend.close, protocol="IMAP", remember=reset)

    async def check_connections(self) -> ConnectionReport:
        self._open()
        backend = self.backend

        async def check():
            imap, smtp = await asyncio.gather(
                self._run(self._imap_lock, backend.check_imap, protocol="IMAP"),
                self._run(self._smtp_lock, backend.check_smtp, protocol="SMTP"),
            )
            errors = " ".join(result.error for result in (imap, smtp) if not result.ok)
            self._status = replace(
                self._status,
                connection="connected" if imap.ok and smtp.ok else "error",
                last_error=errors,
                stats_stale=self._status.stats_stale or not imap.ok,
            )
            return ConnectionReport(imap, smtp, datetime.now(UTC))

        return await asyncio.shield(self._track(check()))

    async def send(self, draft: Draft) -> SendOutcome:
        self._open()
        if self._send_task is not None and not self._send_task.done():
            raise RuntimeError("A message is already being sent for this account")
        snapshot = replace(draft)
        backend = self.backend
        self._status = replace(self._status, send_state="sending")
        self._send_task = self._track(self._send(snapshot, backend))
        return await asyncio.shield(self._send_task)

    async def _send(self, snapshot: Draft, backend) -> SendOutcome:
        try:
            if self.drafts.load().empty:
                self.drafts.save(snapshot)
        except Exception:
            error = "Draft could not be saved. Message was not sent."
            self._status = replace(self._status, send_state="failed", last_error=error)
            return SendOutcome(False, error=error)
        async with self._smtp_lock:
            try:
                raw, result = await asyncio.get_running_loop().run_in_executor(
                    self._smtp_executor, backend.send, snapshot
                )
            except Exception as error:
                message = connection_error(error, "SMTP")
                definite = isinstance(
                    error,
                    (
                        ValueError,
                        ssl.SSLCertVerificationError,
                        smtplib.SMTPAuthenticationError,
                        smtplib.SMTPRecipientsRefused,
                        smtplib.SMTPSenderRefused,
                        smtplib.SMTPDataError,
                    ),
                )
                if not definite:
                    message += (
                        " Submission may have reached the server. Check Sent before retrying."
                    )
                refused = (
                    error.recipients if isinstance(error, smtplib.SMTPRecipientsRefused) else {}
                )
                self._status = replace(self._status, send_state="failed", last_error=message)
                return SendOutcome(False, refused=refused, error=message)
        warnings = [result.warning] if result.warning else []
        try:
            await self._run(
                self._imap_lock,
                lambda: backend.append_sent(raw),
                protocol="IMAP",
                finalizing=True,
            )
        except Exception:
            warnings.append("Message accepted, but the Sent copy could not be saved.")
        try:
            if self.drafts.load() == snapshot:
                self.drafts.clear()
            else:
                warnings.append("A newer draft was preserved.")
        except Exception:
            warnings.append("Message accepted, but draft cleanup failed. Do not resend this draft.")
        self._status = replace(self._status, send_state="accepted")
        return SendOutcome(True, result.refused, tuple(warnings))

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await self._run(self._imap_lock, self.backend.close, protocol="IMAP", finalizing=True)
        finally:
            self._imap_executor.shutdown(wait=False)
            self._smtp_executor.shutdown(wait=False)
            self._status = replace(self._status, connection="closed")
