from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class MailHeader:
    uid: int
    subject: str
    sender: str
    date: datetime | None
    seen: bool
    size: int = 0
    message_id: str = ""
    references: tuple[str, ...] = ()
    in_reply_to: str = ""

    @property
    def searchable(self) -> str:
        return f"{self.sender} {self.subject}".casefold()


@dataclass(frozen=True)
class MailBody:
    uid: int
    subject: str
    sender: str
    to: str
    date: str
    markdown: str
    attachments: tuple[str, ...] = ()
    truncated: bool = False
    cc: str = ""
    reply_to: str = ""
    message_id: str = ""
    in_reply_to: str = ""
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class MailPage:
    headers: list[MailHeader]
    total: int
    next_end: int
    validity: int


@dataclass
class Draft:
    to: str = ""
    cc: str = ""
    subject: str = ""
    body: str = ""
    in_reply_to: str = ""
    references: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not any((self.to, self.cc, self.subject, self.body))


@dataclass(frozen=True)
class SendResult:
    warning: str = ""
    refused: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MessageRef:
    account: str
    folder: str
    validity: int
    uid: int


@dataclass(frozen=True)
class MailboxStats:
    """Server counts and the successful UID watermark used by the next check."""

    total: int
    unread: int
    validity: int
    uidnext: int
    checked_at: datetime
    new_count: int | None = None


@dataclass(frozen=True)
class MailboxSnapshot:
    page: MailPage
    stats: MailboxStats


@dataclass(frozen=True)
class ConnectionResult:
    ok: bool
    error: str = ""


@dataclass(frozen=True)
class ConnectionReport:
    imap: ConnectionResult
    smtp: ConnectionResult
    checked_at: datetime


@dataclass(frozen=True)
class SessionStatus:
    """Inbox timestamps survive failed checks; stale marks their reduced freshness."""

    connection: Literal["unknown", "connected", "error", "closed"] = "unknown"
    inbox: MailboxStats | None = None
    last_sync: datetime | None = None
    last_error: str = ""
    send_state: Literal["idle", "sending", "accepted", "failed"] = "idle"
    stats_stale: bool = True


@dataclass(frozen=True)
class SendOutcome:
    """Acceptance is final, independently of recipient refusals or local finalization."""

    accepted: bool
    refused: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error: str = ""
