"""Exercise IMAPClient and smtplib against local TLS socket servers, no real mailbox."""

import re
import socket
import socketserver
import ssl
import threading
from dataclasses import replace
from email import policy
from email.parser import BytesParser

import pytest
import trustme
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

from kwikmeel.backend import HEADER_FIELD, ImapBackend, MailboxChanged
from kwikmeel.config import Account
from kwikmeel.models import Draft

RAW = (
    b"From: Mira <mira@example.com>\r\nTo: you@example.com\r\n"
    b"Subject: Hello from IMAP\r\nDate: Wed, 28 Oct 2026 12:00:00 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n# Hello\r\n\r\nA real socket test.\r\n"
)


@pytest.fixture
def tls(tmp_path):
    ca = trustme.CA()
    cert = ca.issue_cert("localhost", "127.0.0.1")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.configure_cert(context)
    path = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(path)
    return context, str(path)


@pytest.fixture
def imap_server(tls):
    context, ca_file = tls
    commands = []
    appended = []
    state = {"validity": 42, "uidnext": 104, "uids": [101, 102, 103], "unread": 2}

    class Handler(socketserver.StreamRequestHandler):
        def send(self, data):
            self.wfile.write(data + b"\r\n")
            self.wfile.flush()

        def handle(self):
            self.send(b"* OK local test IMAP ready")
            while line := self.rfile.readline():
                line = line.rstrip(b"\r\n")
                tag, command = line.split(b" ", 1)
                verb = command.split(b" ", 1)[0].upper()
                commands.append(command)
                if verb == b"CAPABILITY":
                    self.send(b"* CAPABILITY IMAP4rev1")
                elif verb == b"LOGIN":
                    if state.get("reject_login"):
                        self.send(tag + b" NO secret-auth-detail")
                        continue
                elif verb == b"NOOP":
                    pass
                elif verb == b"STATUS":
                    if state.get("fail_status"):
                        self.send(tag + b" NO secret-status-detail")
                        continue
                    values = {
                        "MESSAGES": len(state["uids"]),
                        "UNSEEN": state["unread"],
                        "UIDVALIDITY": state["validity"],
                        "UIDNEXT": state["uidnext"],
                    }
                    fields = command.split(b"(")[1].rstrip(b")").decode().split()
                    text = " ".join(f"{key} {values[key]}" for key in fields)
                    self.send(f'* STATUS "INBOX" ({text})'.encode())
                elif verb == b"LIST":
                    self.send(b'* LIST (\\HasNoChildren) "/" "INBOX"')
                    self.send(b'* LIST (\\HasNoChildren) "/" "Sent"')
                    self.send(b'* LIST (\\Noselect) "/" "Parent"')
                elif verb in (b"SELECT", b"EXAMINE"):
                    self.send(b"* 3 EXISTS")
                    self.send(f"* OK [UIDVALIDITY {state['validity']}] stable".encode())
                    self.send(b"* FLAGS (\\Seen)")
                elif verb == b"FETCH" or command.startswith(b"UID FETCH"):
                    is_uid = verb == b"UID"
                    spec = command.split()[2 if is_uid else 1].decode()
                    if ":" in spec:
                        low, high = map(int, spec.split(":"))
                        numbers = range(low, high + 1)
                    else:
                        numbers = [int(n) for n in spec.split(",")]
                    for number in numbers:
                        uid = number if is_uid else 100 + number
                        sequence = uid - 100
                        if b"HEADER.FIELDS" in command:
                            payload = RAW.split(b"\r\n\r\n")[0] + b"\r\n\r\n"
                            field = HEADER_FIELD
                        else:
                            payload = RAW
                            field = b"BODY[]<0>"
                        self.send(
                            f"* {sequence} FETCH (UID {uid} FLAGS () ".encode()
                            + f"RFC822.SIZE {len(RAW)} ".encode()
                            + field
                            + f" {{{len(payload)}}}".encode()
                        )
                        self.wfile.write(payload + b")\r\n")
                        self.wfile.flush()
                elif command.startswith(b"UID SEARCH"):
                    if match := re.search(rb"\{(\d+)\}$", command):
                        self.send(b"+ ready for search literal")
                        self.rfile.read(int(match[1]))
                        self.rfile.readline()
                    if b" UID " in command:
                        low, high = map(int, command.split()[-1].split(b":"))
                        matches = [uid for uid in state["uids"] if low <= uid <= high]
                        self.send(b"* SEARCH " + b" ".join(str(uid).encode() for uid in matches))
                        if state.get("reset_during_search"):
                            state["validity"] += 1
                    else:
                        self.send(b"* SEARCH 101 103")
                elif command.startswith(b"UID STORE"):
                    self.send(b"* 3 FETCH (UID 103 FLAGS (\\Seen))")
                elif verb == b"APPEND":
                    size = int(re.search(rb"\{(\d+)\}$", command)[1])
                    self.send(b"+ ready for literal")
                    appended.append(self.rfile.read(size))
                    self.rfile.readline()
                elif verb == b"LOGOUT":
                    self.send(b"* BYE")
                    self.send(tag + b" OK logout")
                    return
                else:
                    self.send(tag + b" BAD unsupported command")
                    continue
                self.send(tag + b" OK completed")

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

        def get_request(self):
            connection, address = super().get_request()
            return context.wrap_socket(connection, server_side=True), address

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], ca_file, commands, appended, state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_imap_tls_headers_paging_peek_search_seen_append(imap_server, monkeypatch):
    port, ca_file, commands, appended, state = imap_server
    monkeypatch.setenv("KWIKMEEL_PASSWORD", "test-only")
    backend = ImapBackend(
        Account(
            "localhost",
            "you@example.com",
            "you@example.com",
            imap_port=port,
            ca_file=ca_file,
            page_size=2,
        )
    )
    try:
        assert backend.folders() == ["INBOX", "Sent"]
        page = backend.page("INBOX")
        assert [row.uid for row in page.headers] == [103, 102]
        assert page.next_end == 1 and page.total == 3 and page.validity == 42
        assert [row.uid for row in backend.page("INBOX", page.next_end).headers] == [101]
        body = backend.body("INBOX", 42, 103)
        assert body.markdown.startswith("# Hello")
        before = len(commands)
        assert backend.body("INBOX", 42, 103) == body
        assert len(commands) == before  # Cached revisit makes no network request.
        assert not any(b"STORE" in command for command in commands)
        assert any(b"BODY.PEEK[]" in command for command in commands)
        assert [row.uid for row in backend.page("INBOX", query='café "quoted"').headers] == [
            103,
            101,
        ]
        backend.mark_seen("INBOX", 42, 103)
        assert any(b"STORE" in command and b"\\Seen" in command for command in commands)
        backend.append_sent(RAW)
        assert appended == [RAW]
        state["validity"] = 43
        with pytest.raises(MailboxChanged):
            backend.body("INBOX", 42, 102)
    finally:
        backend.close()


@pytest.mark.parametrize("security", ["starttls", "tls"])
def test_smtp_tls_auth_mime_and_partial_refusal(tls, monkeypatch, security):
    context, ca_file = tls
    received = []

    class Handler:
        async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
            if address == "reject@example.com":
                return "550 no such recipient"
            envelope.rcpt_tos.append(address)
            return "250 OK"

        async def handle_DATA(self, server, session, envelope):
            received.append(envelope)
            return "250 accepted"

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    kwargs = (
        {"tls_context": context, "require_starttls": True}
        if security == "starttls"
        else {
            "ssl_context": context,
            "auth_require_tls": False,
        }
    )
    controller = Controller(
        Handler(),
        hostname="127.0.0.1",
        port=port,
        auth_required=True,
        authenticator=lambda *args: AuthResult(success=True),
        **kwargs,
    )
    controller.start()
    monkeypatch.setenv("KWIKMEEL_PASSWORD", "test-only")
    account = Account(
        "localhost",
        "you@example.com",
        "you@example.com",
        smtp_port=port,
        ca_file=ca_file,
        smtp_security=security,
    )
    try:
        raw, result = ImapBackend(account).send(
            Draft("friend@example.com, reject@example.com", subject="MIME test", body="**Hello**")
        )
        assert list(result.refused) == ["reject@example.com"]
        assert len(received) == 1 and received[0].rcpt_tos == ["friend@example.com"]
        message = BytesParser(policy=policy.default).parsebytes(received[0].content)
        assert message["Subject"] == "MIME test"
        assert "<strong>Hello</strong>" in message.get_body(("html",)).get_content()
        assert b"Message-ID:" in raw
        # A wrong trust store must fail before authentication or sending.
        with pytest.raises(ssl.SSLCertVerificationError):
            ImapBackend(replace(account, ca_file=None)).send(Draft("friend@example.com"))
        assert len(received) == 1
    finally:
        controller.stop()
