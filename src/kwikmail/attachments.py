"""Complete MIME attachments and exclusive, owner-only file downloads."""

import os
import re
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path

from .mail import one_line


@dataclass(frozen=True)
class Attachment:
    name: str
    content_type: str
    data: bytes


def attachment_parts(message: Message):
    # Attached messages are files; do not also export their nested MIME parts.
    if message.get_content_disposition() == "attachment" or message.get_filename():
        yield message
    elif message.is_multipart():
        for part in message.get_payload():
            yield from attachment_parts(part)


def extract_attachments(raw: bytes) -> list[Attachment]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    result = []
    for part in attachment_parts(message):
        data = part.get_payload(decode=True)
        if data is None and part.is_multipart():
            data = b"\r\n".join(child.as_bytes(policy=policy.SMTP) for child in part.get_payload())
        result.append(
            Attachment(
                one_line(
                    part.get_filename() or "attachment.eml"
                    if part.get_content_type() == "message/rfc822"
                    else part.get_filename() or "attachment.bin"
                ),
                part.get_content_type(),
                data or b"",
            )
        )
    return result


def safe_name(value: str) -> str:
    name = re.sub(r"[^\w.() -]", "_", one_line(value).replace("\\", "/").split("/")[-1])
    return name.strip(" .").encode("utf-8")[:160].decode("utf-8", "ignore") or "attachment.bin"


def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def save_attachments(raw: bytes, directory: Path) -> list[dict]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    results = []
    for index, attachment in enumerate(extract_attachments(raw), 1):
        name = f"{index:03d}-{safe_name(attachment.name)}"
        path = directory / name
        write_private(path, attachment.data)
        results.append(
            {
                "name": attachment.name,
                "content_type": attachment.content_type,
                "size": len(attachment.data),
                "path": str(path),
            }
        )
    return results
