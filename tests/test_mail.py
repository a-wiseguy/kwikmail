import os
from email.message import EmailMessage

import pytest

from kwikmeel.config import Account, load_account
from kwikmeel.mail import build_message, parse_body, parse_header
from kwikmeel.models import Draft
from kwikmeel.storage import DraftStore


def test_mime_alternatives_and_terminal_input():
    message = EmailMessage()
    message["Subject"] = "Café [bold]"
    message["From"] = "Mira <mira@example.com>"
    message.set_content("Hello\x1b[31m\x00 world")
    message.add_alternative("<h1>Wrong alternative</h1>", subtype="html")
    message.add_attachment(b"pdf", maintype="application", subtype="pdf", filename="notes.pdf")
    raw = message.as_bytes()
    header = parse_header(42, raw, ())
    body = parse_body(42, raw)
    assert header.subject == "Café [bold]"
    assert not header.seen
    assert "Hello" in body.markdown and "Wrong alternative" not in body.markdown
    assert "\x1b" not in body.markdown and "\x00" not in body.markdown
    assert body.attachments == ("notes.pdf",)


def test_html_only_strips_active_and_remote_content():
    message = EmailMessage()
    message.set_content(
        "<h1>Hello</h1><script>evil()</script><style>bad</style>"
        '<img src="https://tracker.invalid/pixel"><p>World</p>',
        subtype="html",
    )
    body = parse_body(1, message.as_bytes())
    assert "# Hello" in body.markdown
    assert "evil" not in body.markdown and "tracker" not in body.markdown


def test_unknown_charset_and_large_message():
    body = parse_body(1, b'Content-Type: text/plain; charset="no-such-charset"\r\n\r\nhello')
    assert body.markdown == "hello"
    body = parse_body(2, b"Content-Type: text/plain\r\n\r\n" + b"a" * 90_000)
    assert body.truncated and len(body.markdown) == 80_000


def test_build_multipart_and_recipients():
    account = Account("localhost", "you@example.com", "you@example.com", "You")
    draft = Draft("Mira <mira@example.com>", "noor@example.com", "Update", "# Hi\n\n**Ready**")
    message, recipients = build_message(account, draft)
    assert recipients == ["mira@example.com", "noor@example.com"]
    assert message["Message-ID"] and message["Date"]
    assert message.get_body(("plain",)).get_content().startswith("# Hi")
    assert "<strong>Ready</strong>" in message.get_body(("html",)).get_content()


@pytest.mark.parametrize(
    "to", ["", "bad address", "a@example.com\nBcc: b@example.com", "é@example.com"]
)
def test_invalid_recipients(to):
    with pytest.raises(ValueError):
        build_message(Account("localhost", "you@example.com", "you@example.com"), Draft(to=to))


def test_draft_round_trip_and_permissions(tmp_path):
    store = DraftStore(tmp_path / "account/draft.json")
    draft = Draft("mira@example.com", subject="Draft", body="Text\n\n**Markdown**")
    store.save(draft)
    assert store.load() == draft
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    assert not list(store.path.parent.glob(".draft-*"))
    store.clear()
    assert store.load().empty


def test_config_validation_and_password(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    base = '[account]\nhost="mail.example.com"\nusername="you"\naddress="you@example.com"\n'
    path.write_text(base + 'imap_security="none"\n')
    with pytest.raises(ValueError, match="imap_security"):
        load_account(path)
    path.write_text(base)
    monkeypatch.setenv("KWIKMEEL_PASSWORD", "private-test-password")
    assert load_account(path).password() == "private-test-password"
