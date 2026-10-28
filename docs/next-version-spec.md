# Next-version behavior and verification

This specification defines the next release's behavior, ownership contracts and verification gates.

## Reply and forward

The reader offers reply, reply-all, and forward actions. Reply prefers Reply-To over From. Reply-all deduplicates recipients and excludes the sending account. A reply preserves Message-ID ancestry through In-Reply-To and References. Forward quotes the source with its visible headers without inventing a reply relationship.

The composer displays the sending account. Reply metadata survives edits, autosave, closing, and recovery. Starting a reply when a draft already exists requires a choice that preserves that draft unless the user explicitly replaces it. Confirmation shows the sender and recipients. SMTP acceptance, recipient refusal, and saving the Sent copy remain separate outcomes. A failed Sent append never enables resending an accepted message.

## Navigation and compact information

Up and down navigate messages, folders, picker options, and the reader. Left and right move between visible mailbox panes. Form fields support vertical arrow navigation. Text editing retains horizontal cursor movement and multiline body navigation. Modified arrows provide an unambiguous focus shortcut from editors.

Mailbox information distinguishes loaded messages, filtered results, server total, and server unread count. Dates include time in the local timezone when space permits. Narrow terminals retain a readable subject and access to the full date through the reader. Account and connection state stay visible without adding a permanent large panel. Sending displays progress and an accurate outcome.

## Conversations

Conversation grouping uses Message-ID, References, and In-Reply-To among loaded headers. Equal subjects alone do not establish a conversation. Missing ancestors, duplicate message IDs, malformed references, and cycles must be handled deterministically without losing messages.

The mailbox can toggle grouping and expand a conversation to select individual messages. Counts indicate loaded members and unread members. Search, pagination, preview, and reading retain the identity of the selected message. The UI explicitly labels grouping as covering loaded messages. Loading more can merge previously separate groups.

## Multiple accounts

Named accounts have stable configuration identities and one configured default. Existing single-account configuration remains valid. The CLI can select a named account. Invalid defaults, duplicate identities, and malformed settings fail with actionable errors.

The account picker shows account name, address, inbox total and unread count, newly observed messages since the previous successful check, last successful inbox check, and connection errors. Unknown values display as unknown. A first check or UIDVALIDITY reset establishes a baseline rather than calling every message new. Failed checks preserve the last successful timestamp and mark statistics stale.

Account switching resets mailbox-specific search, paging, and selection. Each account owns its credentials, draft location, cache, and protocol state. Old queued requests cannot execute against a newly selected account. Old responses cannot replace the new account's rows, preview, reader, or status. Composer operations retain the account that opened them through SMTP and Sent append.

## Diagnostics

Connection tests check IMAP and SMTP authentication independently without sending email. They report which protocol failed and keep certificate verification enabled. Slow or failed accounts must not freeze navigation or prevent another account from loading.

Test email prepares a recognizable message to the selected account's own address by default. The user reviews the sender and recipient and explicitly confirms sending. SMTP acceptance does not establish recipient delivery. Demo accounts exercise this workflow without contacting a server.

## Verification gates

Tests cover MIME reply metadata, recipient rules, draft recovery, account config compatibility, overlapping UIDs across accounts, delayed and failed operations, exact unread statistics, conversation graphs, pagination and search, arrow focus, and send outcome handling. Local TLS fixtures exercise both protocols. UI runs cover wide and narrow terminals, error states, account switching, reply and forward, thread expansion, and diagnostics.

Performance verification compares common paths against the initial release using fixed datasets and locked dependencies. Measure startup to rendered rows, paging, fuzzy search including rendering, slow-network input responsiveness, body fetch and cached revisit, parsing and Markdown rendering. Measure account switching and conversation grouping as new capabilities. Record at least five samples, medians and ranges, correct-output checks, failures, work counts, and a separate profile of bottlenecks. Interleave baseline and candidate measurements. Keep raw results and screenshots in ignored output directories.

After integration, review actual rendered screens and keyboard behavior. Fix observed usability problems. Profile measured bottlenecks, implement justified improvements, and rerun the same measurements. Report synthetic and local TLS evidence separately from live-server verification.

## Account ownership and implementation contracts

Use one `AccountSession` per configured account. Each session permanently binds its backend and draft store. IMAP operations serialize within that session; separate accounts can progress independently. Admission happens before executor submission, and cancellation must not release transport ownership while a blocking call is still running. SMTP uses a separate connection and cannot overlap another send for the same account.

The app owns the active account and mailbox view. Every background UI request captures its session and view revision before awaiting. Results and errors update widgets only if that context still matches. A reader captures its `MessageRef`; a composer captures its session and draft metadata. Neither resolves transport or storage through mutable active-account state.

The following names define the integration boundary. Implementations may add private helpers.

```python
@dataclass(frozen=True)
class AccountEntry:
    key: str
    label: str
    account: Account
    draft_key: str

@dataclass(frozen=True)
class Settings:
    accounts: tuple[AccountEntry, ...]
    default_key: str

def load_settings(path: Path) -> Settings: ...

@dataclass(frozen=True)
class MessageRef:
    account: str
    folder: str
    validity: int
    uid: int

@dataclass(frozen=True)
class MailboxStats:
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

class AccountSession:
    def __init__(self, key: str, label: str, backend, drafts: DraftStore): ...
    async def folders(self) -> list[str]: ...
    async def mailbox(self, folder, end=None, query="", validity=None) -> MailboxSnapshot: ...
    async def body(self, ref: MessageRef) -> MailBody: ...
    async def mark_seen(self, ref: MessageRef) -> None: ...
    async def refresh_inbox(self) -> MailboxStats: ...
    async def check_connections(self) -> ConnectionReport: ...
    async def send(self, draft: Draft) -> SendOutcome: ...
    async def close(self) -> None: ...
```

Session attributes expose `key`, `label`, immutable `account`, bound `drafts`, and a status snapshot. Status includes connection state, last inbox statistics, last successful sync, last error, and send state. `ConnectionReport` contains independent IMAP and SMTP results. `SendOutcome` reports accepted submission, refused recipients, and finalization warnings. SMTP errors retain the draft. Accepted submission remains accepted when Sent append or draft cleanup fails.

Capture account, view epoch, operation slot and serial for UI work. Apply the same acceptance check to success, errors, reader mounting, and loading cleanup. Account switches and selection changes invalidate the relevant tickets, including switches back to a previously selected account. Confirmed draft snapshots must match the draft being cleared after submission. Preserve a newer draft and report the cleanup difference.

Configuration accepts legacy `[account]` or named `[accounts.personal]` tables with `default_account = "personal"`. Named tables accept the existing account fields and an optional `label`. Reject mixed shapes and unknown defaults. Legacy state retains its existing host/port/username hash. Named accounts use stable ID-derived draft paths, with documented manual recovery when converting configuration. Do not move draft files automatically.

New-message counts use a successful prior UIDNEXT watermark under unchanged UIDVALIDITY and count existing UIDs within the newly allocated range. Do not treat UIDNEXT differences as message counts. First check and reset return unknown. Failed checks preserve the prior successful watermark. The server's unread count comes from STATUS UNSEEN. Search result count remains separate from folder total.

Reply metadata extends the existing header, body and draft records with defaulted fields. `make_response(original, *, kind, own_addresses)` returns a draft for `reply`, `reply_all`, or `forward`. The composer uses `dataclasses.replace` or equivalent metadata-preserving reconstruction when editing fields. Pure conversation grouping returns every loaded UID exactly once and never fetches messages.

Conversation grouping reuses the computed graph while loaded immutable header snapshots are unchanged. Paging, account/folder changes and read-state updates invalidate that snapshot; filtering and expansion retain complete loaded-member counts.
