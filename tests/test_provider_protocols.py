import base64
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult
from test_protocols import RAW
from test_protocols import imap_server as imap_server
from test_protocols import tls as tls

from kwikmail.backend import ImapBackend
from kwikmail.config import Account
from kwikmail.models import Draft
from kwikmail.oauth import LoginRequired
from kwikmail.session import AccountSession
from kwikmail.storage import DraftStore


def local_account(imap_server):
    port, ca_file, *_ = imap_server
    return Account(
        "localhost",
        "you@example.com",
        "you@example.com",
        imap_port=port,
        ca_file=ca_file,
        provider="gmail",
        auth="oauth2",
    )


def test_imap_xoauth2_on_tls_and_rejection(imap_server):
    *_, state = imap_server
    backend = ImapBackend(
        local_account(imap_server), tokens=SimpleNamespace(access_token=lambda: "test-token")
    )
    assert backend.check_imap().ok
    assert state["oauth_payload"] == b"user=you@example.com\x01auth=Bearer test-token\x01\x01"
    assert not any(command.startswith(b"LOGIN") for command in imap_server[2])
    state["reject_login"] = True
    assert backend.check_imap().error == "IMAP: authentication failed."


@pytest.mark.parametrize(
    "folder,special,expected", [("", False, 0), ("auto", True, 1), ("Sent", False, 1)]
)
def test_sent_copy_policy(imap_server, monkeypatch, folder, special, expected):
    monkeypatch.setenv("KWIKMAIL_PASSWORD", "test-only")
    imap_server[4]["special_sent"] = special
    account = replace(local_account(imap_server), auth="password", sent_folder=folder)
    backend = ImapBackend(account)
    try:
        backend.append_sent(RAW)
        assert len(imap_server[3]) == expected
        if not expected:
            assert not imap_server[2]
    finally:
        backend.close()


def test_auto_sent_without_special_use_requires_configuration(imap_server, monkeypatch):
    monkeypatch.setenv("KWIKMAIL_PASSWORD", "test-only")
    backend = ImapBackend(replace(local_account(imap_server), auth="password", sent_folder="auto"))
    try:
        with pytest.raises(ValueError, match="sent_folder"):
            backend.append_sent(RAW)
        assert not imap_server[3]
    finally:
        backend.close()


@pytest.mark.parametrize("security", ["tls", "starttls"])
@pytest.mark.parametrize("challenge", [False, True])
def test_smtp_xoauth2_over_tls(tls, security, challenge):
    context, ca_file = tls
    payloads, received = [], []

    class Handler:
        async def auth_XOAUTH2(self, server, args):
            if challenge:
                await server.push("334 ")
                encoded = await server._reader.readline()
            else:
                encoded = args[1].encode()
            payloads.append(base64.b64decode(encoded))
            return AuthResult(success=not challenge, handled=False)

        async def handle_DATA(self, server, session, envelope):
            received.append(envelope)
            return "250 accepted"

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    kwargs = (
        {"tls_context": context, "require_starttls": True}
        if security == "starttls"
        else {"ssl_context": context, "auth_require_tls": False}
    )
    controller = Controller(
        Handler(), hostname="127.0.0.1", port=port, auth_required=True, **kwargs
    )
    controller.start()
    backend = ImapBackend(
        Account(
            "localhost",
            "you@example.com",
            "you@example.com",
            smtp_port=port,
            smtp_security=security,
            ca_file=ca_file,
            auth="oauth2",
            provider="outlook",
        ),
        tokens=SimpleNamespace(access_token=lambda: "test-token"),
    )
    try:
        if challenge:
            # rejection challenge must receive an empty response, never another bearer token
            assert not backend.check_smtp().ok
            assert payloads == [b""]
        else:
            backend.send(Draft("friend@example.com", subject="OAuth"))
            assert payloads == [b"user=you@example.com\x01auth=Bearer test-token\x01\x01"]
            assert len(received) == 1
    finally:
        controller.stop()


async def test_expired_credentials_stop_submission_without_resend(tmp_path):
    calls = []

    def fail():
        calls.append("token")
        raise LoginRequired()

    account = Account("localhost", "you", "you@example.com", auth="oauth2", provider="outlook")
    backend = ImapBackend(account, tokens=SimpleNamespace(access_token=fail))
    # transport is connected; authentication fails before a message can be submitted
    import smtplib
    from unittest.mock import Mock, patch

    smtp = Mock()
    session = AccountSession("home", "Home", backend, DraftStore(tmp_path / "draft.json"))
    draft = Draft("friend@example.com")
    session.drafts.save(draft)
    try:
        with patch.object(smtplib, "SMTP", return_value=smtp):
            outcome = await session.send(draft)
        assert not outcome.accepted and "login required" in outcome.error
        assert calls == ["token"]
        smtp.send_message.assert_not_called()
        smtp.close.assert_called_once()
        assert session.drafts.load() == draft
    finally:
        await session.close()


async def test_oauth_protocol_diagnostics_are_independent(imap_server, tmp_path, monkeypatch):
    import smtplib
    from unittest.mock import Mock

    account = replace(local_account(imap_server), provider="microsoft365")
    backend = ImapBackend(account, tokens=SimpleNamespace(access_token=lambda: "test-token"))
    smtp = Mock()
    smtp.auth.side_effect = smtplib.SMTPAuthenticationError(
        535, b"SmtpClientAuthentication is disabled; private-detail"
    )
    monkeypatch.setattr(smtplib, "SMTP", lambda *args, **kwargs: smtp)
    session = AccountSession("home", "Home", backend, DraftStore(tmp_path / "draft.json"))
    try:
        report = await session.check_connections()
        assert report.imap.ok and not report.smtp.ok
        assert "policy" in report.smtp.error
        assert "private-detail" not in report.smtp.error
    finally:
        await session.close()
