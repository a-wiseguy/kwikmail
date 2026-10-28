from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class MailHeader:
    uid: int
    subject: str
    sender: str
    date: datetime | None
    seen: bool
    size: int = 0

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

    @property
    def empty(self) -> bool:
        return not any((self.to, self.cc, self.subject, self.body))


@dataclass(frozen=True)
class SendResult:
    warning: str = ""
    refused: dict = field(default_factory=dict)
