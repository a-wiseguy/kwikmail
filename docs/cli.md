# Command line

Run `kwikmail cli --help` for scriptable mail commands. Running `kwikmail` opens
the terminal UI. The `--check`, `--login` and `--logout` flags also remain available.

Global options go before the command: `--config PATH`, `--account KEY`,
`--demo`, and `--json`. Without `--json`, results are indented JSON. With it,
stdout contains one compact JSON value. Help remains text. Errors go to stderr
as JSON, with a nonzero exit code: 2 for invalid input, 1 for operational failure.
Failed connection checks and send outcomes stay on stdout with a nonzero exit code.
OAuth login instructions go to stderr; the final login result goes to stdout.

```sh
kwikmail cli --demo --json accounts
kwikmail cli --config ./config.toml --account work --json folders
kwikmail cli --account work --json list --folder INBOX
kwikmail cli --account work --json status
kwikmail cli --account work --json check
kwikmail cli --account work --json login
```

## Read and download

`list` returns headers, `validity`, and `next_end`. Supply `--end NEXT_END` and
`--validity VALIDITY` to fetch the next page. Stop when `next_end` is zero.
`--query TEXT` searches the folder on the IMAP server.

Message commands require the UID and validity from the same account and folder.
A UID alone is not a stable identity across mailbox resets. These example numbers
must be replaced with values returned by your server.

```sh
kwikmail cli --json read 123 --validity 456 --folder INBOX
kwikmail cli --json attachments 123 --validity 456
kwikmail cli --json attachments 123 --validity 456 --output ./downloads/message-123
kwikmail cli --json seen 123 --validity 456
```

Reading, searching and downloading use read-only selection and `BODY.PEEK`;
only `seen` marks mail read. Downloads fetch complete messages, independently of
the UI preview limit. Named inline MIME parts also count as attachments. Attached
emails are saved as `.eml` files. Filenames are sanitized and numbered; existing
files are never overwritten. New files have owner-only permissions.

In the terminal reader, press `d` to download attachments into a new directory
under `~/Downloads/kwikmail/`. The notification shows its location. Opening the
reader retains its existing mark-as-read behavior.

## Date ranges and invoice exports

Write an overview of every Q3 inbox item to JSON:

```sh
kwikmail cli --json search --quarter 2026-Q3 --match all > inbox-q3.json
```

Add `--all-accounts` to include every configured inbox, or put `--account KEY`
before `search` to select one account. Results include account, folder, UID validity,
UID, subject, sender, date, size and attachment filenames. They omit message bodies.
Q3 covers July 1 through September 30.

To save the original messages and their attachments:

```sh
# Previous completed calendar quarter, across all configured inboxes.
kwikmail cli --json export --last quarter --all-accounts --output ./exports

# A specific calendar quarter from one account.
kwikmail cli --account work --json export --quarter 2026-Q3 --output ./exports

# Preview matches without writing files.
kwikmail cli --json search --last 90d --match invoices

# Explicit dates; the end date is exclusive.
kwikmail cli --json export --since 2026-07-01 --before 2026-10-01 --output ./exports

# Attachments only, or attachments OR custom keywords.
kwikmail cli --json export --last 3months --match attachments --output ./exports
kwikmail cli --json export --last 30d --keyword invoice --keyword factuur --output ./exports
```

Select exactly one range style:

- `--quarter YYYY-Q1` through `YYYY-Q4`: the named calendar quarter.
- `--last quarter`: the previous completed calendar quarter.
- `--last 3months`: three calendar months back through today, clamped to a valid day.
- `--last Nd`: N calendar dates including today.
- `--since YYYY-MM-DD [--before YYYY-MM-DD]`: inclusive start, exclusive end;
  omitted end means tomorrow.

Relative dates use the machine's local date. IMAP `SINCE`/`BEFORE` select by the
server's internal message date, not the sender-controlled `Date` header. The
manifest's per-message `date` is the sender header and can differ or be absent.

`search` defaults to `--match all`. `export` defaults to `--match invoices`, which
matches any attachment OR any keyword in the decoded subject or text MIME
parts. Default case-insensitive substrings are `invoice`, `factuur`, `payment`,
`betaling`, `receipt`, and `rekening`. Repeated `--keyword` options replace those
defaults. The filter does not search inside attachments. Review the matches
before treating them as invoices.

Each export creates a unique `kwikmail-*` directory beneath `--output`, containing:

- `manifest.json`: account, folder, UID validity, UID, sender, subject, size,
  attachment metadata, paths and completion state.
- One numbered directory per match with the original `message.eml` bytes and
  decoded files in `attachments/`.

Rerunning creates another export; it never overwrites a previous run. On failure,
stdout contains partial results with `complete: false`, stderr contains the error,
and the exit code is nonzero. The manifest remains incomplete. Check completion
before using an export in another process. An interrupted process can leave files
not yet listed in its incomplete manifest; restart into a new export directory.

The scan downloads every message in the date range, one at a time, to inspect
its MIME attachments. Scans are not limited to the first
page. The default per-message download limit is 50 MB; scans accept
`--max-bytes NUMBER`. An oversized, vanished or incomplete message fails the scan
rather than silently omitting it. Search results and manifest metadata remain in
memory; use narrower ranges for very large mailboxes. Exports contain private
mail in plaintext; choose the output directory accordingly.

## Drafts and sending

```sh
kwikmail cli --json draft save --to person@example.com --subject Update --body-file ./body.md
kwikmail cli --json draft show
kwikmail cli --json send --confirm
kwikmail cli --json draft clear

# Prepare a response, edit its JSON, import it, then review and send.
kwikmail cli --json respond 123 --validity 456 --kind reply_all > response.json
kwikmail cli --json draft import ./response.json
```

`respond` supports `reply`, `reply_all`, and `forward`. It neither saves nor sends.
Import preserves threading headers. Draft save/import replace the selected
account's local draft; avoid editing that draft concurrently in the TUI. `send`
uses the same SMTP and Sent-copy handling as the UI, requires `--confirm`, and
reports acceptance separately from warnings. After SMTP acceptance, Sent-copy
failure is a warning, not an instruction to resend. Sending attachments and
forwarding original attachment files are not supported.
