"""MIME and address handling. No network or UI dependencies."""

import re
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, formataddr, getaddresses, make_msgid, parsedate_to_datetime
from typing import Literal

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from markdownify import markdownify

from .config import Account
from .models import Draft, MailBody, MailHeader

_ID_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_ID_PART = rf"{_ID_ATOM}(?:\.{_ID_ATOM})*"
_ID = re.compile(rf"<{_ID_PART}@{_ID_PART}>")
_MAX_REFERENCES = 50
_MAX_QUOTE = 80_000


def message_ids(value: str) -> tuple[str, ...]:
    """Read bounded ASCII message IDs, keeping the root and latest ancestors."""
    found = dict.fromkeys(
        match.group() for match in _ID.finditer(value[:16_384]) if len(match.group()) <= 254
    )
    ids = tuple(found)
    return ids if len(ids) <= _MAX_REFERENCES else ids[:1] + ids[-(_MAX_REFERENCES - 1) :]


def _references(ancestors: tuple[str, ...], parent: str) -> tuple[str, ...]:
    ids = tuple(value for value in message_ids(" ".join(ancestors)) if value != parent)
    return message_ids(" ".join((*ids, parent)))


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
        next(iter(message_ids(str(message.get("Message-ID", "")))), ""),
        message_ids(str(message.get("References", ""))),
        next(iter(message_ids(str(message.get("In-Reply-To", "")))), ""),
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
        one_line(str(message.get("Cc", ""))),
        one_line(str(message.get("Reply-To", ""))),
        next(iter(message_ids(str(message.get("Message-ID", "")))), ""),
        next(iter(message_ids(str(message.get("In-Reply-To", "")))), ""),
        message_ids(str(message.get("References", ""))),
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


def _reply_addresses(value: str) -> list[tuple[str, str]]:
    try:
        return addresses(value)
    except ValueError:
        return []


def make_response(
    original: MailBody,
    *,
    kind: Literal["reply", "reply_all", "forward"],
    own_addresses: frozenset[str],
) -> Draft:
    """Prepare editable text and ancestry without sending or copying attachments."""
    if kind not in ("reply", "reply_all", "forward"):
        raise ValueError("Unknown response kind.")
    forward = kind == "forward"
    subject = one_line(original.subject)
    prefix = r"^(?:fw|fwd)\s*:" if forward else r"^re\s*:"
    if not re.match(prefix, subject, re.IGNORECASE):
        subject = ("Fwd: " if forward else "Re: ") + subject

    quote = clean(original.markdown[:_MAX_QUOTE])
    quoted = "\n".join("> " + line if line else ">" for line in quote.splitlines())
    if forward:
        headers = ["---------- Forwarded message ----------"]
        for label, value in (
            ("From", original.sender),
            ("Date", original.date),
            ("Subject", original.subject),
            ("To", original.to),
            ("Cc", original.cc),
        ):
            if value:
                headers.append(f"{label}: {one_line(value)}")
        intro = "\n".join(headers)
    else:
        when = f"On {one_line(original.date)}, " if original.date else ""
        intro = f"{when}{one_line(original.sender) or 'Unknown sender'} wrote:"
    body = f"\n\n{intro}\n\n{quoted}"
    if original.truncated or len(original.markdown) > _MAX_QUOTE:
        body += "\n\n[Original message truncated.]"
    if original.attachments:
        body += (
            "\n\n[Attachments omitted: "
            + ", ".join(one_line(name) for name in original.attachments)
            + "]"
        )
    if forward:
        return Draft(subject=subject, body=body)

    own = {address.casefold() for address in own_addresses}
    sender = _reply_addresses(original.sender)
    target = _reply_addresses(original.reply_to) or sender
    self_sent = bool(sender) and all(address.casefold() in own for _, address in sender)
    if self_sent and all(address.casefold() in own for _, address in target):
        target = _reply_addresses(original.to)
    seen = set(own)

    def recipients(pairs: list[tuple[str, str]]) -> str:
        result = []
        for name, address in pairs:
            key = address.casefold()
            if key not in seen:
                seen.add(key)
                result.append(formataddr((name, address)))
        return ", ".join(result)

    to = recipients(target + (_reply_addresses(original.to) if kind == "reply_all" else []))
    cc = recipients(_reply_addresses(original.cc)) if kind == "reply_all" else ""
    parent = next(iter(message_ids(original.message_id)), "")
    ancestry = original.references or ((original.in_reply_to,) if original.in_reply_to else ())
    references = _references(ancestry, parent)
    return Draft(to, cc, subject, body, parent, references)


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
    parent = next(iter(message_ids(draft.in_reply_to)), "")
    references = _references(draft.references, parent)
    if parent:
        message["In-Reply-To"] = parent
    if references:
        message["References"] = " ".join(references)
    message.set_content(draft.body)
    renderer = MarkdownIt("commonmark", {"html": False})
    message.add_alternative(renderer.render(draft.body), subtype="html")
    return message, list(dict.fromkeys(address for _, address in to + cc))
