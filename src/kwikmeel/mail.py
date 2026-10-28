"""MIME and address handling. No network or UI dependencies."""

import re
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, formataddr, getaddresses, make_msgid, parsedate_to_datetime

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from markdownify import markdownify

from .config import Account
from .models import Draft, MailBody, MailHeader


def clean(value: str) -> str:
    # Email is untrusted terminal input. Strip escape/control characters and bidi overrides.
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]", "", value)


def one_line(value: str) -> str:
    return " ".join(clean(value).split())


def parse_header(uid: int, raw: bytes, flags: tuple, size: int = 0) -> MailHeader:
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    try:
        date = parsedate_to_datetime(str(message.get("Date", "")))
    except (TypeError, ValueError, OverflowError):
        date = None
    return MailHeader(
        uid,
        one_line(str(message.get("Subject", "(no subject)"))),
        one_line(str(message.get("From", "(unknown sender)"))),
        date,
        b"\\Seen" in flags,
        size,
    )


def parse_body(uid: int, raw: bytes, truncated: bool = False) -> MailBody:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    part = message.get_body(preferencelist=("plain", "html"))
    body = "No readable text in this message."
    if part:
        try:
            body = part.get_content(errors="replace")
        except LookupError:
            body = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            soup = BeautifulSoup(body, "html.parser")
            for element in soup(["script", "style", "img", "iframe", "object"]):
                element.decompose()
            body = markdownify(str(soup), heading_style="ATX")
    # Keep pathological messages from constructing thousands of Markdown widgets.
    truncated = truncated or len(body) > 80_000
    body = clean(body[:80_000])
    attachments = tuple(
        one_line(part.get_filename() or "unnamed attachment")
        for part in message.walk()
        if part.get_content_disposition() == "attachment"
    )
    return MailBody(
        uid,
        one_line(str(message.get("Subject", "(no subject)"))),
        one_line(str(message.get("From", ""))),
        one_line(str(message.get("To", ""))),
        one_line(str(message.get("Date", ""))),
        body,
        attachments,
        truncated,
    )


def addresses(value: str, required: bool = False) -> list[tuple[str, str]]:
    if "\n" in value or "\r" in value:
        raise ValueError("Addresses must fit on one line.")
    parsed = getaddresses([value]) if value.strip() else []
    if (required and not parsed) or any(
        not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", address) for _, address in parsed
    ):
        raise ValueError("Enter valid email addresses, separated by commas.")
    # SMTPUTF8 is deliberately outside v1; Unicode display names remain supported.
    if any(not address.isascii() for _, address in parsed):
        raise ValueError("Internationalized email addresses are not supported in v1.")
    return parsed


def build_message(account: Account, draft: Draft) -> tuple[EmailMessage, list[str]]:
    to = addresses(draft.to, required=True)
    cc = addresses(draft.cc)
    addresses(account.address, required=True)
    if "\n" in draft.subject or "\r" in draft.subject:
        raise ValueError("Subject must fit on one line.")
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = formataddr((account.name, account.address))
    message["To"] = ", ".join(formataddr(pair) for pair in to)
    if cc:
        message["Cc"] = ", ".join(formataddr(pair) for pair in cc)
    message["Subject"] = draft.subject or "(no subject)"
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = make_msgid()
    message.set_content(draft.body)
    renderer = MarkdownIt("commonmark", {"html": False})
    message.add_alternative(renderer.render(draft.body), subtype="html")
    return message, list(dict.fromkeys(address for _, address in to + cc))
