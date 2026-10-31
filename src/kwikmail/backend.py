import smtplib
import ssl
from collections import OrderedDict
from contextlib import suppress
from datetime import UTC, datetime

from imapclient import IMAPClient
from imapclient.exceptions import LoginError

from .config import Account
from .mail import build_message, parse_body, parse_header
from .models import ConnectionResult, Draft, MailBody, MailboxStats, MailPage, SendResult
from .oauth import LoginRequired, OAuthError, OAuthPolicyError, OAuthTokens

HEADER_NAMES = "FROM TO CC REPLY-TO SUBJECT DATE MESSAGE-ID REFERENCES IN-REPLY-TO"
HEADER_FIELD = f"BODY[HEADER.FIELDS ({HEADER_NAMES})]".encode()
HEADER_QUERY = f"BODY.PEEK[HEADER.FIELDS ({HEADER_NAMES})]"


class MailboxChanged(ValueError):
    pass


def connection_error(error: Exception, protocol: str) -> str:
    """Do not expose server replies, credentials, subprocess output or terminal controls."""
    if isinstance(error, LoginRequired):
        detail = "OAuth login required; run --account NAME --login"
    elif isinstance(error, OAuthPolicyError):
        detail = "OAuth access blocked; check registration, consent and tenant policy"
    elif isinstance(error, OAuthError):
        detail = "OAuth unavailable; check registration, token storage or retry login"
    elif isinstance(error, ssl.SSLCertVerificationError):
        detail = "certificate verification failed"
    elif isinstance(error, (smtplib.SMTPAuthenticationError, LoginError)):
        detail = "authentication failed"
    elif isinstance(error, TimeoutError):
        detail = "connection timed out"
    elif isinstance(error, ssl.SSLError):
        detail = "TLS connection failed"
    elif isinstance(error, smtplib.SMTPRecipientsRefused):
        detail = "all recipients were refused"
    elif isinstance(error, smtplib.SMTPSenderRefused):
        detail = "sender was refused"
    elif isinstance(error, smtplib.SMTPDataError):
        detail = "message was refused"
    elif isinstance(error, ValueError):
        detail = "check configuration, credentials and message fields"
    elif isinstance(error, OSError):
        detail = "connection failed"
    else:
        detail = "operation failed"
    return f"{protocol}: {detail}."


class ImapBackend:
    def __init__(self, account: Account, *, account_key: str = "default", tokens=None):
        self.account = account
        self.tokens = tokens
        if account.auth == "oauth2" and tokens is None:
            self.tokens = OAuthTokens(account, account_key)
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
                if account.auth == "oauth2":
                    client.oauth2_login(account.username, self.tokens.access_token())
                else:
                    client.login(account.username, account.password())
            except Exception:
                with suppress(Exception):
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

    def stats(self, folder: str, previous: MailboxStats | None = None) -> MailboxStats:
        client = self.connection()
        fields = ["MESSAGES", "UNSEEN", "UIDVALIDITY", "UIDNEXT"]
        values = client.folder_status(folder, fields)
        try:
            total, unread, validity, uidnext = (
                int(values[key]) for key in (b"MESSAGES", b"UNSEEN", b"UIDVALIDITY", b"UIDNEXT")
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Invalid mailbox STATUS response") from error
        if total < 0 or not 0 <= unread <= total or validity < 1 or uidnext < 1:
            raise ValueError("Invalid mailbox STATUS counts")
        new_count = None
        if previous is not None and previous.validity == validity:
            if uidnext < previous.uidnext:
                raise MailboxChanged("Mailbox UIDNEXT decreased. Refresh the connection.")
            new_count = 0
            if uidnext > previous.uidnext:
                info = client.select_folder(folder, readonly=True)
                if int(info[b"UIDVALIDITY"]) != validity:
                    raise MailboxChanged("Mailbox changed during the check. Refresh to retry.")
                uids = client.search(["UID", f"{previous.uidnext}:{uidnext - 1}"])
                current = client.folder_status(folder, ["UIDVALIDITY"])
                if int(current[b"UIDVALIDITY"]) != validity:
                    raise MailboxChanged("Mailbox changed during the check. Refresh to retry.")
                new_count = len({uid for uid in uids if previous.uidnext <= uid < uidnext})
        return MailboxStats(total, unread, validity, uidnext, datetime.now(UTC), new_count)

    def check_imap(self) -> ConnectionResult:
        probe = ImapBackend(self.account, tokens=self.tokens)
        try:
            probe.connection().noop()
            return ConnectionResult(True)
        except Exception as error:
            return ConnectionResult(False, connection_error(error, "IMAP"))
        finally:
            with suppress(Exception):
                probe.close()

    def _smtp_connection(self) -> smtplib.SMTP:
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
            username = account.smtp_username or account.username
            if account.auth == "oauth2":
                smtp.ehlo_or_helo_if_needed()
                token = self.tokens.access_token()
                payload = f"user={username}\x01auth=Bearer {token}\x01\x01"
                smtp.auth("XOAUTH2", lambda challenge=None: payload if challenge is None else "")
            else:
                smtp.login(username, account.password(smtp=True))
            return smtp
        except Exception as error:
            with suppress(Exception):
                smtp.close()
            if (
                account.provider in ("outlook", "microsoft365")
                and isinstance(error, smtplib.SMTPAuthenticationError)
                and b"disabled" in error.smtp_error.lower()
            ):
                raise OAuthPolicyError() from None
            raise

    def check_smtp(self) -> ConnectionResult:
        try:
            smtp = self._smtp_connection()
        except Exception as error:
            return ConnectionResult(False, connection_error(error, "SMTP"))
        with suppress(Exception):
            smtp.close()
        return ConnectionResult(True)

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

    def range_uids(self, folder, since, before):
        client = self.connection()
        info = client.select_folder(folder, readonly=True)
        criteria = ["SINCE", since, "BEFORE", before]
        return int(info[b"UIDVALIDITY"]), sorted(client.search(criteria))

    def raw(self, folder: str, validity: int, uid: int, max_bytes: int = 50_000_000) -> bytes:
        client = self.connection()
        info = client.select_folder(folder, readonly=True)
        if int(info[b"UIDVALIDITY"]) != validity:
            raise MailboxChanged("Mailbox changed; list messages again before downloading.")
        rows = client.fetch([uid], ["RFC822.SIZE"])
        if uid not in rows or b"RFC822.SIZE" not in rows[uid]:
            raise ValueError("Message unavailable or size missing.")
        size = int(rows[uid][b"RFC822.SIZE"])
        if size < 0 or max_bytes < 1:
            raise ValueError("Invalid message size or download limit.")
        if size > max_bytes:
            raise ValueError("Message exceeds the download size limit.")
        rows = client.fetch([uid], [f"BODY.PEEK[]<0.{max_bytes + 1}>"])
        row = rows.get(uid, {})
        raw = row.get(b"BODY[]<0>", row.get(b"BODY[]"))
        if raw is None or len(raw) != size or len(raw) > max_bytes:
            raise ValueError("Incomplete message download; no file was exported.")
        return raw

    def mark_seen(self, folder: str, validity: int, uid: int) -> None:
        client = self.connection()
        info = client.select_folder(folder, readonly=False)
        if int(info[b"UIDVALIDITY"]) != validity:
            raise MailboxChanged("Mailbox changed. Refresh before marking messages read.")
        client.add_flags([uid], [b"\\Seen"])

    def append_sent(self, raw: bytes) -> None:
        folder = self.account.sent_folder
        if not folder:
            return
        client = self.connection()
        if folder == "auto":
            folders = [
                name
                for flags, _, name in client.list_folders()
                if b"\\Sent" in flags and b"\\Noselect" not in flags
            ]
            if len(folders) != 1:
                raise ValueError(
                    "Set sent_folder to an existing folder, or empty for server copies."
                )
            folder = folders[0]
        client.append(folder, raw, flags=[b"\\Seen"])

    def send(self, draft: Draft) -> tuple[bytes, SendResult]:
        message, recipients = build_message(self.account, draft)
        raw = message.as_bytes()
        smtp = self._smtp_connection()
        try:
            refused = smtp.send_message(
                message, from_addr=self.account.address, to_addrs=recipients
            )
        finally:
            with suppress(Exception):
                smtp.close()
        return raw, SendResult(refused=refused)
