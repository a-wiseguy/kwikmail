# Next-version behavior and verification

This specification defines the next release. It describes planned behavior, not a claim that the current implementation supports it.

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
