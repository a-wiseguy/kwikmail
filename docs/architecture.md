# Architecture

The UI calls `await app.imap("page", folder, end, query, validity)` for a page of
headers, then `await app.imap("body", folder, validity, uid)` for the selected
message. Both run on the same dedicated executor thread. Only that thread touches
the IMAP connection and body cache.

## Ownership

| Module | Responsibility |
| --- | --- |
| `models.py` | Header, body, page, draft, and send-result data |
| `config.py` | TOML validation, account settings, password resolution |
| `mail.py` | MIME decoding, HTML conversion, address validation, outgoing MIME |
| `backend.py` | IMAP session, UID identity, body cache, SMTP submission |
| `storage.py` | Atomic local draft persistence |
| `app.py` | Folder state, list filtering, job scheduling, navigation |
| `widgets.py` | Reader, preview, fuzzy pickers, send confirmation |
| `composer.py` | Draft editing and send state |
| `demo.py` | Deterministic mailbox behavior without external connections |

`MailHeader` and `MailBody` are immutable. A message identity is a folder,
UIDVALIDITY, and UID; sequence numbers are used only to select a bounded page.
UIDVALIDITY is checked before a body fetch or read-state mutation. Folder names
are passed through IMAPClient's modified UTF-7 handling.

The main thread owns widgets. Workers return values to the event loop before
updating them. Selecting a new row cancels the previous preview worker and advances
a serial number. An in-progress socket call cannot be forcibly canceled, but its
result is discarded and pending executor jobs can be canceled. Every socket has a
timeout. Refresh reconnects explicitly. Failed network operations close the IMAP
connection before a future request reconnects.

Drafts have one local owner. Autosave, explicit save, and exit all use the same
atomic replacement operation. Sending disables editing. Once SMTP accepts DATA,
the composer records delivery before saving the Sent copy. A Sent-copy failure
cannot move the composer back into a state that permits another send. A transport
failure during SMTP can still be ambiguous; there is no protocol-level exactly-once
guarantee, so the app retains the draft and asks the user to verify delivery.

## Choices

An async IMAP library would avoid a thread, but requires more protocol lifecycle
code. A single executor with IMAPClient gives one owner for the mutable selected
mailbox and lets the UI stay responsive. Separate per-request IMAP connections
would permit parallel body fetching but add login round trips and server load.
For a client with one active selection, debouncing and caching fit better.

A persistent SQLite mail index would enable offline search, but adds synchronization,
deletion, and cache-invalidation behavior beyond this first release. This version
keeps headers and bodies in memory and makes local versus server search explicit.

Textual provides an efficient table, Markdown rendering, modal screens, and headless
keyboard tests. A custom curses renderer would reduce dependencies but require
implementing those parts from scratch. Network batching and bounded rendering are
the first performance priorities.

## Limits to revisit

Sequence-based paging may skip or repeat a position after concurrent expunges.
The UI deduplicates UIDs; refreshing establishes a new page. Search results collect
all matching UIDs before fetching a bounded header page. Large messages are partial
MIME documents after the wire limit, so attachments or a later text part can be absent.
These limitations are visible in the README and truncation notice.

Local TLS test servers verify client protocol usage, but are not a substitute for
acceptance testing against the user's own Dovecot/Postfix configuration.

Implementation references: [Textual workers](https://textual.textualize.io/guide/workers/),
[Textual DataTable](https://textual.textualize.io/widgets/data_table/), and
[IMAPClient](https://imapclient.readthedocs.io/en/master/).
