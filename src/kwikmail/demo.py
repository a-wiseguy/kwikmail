from dataclasses import replace
from datetime import UTC, datetime, timedelta

from .backend import MailboxChanged
from .config import Account
from .mail import build_message
from .models import (
    ConnectionResult,
    Draft,
    MailBody,
    MailboxStats,
    MailHeader,
    MailPage,
    SendResult,
)

WELCOME = """# A quieter place for your inbox

Welcome to **kwikmail**. Your hands can stay on the keyboard.

## Make yourself at home

| Key | Action |
| --- | --- |
| `j` / `k` | Move through messages |
| `/` | Fuzzy find sender or subject |
| `Enter` | Open the reader |
| `f` | Jump to a folder |
| `c` | Write a message |
| `?` | All shortcuts |

Previewing a message leaves it unread. Opening the reader marks it read.

## Small by design

Headers arrive in batches. Bodies load when you pause on a message.
Search the entire folder with `s`, or filter loaded headers instantly with `/`.

> This is a demo mailbox. Sending here never contacts a mail server.

Try `c` to open the Markdown composer. `Ctrl+S` saves your draft;
`Ctrl+Enter` opens a send confirmation.

Enjoy the quiet.
"""


class DemoBackend:
    def __init__(self, count: int = 240, account: Account | None = None):
        self.account = account or Account("demo.local", "you@example.com", "you@example.com", "You")
        self.items: dict[str, list[MailHeader]] = {}
        self.sent: list[Draft] = []
        subjects = [
            ("Mira Chen", "The new homelab is online"),
            ("Forge", "[infra/mail] Backup job completed"),
            ("Noor", "Coffee on Friday?"),
            ("Buildkite", "Build #482 passed · main"),
            ("Robin", "Notes from the weekend"),
            ("Self-hosted Weekly", "Issue 42: smaller tools, better systems"),
            ("Alex", "Re: terminal workflow ideas"),
            ("Dovecot", "Your mailbox migration is complete"),
        ]
        now = datetime.now(UTC)
        for folder, size in (("INBOX", count), ("Sent", 12), ("Archive", 38), ("Drafts", 0)):
            rows = []
            for index in range(size):
                name, subject = subjects[index % len(subjects)]
                rows.append(
                    MailHeader(
                        size - index,
                        "Welcome to kwikmail" if index == 0 else subject,
                        "Kwikmail <hello@kwikmail.local>"
                        if index == 0
                        else f"{name} <{name.lower().split()[0]}@example.com>",
                        now - timedelta(minutes=index * 37),
                        index % 4 != 0,
                        2048,
                        message_id=f"<demo-{folder}-{size - index}@{self.account.host}>",
                        references=(
                            (
                                f"<demo-{folder}-{(size - index - 1) // 3 * 3 + 1}"
                                f"@{self.account.host}>",
                            )
                            if (size - index - 1) % 3
                            else ()
                        ),
                    )
                )
            self.items[folder] = rows
        self._uidnext = {
            folder: max((row.uid for row in rows), default=0) + 1
            for folder, rows in self.items.items()
        }

    def folders(self) -> list[str]:
        return list(self.items)

    def page(self, folder: str, end=None, query="", validity=None) -> MailPage:
        if validity not in (None, 1):
            raise MailboxChanged("Demo mailbox changed. Refresh to reload it.")
        rows = self.items[folder]
        if query:
            rows = [
                row
                for row in rows
                if query.casefold() in row.searchable or query.casefold() in WELCOME.casefold()
            ]
        stop = min(end if end is not None else len(rows), len(rows))
        start = max(0, stop - self.account.page_size)
        return MailPage(list(reversed(list(reversed(rows))[start:stop])), len(rows), start, 1)

    def stats(self, folder: str, previous: MailboxStats | None = None) -> MailboxStats:
        rows = self.items[folder]
        uidnext = max(self._uidnext[folder], max((row.uid for row in rows), default=0) + 1)
        self._uidnext[folder] = uidnext
        new_count = (
            sum(previous.uidnext <= row.uid < uidnext for row in rows)
            if previous is not None and previous.validity == 1
            else None
        )
        return MailboxStats(
            len(rows), sum(not row.seen for row in rows), 1, uidnext, datetime.now(UTC), new_count
        )

    def check_imap(self) -> ConnectionResult:
        return ConnectionResult(True)

    def check_smtp(self) -> ConnectionResult:
        return ConnectionResult(True)

    def body(self, folder: str, validity: int, uid: int) -> MailBody:
        if validity != 1:
            raise MailboxChanged("Demo mailbox changed. Refresh to reload it.")
        row = next(item for item in self.items[folder] if item.uid == uid)
        content = (
            WELCOME
            if uid == self.items[folder][0].uid
            else (
                f"# {row.subject}\n\nHey,\n\n"
                "A quick update from the lab. Everything is looking good.\n\n"
                "## What changed\n\n- Mail is running on our own server\n"
                "- Backups finished overnight\n"
                "- The terminal workflow is coming together\n\n"
                "```sh\ndocker compose ps\n```\n\n"
                "> Keep the tools small, and keep the feedback loop short.\n\n"
                "Let me know what you think.\n\nCheers!"
            )
        )
        return MailBody(
            uid,
            row.subject,
            row.sender,
            self.account.address,
            row.date.strftime("%a, %d %b %Y %H:%M %z") if row.date else "",
            content,
            attachments=("invoice.txt",) if uid % 5 == 0 else (),
            message_id=row.message_id,
            references=row.references,
            in_reply_to=row.references[-1] if row.references else "",
        )

    def range_uids(self, folder, since, before):
        return 1, sorted(
            row.uid for row in self.items[folder] if row.date and since <= row.date.date() < before
        )

    def raw(self, folder, validity, uid, max_bytes=50_000_000):
        from email.message import EmailMessage

        body = self.body(folder, validity, uid)
        message = EmailMessage()
        for key, value in (
            ("Subject", body.subject),
            ("From", body.sender),
            ("To", body.to),
            ("Date", body.date),
        ):
            message[key] = value
        message.set_content(body.markdown)
        if uid % 5 == 0:
            message.add_attachment(
                b"demo invoice\n",
                maintype="application",
                subtype="octet-stream",
                filename="invoice.txt",
            )
        return message.as_bytes()

    def mark_seen(self, folder: str, validity: int, uid: int) -> None:
        if validity != 1:
            raise MailboxChanged("Demo mailbox changed. Refresh to reload it.")
        self.items[folder] = [
            replace(row, seen=True) if row.uid == uid else row for row in self.items[folder]
        ]

    def send(self, draft: Draft) -> tuple[bytes, SendResult]:
        message, _ = build_message(self.account, draft)
        self.sent.append(replace(draft))
        return message.as_bytes(), SendResult(warning="Demo only. No email was delivered.")

    def append_sent(self, raw: bytes) -> None:
        pass

    def close(self) -> None:
        pass
