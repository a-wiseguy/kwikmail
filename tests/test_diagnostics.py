import socket
import ssl
from dataclasses import replace

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult
from test_protocols import imap_server as imap_server
from test_protocols import tls as tls

from kwikmeel.backend import ImapBackend, MailboxChanged
from kwikmeel.config import Account
from kwikmeel.session import AccountSession
from kwikmeel.storage import DraftStore


def local_account(imap_server):
    port, ca_file, _, _, _ = imap_server
    return Account(
        "localhost", "you@example.com", "you@example.com", imap_port=port, ca_file=ca_file
    )


def test_stats_use_unseen_and_count_existing_uid_range(imap_server, monkeypatch):
    monkeypatch.setenv("KWIKMEEL_PASSWORD", "test-only")
    _, _, commands, _, state = imap_server
    backend = ImapBackend(local_account(imap_server))
    try:
        baseline = backend.stats("INBOX")
        assert (baseline.total, baseline.unread, baseline.validity, baseline.uidnext) == (
            3,
            2,
            42,
            104,
        )
        assert baseline.new_count is None
        assert not any(b"SEARCH" in command for command in commands)
        state["uids"] = [101, 103, 105, 109]
        state["uidnext"] = 111
        state["unread"] = 1
        observed = backend.stats("INBOX", baseline)
        assert observed.new_count == 2 and observed.total == 4 and observed.unread == 1
        assert any(b"UID SEARCH UID 104:110" in command for command in commands)
        assert not any(b"*" in command for command in commands)
        assert backend.stats("INBOX", observed).new_count == 0
        state["validity"] = 43
        state["uidnext"] = 4
        state["uids"] = [1, 2, 3]
        assert backend.stats("INBOX", observed).new_count is None
    finally:
        backend.close()


async def test_failed_and_reset_checks_preserve_successful_watermark(
    imap_server, monkeypatch, tmp_path
):
    monkeypatch.setenv("KWIKMEEL_PASSWORD", "test-only")
    _, _, _, _, state = imap_server
    session = AccountSession(
        "home", "Home", ImapBackend(local_account(imap_server)), DraftStore(tmp_path / "draft.json")
    )
    try:
        baseline = await session.refresh_inbox()
        state["uids"] += [105, 110]
        state["uidnext"] = 111
        state["fail_status"] = True
        with pytest.raises(RuntimeError):
            await session.refresh_inbox()
        assert session.status.inbox == baseline
        assert session.status.last_sync == baseline.checked_at and session.status.stats_stale
        state["fail_status"] = False
        observed = await session.refresh_inbox()
        assert observed.new_count == 2
        state["uidnext"] = 120
        state["reset_during_search"] = True
        with pytest.raises(MailboxChanged):
            await session.refresh_inbox()
        assert session.status.inbox == observed
        state["reset_during_search"] = False
        assert (await session.refresh_inbox()).new_count is None
    finally:
        await session.close()


@pytest.fixture(params=["tls", "starttls"])
def smtp_server(tls, request):
    context, ca_file = tls
    state = {"authenticate": True, "logins": 0, "data": []}

    class Handler:
        async def handle_DATA(self, server, session, envelope):
            state["data"].append(envelope)
            return "250 accepted"

    def authenticate(*args):
        state["logins"] += 1
        return AuthResult(
            success=state["authenticate"], handled=False, message="535 secret authentication detail"
        )

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    kwargs = (
        {"tls_context": context, "require_starttls": True}
        if request.param == "starttls"
        else {"ssl_context": context, "auth_require_tls": False}
    )
    controller = Controller(
        Handler(),
        hostname="127.0.0.1",
        port=port,
        auth_required=True,
        authenticator=authenticate,
        **kwargs,
    )
    controller.start()
    yield port, ca_file, request.param, state
    controller.stop()


async def test_checks_authenticate_both_protocols_without_data(
    imap_server, smtp_server, monkeypatch, tmp_path
):
    monkeypatch.setenv("KWIKMEEL_PASSWORD", "test-only")
    smtp_port, _, security, smtp_state = smtp_server
    _, _, commands, _, imap_state = imap_server
    account = replace(local_account(imap_server), smtp_port=smtp_port, smtp_security=security)
    session = AccountSession("home", "Home", ImapBackend(account), DraftStore(tmp_path / "draft"))
    try:
        report = await session.check_connections()
        assert report.imap.ok and report.smtp.ok
        assert smtp_state["logins"] == 1 and smtp_state["data"] == []
        assert any(command.startswith(b"LOGIN") for command in commands)
        imap_state["reject_login"] = True
        report = await session.check_connections()
        assert not report.imap.ok and report.smtp.ok
        assert "secret" not in report.imap.error
        imap_state["reject_login"] = False
        smtp_state["authenticate"] = False
        report = await session.check_connections()
        assert report.imap.ok and not report.smtp.ok
        assert "secret" not in report.smtp.error
        assert smtp_state["data"] == []
        assert session.status.connection == "error"
    finally:
        await session.close()


def test_connection_checks_reject_untrusted_certificates(imap_server, smtp_server, monkeypatch):
    monkeypatch.setenv("KWIKMEEL_PASSWORD", "test-only")
    smtp_port, _, security, smtp_state = smtp_server
    account = replace(
        local_account(imap_server), smtp_port=smtp_port, smtp_security=security, ca_file=None
    )
    backend = ImapBackend(account)
    assert not backend.check_imap().ok
    smtp = backend.check_smtp()
    assert not smtp.ok and "certificate verification" in smtp.error
    assert smtp_state["logins"] == 0 and smtp_state["data"] == []


def test_submission_remains_accepted_when_smtp_close_fails(monkeypatch):
    class SMTP:
        def send_message(self, *args, **kwargs):
            return {"refused@example.com": (550, b"no")}

        def close(self):
            raise ssl.SSLError("close failed after acceptance")

    from kwikmeel.models import Draft

    backend = ImapBackend(Account("example.com", "you", "you@example.com"))
    monkeypatch.setattr(backend, "_smtp_connection", lambda: SMTP())
    raw, result = backend.send(Draft("friend@example.com,refused@example.com"))
    assert raw and "refused@example.com" in result.refused
