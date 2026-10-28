import socketserver
import ssl
import threading

import pytest
from test_protocols import tls as tls

from kwikmail.backend import ImapBackend
from kwikmail.config import Account


@pytest.fixture
def imap_starttls_server(tls):
    context, ca_file = tls
    commands = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            connection = self.request
            stream = connection.makefile("rb")
            connection.sendall(b"* OK local IMAP ready\r\n")
            try:
                while line := stream.readline():
                    tag, command = line.rstrip(b"\r\n").split(b" ", 1)
                    verb = command.split(b" ", 1)[0]
                    commands.append(verb)
                    if verb == b"CAPABILITY":
                        connection.sendall(b"* CAPABILITY IMAP4rev1 STARTTLS\r\n")
                    elif verb == b"STARTTLS":
                        connection.sendall(tag + b" OK upgrade\r\n")
                        stream.close()
                        connection = context.wrap_socket(connection, server_side=True)
                        stream = connection.makefile("rb")
                        continue
                    elif verb not in (b"LOGIN", b"NOOP", b"LOGOUT"):
                        connection.sendall(tag + b" BAD unsupported\r\n")
                        continue
                    connection.sendall(tag + b" OK complete\r\n")
                    if verb == b"LOGOUT":
                        break
            except (ssl.SSLError, OSError):
                pass
            finally:
                stream.close()
                connection.close()

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], ca_file, commands
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def account_for(server, trusted=True):
    port, ca_file, _ = server
    return Account(
        "localhost",
        "user",
        "user@example.com",
        imap_port=port,
        imap_security="starttls",
        ca_file=ca_file if trusted else None,
    )


@pytest.mark.parametrize("trusted", [True, False])
def test_imap_starttls_checks_certificate_before_authentication(
    imap_starttls_server, monkeypatch, trusted
):
    monkeypatch.setenv("KWIKMAIL_PASSWORD", "test-only")
    backend = ImapBackend(account_for(imap_starttls_server, trusted))
    result = backend.check_imap()
    assert result.ok is trusted
    assert (b"LOGIN" in imap_starttls_server[2]) is trusted
    if not trusted:
        assert result.error == "IMAP: certificate verification failed."


def test_imap_starttls_cleanup_preserves_original_certificate_error(
    imap_starttls_server, monkeypatch
):
    monkeypatch.setenv("KWIKMAIL_PASSWORD", "test-only")
    backend = ImapBackend(account_for(imap_starttls_server, trusted=False))
    with pytest.raises(ssl.SSLCertVerificationError):
        backend.connection()
    assert b"LOGIN" not in imap_starttls_server[2]
    assert backend.client is None
