"""Blocking mail operations. The app serializes IMAP on a dedicated worker thread."""

import smtplib
import ssl
from collections import OrderedDict
from contextlib import suppress

from imapclient import IMAPClient

from .config import Account
from .mail import build_message, parse_body, parse_header
from .models import Draft, MailBody, MailPage, SendResult

HEADER_FIELD = b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]"
HEADER_QUERY = "BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)]"


class MailboxChanged(ValueError):
    pass


class ImapBackend:
    def __init__(self, account: Account):
        self.account = account
        self.client: IMAPClient | None = None
        self.bodies: OrderedDict[tuple[str, int, int], MailBody] = OrderedDict()

    def connection(self) -> IMAPClient:
        if self.client is None:
            account = self.account
            context = ssl.create_default_context(cafile=account.ca_file)
            client = IMAPClient(
                account.host,
                port=account.imap_port,
                ssl=account.imap_security == "tls",
                ssl_context=context,
                timeout=account.timeout,
            )
            try:
                if account.imap_security == "starttls":
                    client.starttls(context)
                client.login(account.username, account.password())
            except Exception:
                client.shutdown()
                raise
            self.client = client
        return self.client

    def close(self) -> None:
        if self.client:
            try:
                self.client.shutdown()
            finally:
                self.client = None

    def folders(self) -> list[str]:
        return [
            name
            for flags, _, name in self.connection().list_folders()
            if b"\\Noselect" not in flags
        ]

    def page(
        self,
        folder: str,
        end: int | None = None,
        query: str = "",
        validity: int | None = None,
    ) -> MailPage:
        client = self.connection()
        info = client.select_folder(folder, readonly=True)
        current_validity = int(info[b"UIDVALIDITY"])
        if validity is not None and current_validity != validity:
            raise MailboxChanged("Mailbox changed on the server. Press R to reload it.")
        count = self.account.page_size
        if query:
            # Dovecot supports UTF-8 search. Keep the user's text a quoted IMAP value.
            uids = sorted(client.search(["TEXT", query], charset="UTF-8"), reverse=True)
            total = len(uids)
            stop = min(end if end is not None else total, total)
            start = max(0, stop - count)
            selected = list(reversed(uids))[start:stop]
            rows = (
                client.fetch(selected, [HEADER_QUERY, "FLAGS", "RFC822.SIZE"]) if selected else {}
            )
        else:
            total = int(info[b"EXISTS"])
            stop = min(end if end is not None else total, total)
            start = max(0, stop - count)
            # Sequence ranges fetch only the newest page, even in a million-message mailbox.
            client.use_uid = False
            try:
                rows = (
                    client.fetch(
                        f"{start + 1}:{stop}", ["UID", HEADER_QUERY, "FLAGS", "RFC822.SIZE"]
                    )
                    if stop
                    else {}
                )
            finally:
                client.use_uid = True
        headers = [
            parse_header(
                int(row.get(b"UID", key)),
                row.get(HEADER_FIELD, b""),
                row.get(b"FLAGS", ()),
                int(row.get(b"RFC822.SIZE", 0)),
            )
            for key, row in rows.items()
        ]
        return MailPage(
            sorted(headers, key=lambda item: item.uid, reverse=True), total, start, current_validity
        )

    def body(self, folder: str, validity: int, uid: int) -> MailBody:
        key = (folder, validity, uid)
        if key in self.bodies:
            self.bodies.move_to_end(key)
            return self.bodies[key]
        client = self.connection()
        info = client.select_folder(folder, readonly=True)
        if int(info[b"UIDVALIDITY"]) != validity:
            raise MailboxChanged("Mailbox changed on the server. Press R to reload it.")
        limit = self.account.max_body_bytes
        rows = client.fetch([uid], [f"BODY.PEEK[]<0.{limit}>", "RFC822.SIZE"])
        if uid not in rows:
            raise ValueError("This message is no longer on the server. Press R to refresh.")
        row = rows[uid]
        raw = row.get(b"BODY[]<0>", row.get(b"BODY[]", b""))
        body = parse_body(uid, raw, int(row.get(b"RFC822.SIZE", 0)) > len(raw))
        self.bodies[key] = body
        if len(self.bodies) > 24:
            self.bodies.popitem(last=False)
        return body

    def mark_seen(self, folder: str, validity: int, uid: int) -> None:
        client = self.connection()
        info = client.select_folder(folder, readonly=False)
        if int(info[b"UIDVALIDITY"]) != validity:
            raise MailboxChanged("Mailbox changed. Refresh before marking messages read.")
        client.add_flags([uid], [b"\\Seen"])

    def append_sent(self, raw: bytes) -> None:
        if self.account.sent_folder:
            self.connection().append(self.account.sent_folder, raw, flags=[b"\\Seen"])

    def send(self, draft: Draft) -> tuple[bytes, SendResult]:
        # SMTP owns its connection and may run concurrently with IMAP.
        message, recipients = build_message(self.account, draft)
        account = self.account
        context = ssl.create_default_context(cafile=account.ca_file)
        host = account.smtp_host or account.host
        if account.smtp_security == "tls":
            smtp = smtplib.SMTP_SSL(
                host, account.smtp_port, timeout=account.timeout, context=context
            )
        else:
            smtp = smtplib.SMTP(host, account.smtp_port, timeout=account.timeout)
        try:
            if account.smtp_security == "starttls":
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(account.smtp_username or account.username, account.password(smtp=True))
            refused = smtp.send_message(message, from_addr=account.address, to_addrs=recipients)
            # After SMTP accepts DATA, cleanup failures must never turn success into a retry.
        finally:
            with suppress(OSError):
                smtp.close()
        return message.as_bytes(), SendResult(refused=refused)
