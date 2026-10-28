from dataclasses import replace
from datetime import UTC, datetime, timedelta

from .config import Account
from .mail import build_message
from .models import Draft, MailBody, MailHeader, MailPage, SendResult

WELCOME = """# A quieter place for your inbox

Welcome to **kwikmeel**. Your hands can stay on the keyboard.

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
    def __init__(self, count: int = 240):
        self.account = Account("demo.local", "you@example.com", "you@example.com", "You")
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
                        "Welcome to kwikmeel" if index == 0 else subject,
                        "Kwikmeel <hello@kwikmeel.local>"
                        if index == 0
                        else f"{name} <{name.lower().split()[0]}@example.com>",
                        now - timedelta(minutes=index * 37),
                        index % 4 != 0,
                        2048,
                    )
                )
            self.items[folder] = rows

    def folders(self) -> list[str]:
        return list(self.items)

    def page(self, folder: str, end=None, query="", validity=None) -> MailPage:
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

    def body(self, folder: str, validity: int, uid: int) -> MailBody:
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
            row.date.strftime("%a, %d %b %Y %H:%M") if row.date else "",
            content,
        )

    def mark_seen(self, folder: str, validity: int, uid: int) -> None:
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
