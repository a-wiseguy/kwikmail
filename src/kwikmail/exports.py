"""Date-bounded mailbox selection and lossless message exports."""

import calendar
import hashlib
import json
import re
import tempfile
import uuid
from datetime import date, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path

from .attachments import extract_attachments, save_attachments, write_private

INVOICE_WORDS = ("invoice", "factuur", "payment", "betaling", "receipt", "rekening")


def date_range(*, since=None, before=None, quarter=None, last=None, today=None):
    today = today or date.today()
    if sum((bool(since or before), bool(quarter), bool(last))) > 1:
        raise ValueError("Use dates, --quarter, or --last, not a combination.")
    if quarter:
        match = re.fullmatch(r"(\d{4})-[Qq]([1-4])", quarter)
        if not match:
            raise ValueError("Quarter must be YYYY-Q1 through YYYY-Q4.")
        year, number = map(int, match.groups())
        start = date(year, number * 3 - 2, 1)
        end = date(year + (number == 4), 1 if number == 4 else number * 3 + 1, 1)
    elif last == "quarter":
        end = date(today.year, (today.month - 1) // 3 * 3 + 1, 1)
        previous = end - timedelta(days=1)
        start = date(previous.year, (previous.month - 1) // 3 * 3 + 1, 1)
    elif last == "3months":
        month = today.year * 12 + today.month - 1 - 3
        year, month = divmod(month, 12)
        start = date(year, month + 1, min(today.day, calendar.monthrange(year, month + 1)[1]))
        end = today + timedelta(days=1)
    elif last:
        match = re.fullmatch(r"([1-9]\d*)d", last)
        if not match:
            raise ValueError(
                "--last accepts quarter, 3months, or a positive day count such as 90d."
            )
        end = today + timedelta(days=1)
        start = end - timedelta(days=int(match[1]))
    else:
        if not since:
            raise ValueError("Supply --since, --quarter, or --last to bound the scan.")
        start = date.fromisoformat(since)
        end = date.fromisoformat(before) if before else today + timedelta(days=1)
    if start >= end:
        raise ValueError("The start date must precede the exclusive end date.")
    return start, end


def matches(raw, mode, keywords):
    if mode == "all":
        return True
    if extract_attachments(raw):
        return True
    if mode == "attachments":
        return False
    message = BytesParser(policy=policy.default).parsebytes(raw)
    text = [str(message.get("Subject", ""))]

    def collect(part):
        if part.get_content_disposition() == "attachment" or part.get_filename():
            return
        if part.is_multipart():
            for child in part.get_payload():
                collect(child)
        elif part.get_content_maintype() == "text":
            try:
                text.append(part.get_content(errors="replace"))
            except LookupError:
                text.append((part.get_payload(decode=True) or b"").decode("utf-8", "replace"))

    collect(message)
    haystack = "\n".join(text).casefold()
    return any(word.casefold() in haystack for word in keywords)


def export_directory(output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix="kwikmail-", dir=output))


def export_message(directory, index, raw):
    target = directory / f"{index:06d}"
    while target.exists():
        index += 1
        target = directory / f"{index:06d}"
    target.mkdir(mode=0o700)
    path = target / "message.eml"
    write_private(path, raw)
    attachments = save_attachments(raw, target / "attachments")
    for attachment in attachments:
        attachment["sha256"] = hashlib.sha256(Path(attachment["path"]).read_bytes()).hexdigest()
    return {
        "eml": str(path),
        "eml_sha256": hashlib.sha256(raw).hexdigest(),
        "attachments": attachments,
    }


def validate_export_files(directory, report):
    for item in report["messages"]:
        path = Path(item["eml"])
        if (
            not path.resolve().is_relative_to(directory)
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != item["eml_sha256"]
        ):
            raise ValueError(
                "Exported message missing or changed; restore files or start a new export."
            )
        for attachment in item["attachments"]:
            path = Path(attachment["path"])
            if (
                not path.resolve().is_relative_to(directory)
                or not path.is_file()
                or path.stat().st_size != attachment["size"]
                or hashlib.sha256(path.read_bytes()).hexdigest() != attachment["sha256"]
            ):
                raise ValueError("Exported attachment missing or changed; start a new export.")


def save_manifest(directory, manifest):
    temporary = directory / f".manifest-{uuid.uuid4().hex}.tmp"
    try:
        write_private(temporary, json.dumps(manifest, ensure_ascii=True, indent=2).encode())
        temporary.replace(directory / "manifest.json")
    finally:
        temporary.unlink(missing_ok=True)
