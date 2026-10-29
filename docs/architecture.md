# Architecture

The app receives one `AccountSession` per configured account. Each session binds
its backend, credentials, draft store, protocol state, and body cache for its
entire lifetime. The app selects a session and owns the visible mailbox state.

## Ownership

| Module | Responsibility |
| --- | --- |
| `models.py` | Header, body, message identity, statistics, draft, and send outcomes |
| `config.py` | TOML validation, account settings, password resolution |
| `providers.py` | Provider defaults with explicit configuration overrides |
| `oauth.py` | Login flows, account-bound token caches and serialized refresh |
| `mail.py` | MIME decoding, address validation, outgoing MIME |
| `html.py` | HTML reading order, hidden content removal, image descriptions, safe links |
| `backend.py` | IMAP session, UID identity, body cache, SMTP submission |
| `session.py` | Per-account admission, protocol serialization, statistics, send finalization |
| `threads.py` | Pure grouping and flattening of loaded conversation headers |
| `storage.py` | Atomic local draft persistence |
| `app.py` | Folder state, list filtering, job scheduling, navigation |
| `widgets.py` | Reader, preview, fuzzy pickers, send confirmation |
| `composer.py` | Draft editing and send state |
| `demo.py` | Deterministic mailbox behavior without external connections |

`MailHeader` and `MailBody` are immutable. A `MessageRef` contains account identity, folder,
UIDVALIDITY, and UID; sequence numbers are used only to select a bounded page.
UIDVALIDITY is checked before a body fetch or read-state mutation. Folder names
are passed through IMAPClient's modified UTF-7 handling.

The main thread owns widgets. Every UI request captures a `ViewTicket` with the
session, mailbox epoch, operation slot, and serial before scheduling asynchronous
work. Success, errors, reader mounting, and loading cleanup require the same ticket
to remain current. Switching accounts advances the epoch even when switching back
to an earlier account. Row changes invalidate preview and reader operations.

The session admits IMAP work before executor submission. A canceled caller cannot
release ownership while its blocking operation still runs. Requests waiting for
admission can be canceled without reaching the backend. Each account progresses
independently. Refresh reconnects explicitly. Socket timeouts bound network waits.

`mailbox()` returns a page and server statistics separately. STATUS UNSEEN supplies
the unread count. Successful UIDVALIDITY and UIDNEXT watermarks define the new UID
range for the next check. The backend counts existing UIDs in that range rather
than interpreting the UIDNEXT difference as a count. Failed checks retain the last
successful timestamp and mark statistics stale.

Readers capture a `MessageRef` and session. Composers capture a session and immutable
initial draft metadata. Editing uses `dataclasses.replace` to preserve ancestry.
Account changes cannot redirect a reader or composer to another account's backend.

Drafts have one local owner. Autosave, explicit save, and exit all use the same
atomic replacement operation. Sending disables editing. Once SMTP accepts DATA,
the session records final acceptance separately from saving the Sent copy. A Sent-copy failure
cannot move the composer back into a state that permits another send. A transport
failure during SMTP can still be ambiguous; there is no protocol-level exactly-once
guarantee, so the app retains the draft and asks the user to verify delivery.

## Choices

An async IMAP library would avoid a thread, but requires more protocol lifecycle
code. One executor per account with IMAPClient gives each mutable selected mailbox one
owner and lets the UI stay responsive. Separate per-request IMAP connections
would permit parallel body fetching but add login round trips and server load.
For a client with one active selection, debouncing and caching fit better.

A persistent SQLite mail index would enable offline search, but adds synchronization,
deletion, and cache-invalidation behavior beyond the current scope. This version
keeps headers and bodies in memory and makes local versus server search explicit.

Textual provides an efficient table, Markdown rendering, modal screens, and headless
keyboard tests. A custom curses renderer would reduce dependencies but require
implementing those parts from scratch. Network batching and bounded rendering are
the first performance priorities.

## Loaded conversations

`group_threads()` builds connected components from valid Message-ID ancestry.
It handles missing ancestors, duplicate IDs, and cycles without network requests.
Every loaded UID belongs to one component. `flatten_threads()` emits collapsed or
expanded rows and applies matching UIDs to the members, so a search can expose a
matching older message. Subject equality alone is ignored.

Conversation keys may change when a newly loaded header connects earlier groups.
The app preserves the selected UID and expands its containing group after a merge.
Counts describe loaded members; server totals describe the entire mailbox.

## Limits to revisit

Sequence-based paging may skip or repeat a position after concurrent expunges.
The UI deduplicates UIDs; refreshing establishes a new page. Search results collect
all matching UIDs before fetching a bounded header page. Large messages are partial
MIME documents after the wire limit, so attachments or a later text part can be absent.
These limitations are described in [usage](usage.md) and the truncation notice.

Local TLS test servers verify client protocol usage, but are not a substitute for
acceptance testing against the user's own Dovecot/Postfix configuration.

Implementation references: [Textual workers](https://textual.textualize.io/guide/workers/),
[Textual DataTable](https://textual.textualize.io/widgets/data_table/), and
[IMAPClient](https://imapclient.readthedocs.io/en/master/).

## Provider authentication

Account configuration resolves provider defaults before validation. Explicit
settings override presets; legacy configuration and draft identities stay stable.
The backend uses password login or XOAUTH2 after TLS verification. Connection
probes share the account's token manager, while IMAP and SMTP report independently.

The CLI performs interactive login before creating sessions. MSAL handles
Microsoft device authorization and refresh; Google libraries handle desktop
loopback authorization, identity verification and refresh. Normal mailbox work
never starts interactive authorization. Tokens live outside draft storage, bound
to the stable account identity and OAuth registration. Per-cache process locks
serialize refresh and logout; owner-only temporary files replace caches atomically.

Sent saving remains a separate finalization step. Provider presets can disable
client append for server-saved copies, or discover a single selectable `\Sent`
folder. Discovery failure reports an accepted-send warning without resubmission.
See [provider setup](providers.md) for configuration and live verification.
