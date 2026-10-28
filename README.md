# kwikmeel

A Python terminal mail client for a self-hosted IMAP server. Split panes, Vim-style
navigation, fuzzy finders, and a Markdown composer. The default colors take cues
from Tokyo Night. No Nerd Font required.

## Try it

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run kwikmeel --demo
```

The demo has personal and work accounts and exercises the same UI. Sending in demo mode
never contacts a server. Press `?` for the keyboard reference.

To install the command independently of this checkout:

```sh
uv tool install .
kwikmeel --demo
```

## Connect to docker-mailserver

Use the server's externally reachable mail hostname, matching its TLS certificate.
The default configuration uses IMAPS on port 993 and SMTP submission with STARTTLS
on port 587. Both authenticate with your full email address and mailbox password.
SMTP over implicit TLS on port 465 also works with `smtp_security = "tls"`.

```sh
mkdir -p ~/.config/kwikmeel
cp config.example.toml ~/.config/kwikmeel/config.toml
chmod 600 ~/.config/kwikmeel/config.toml
${EDITOR:-vi} ~/.config/kwikmeel/config.toml
```

Set `host`, `username`, `address`, and `name`. Check that `sent_folder` matches a
folder on your server. `kwikmeel --check` tests IMAP and SMTP authentication independently without sending. Set
`sent_folder = ""` if your server already files outgoing mail automatically.
Kwikmeel does not create remote folders.

Supply the password through a password manager command or environment variable.
For example, macOS Keychain can be used with the following config entry after
you create a generic password named `kwikmeel` in Keychain Access:

```toml
password_command = ["security", "find-generic-password", "-s", "kwikmeel", "-w"]
```

For a temporary interactive session, this Bash/Zsh-compatible prompt avoids
placing the password in shell history:

```sh
printf 'Mailbox password: '
read -rs KWIKMEEL_PASSWORD
printf '\n'
export KWIKMEEL_PASSWORD
uv run kwikmeel --check
uv run kwikmeel
unset KWIKMEEL_PASSWORD
```

`password_command` is an array of executable arguments, run without a shell. Its
stdout must contain only the password. `password_env` takes precedence when set.
For different SMTP credentials, set `smtp_username` and `smtp_password_env`.
For a private certificate authority, set `ca_file` to its PEM bundle; certificate
and hostname verification remain enabled.

Use `--config /path/to/account.toml` for alternate settings and `--account work` to select a named account. Configuration follows
`XDG_CONFIG_HOME` if set. The app never stores a password in its draft file.

## Multiple accounts and diagnostics

The existing `[account]` configuration remains valid. To use named accounts,
replace it with account tables and set a default at the top of the file:

```toml
default_account = "personal"

[accounts.personal]
label = "Personal"
host = "mail.example.com"
username = "you@example.com"
address = "you@example.com"
password_env = "PERSONAL_MAIL_PASSWORD"

[accounts.work]
label = "Work"
host = "mail.example.org"
username = "you@example.org"
address = "you@example.org"
password_env = "WORK_MAIL_PASSWORD"
```

Each table accepts the same fields as `[account]`. Account identities must be
unique. The default must name an account. Mixed legacy and named tables fail
validation. `kwikmeel --account work --check` checks the selected account.

Press `a` to see each account's address, inbox total and unread counts, new-message
count, last successful inbox check, connection errors, and stale state. Unknown
values remain unknown until checked. Opening the picker refreshes inbox statistics
for all accounts concurrently. Press `R` to refresh them again. A slow account does not stop navigation or other accounts.
A first check or UIDVALIDITY reset establishes a baseline for new-message counts.

In the account picker, `Ctrl+D` shows protocol endpoints, TLS modes, sender, and
status. `Ctrl+T` tests IMAP and SMTP
authentication independently. `Ctrl+E` opens a recognizable test message addressed
to that account. Review the sender and recipients, then confirm sending. SMTP
acceptance does not prove recipient delivery.

Named draft directories use `account-` followed by the first 16 hexadecimal
characters of SHA-256 of the UTF-8 configuration key.
Legacy accounts retain the directory hash derived from host, IMAP port, and username.
Changing from `[account]` to `[accounts.name]` does not move the legacy draft.
Close the client, locate the existing `draft.json` under the state directory,
and copy it into the new account's draft directory only after checking the sender.
Keep the original until the recovered draft has been checked in the composer.

Conversations use Message-ID, References, and In-Reply-To among loaded headers.
Equal subjects alone never create a conversation. `t` toggles grouping and `e`
expands the selected conversation. Counts use `[loaded/unread]` for loaded members and unread members.
Search exposes the matching member. Loading older messages can merge groups while
preserving the selected message. Inbox server totals remain separate from loaded
messages and search results.

## Keys

| Key | Action |
| --- | --- |
| `j` / `k`, arrows | Next / previous message |
| `g` / `G` | First / last loaded message |
| `Ctrl+D` / `Ctrl+U` | Page down / up |
| `/` | Fuzzy filter loaded senders and subjects |
| `s` | Search the entire selected folder, including body text |
| `Enter` | Open the reader and mark the message read |
| `Esc` | Leave the reader, or clear the current search |
| `f` | Fuzzy folder picker |
| `c` | Compose or resume a saved draft |
| `R` | Reconnect and refresh |
| `L` | Load the next batch of older messages |
| `Left` / `Right`, `Tab` | Move between visible panes |
| `a` | Account picker and diagnostics |
| `t` | Toggle conversations among loaded messages |
| `e` | Expand or collapse a conversation |
| `Space` / `Ctrl+P` | Command picker |
| `?` | Help |
| `q` / `Ctrl+Q` | Quit |

In pickers, type to filter, `Ctrl+J/K` or arrows to select, and `Enter` to accept.
In the reader, arrows or `j/k` scroll. `r` replies, `a` replies to all, and `w`
forwards. `q` or `Esc` returns to the mailbox.

Local fuzzy filtering searches only loaded headers and displays up to 500 ranked
matches. Server search uses IMAP's substring matching, runs on Enter, and includes
the selected folder's entire contents. Press `Esc` to return to the unfiltered folder.

## Compose and send

`c` opens the saved draft, or a blank one. `Tab` moves between To, Cc, Subject and
the Markdown body. Up and Down navigate the single-line fields. The body keeps
native arrow editing. `Alt+Up/Down` moves focus from an editor. `Ctrl+S` saves immediately; `Esc` saves and closes. `Ctrl+P`
toggles the rendered preview. Drafts autosave 600 ms after typing stops.

Replies prefer Reply-To and preserve Message-ID ancestry through edits and recovery.
Reply-all deduplicates recipients and excludes the sending account. Forwarding
quotes the visible headers without creating a reply relationship. If a draft
already exists, choose to resume it, replace it, or cancel.

`Ctrl+Enter` opens a sender and recipient confirmation. `Ctrl+X` does the same in terminals
that cannot distinguish Ctrl+Enter from Enter. Press `y` to send, or `Esc` to edit.
Outgoing mail includes a plain-text Markdown part and rendered HTML alternative.

After SMTP acceptance, the app appends a copy to `sent_folder` and clears the
confirmed draft. If another draft changed during submission, it is preserved.
If saving that copy fails, the app reports it and prevents resending that message.
Partial recipient rejection is reported separately. If a connection drops during
sending, the draft is retained and the app asks you to check delivery before retrying.
No automatic send retries occur.

One draft per account is stored under
`~/.local/state/kwikmeel/<account-id>/draft.json`, or `XDG_STATE_HOME`. Drafts are
plain text with owner-only permissions and atomic writes. They are not synchronized
to the IMAP Drafts folder. Demo accounts use separate drafts. Body and header caches
are kept in memory only.

## Performance and scope

The UI never waits synchronously for IMAP or SMTP. One dedicated thread owns the
IMAP connection for each account; SMTP uses a separate connection. Normal folder loads fetch only
the latest 100 headers using a sequence range, without listing every UID or
downloading bodies. `page_size` can be set from 10 to 500.

Conversation graphs are reused while loaded headers are unchanged. Paging and read-state
changes rebuild the graph; filtering and expansion keep the full loaded counts.

Preview requests are debounced by 120 ms. Canceled queued requests are skipped,
and late responses cannot overwrite the latest selection. The last 24 parsed
bodies are cached by folder, UIDVALIDITY, and UID. Reopening a cached preview needs
no network request. Preview fetches use `BODY.PEEK`, leaving unread state intact.

Bodies are capped at 2 MB of wire data and 80,000 displayed characters. Large
messages show a truncation notice. HTML-only mail is converted to Markdown. Remote
images and active HTML are stripped. Links do not launch automatically. Attachments
are listed when MIME information is available but cannot be opened or added in v1.

The sidebar disappears below 110 columns; below 85 columns the message list fills
the screen and Enter opens the reader. A terminal of at least 80 × 30 is recommended.

The client has no IMAP IDLE/push, automatic polling, offline mail store, delete/move,
attachment transfer, or OAuth. Use `R` for new mail. Paging uses sequence positions; concurrent expunges can change those
positions, so refresh if another client removes messages while paging. Server-wide
search collects matching UIDs and can take longer on a large mailbox.

## Development and verification

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run python scripts/verify.py
uv build
```

Tests drive the actual Textual app with keypresses and use local TLS socket servers
for IMAP and SMTP. They cover MIME parsing, reply metadata, conversation graphs, search, pagination,
unread statistics, account isolation, delayed operations, draft recovery, failed
sends, certificate validation, and partial recipient rejection.
They do not send external email. The verification script exports demo screenshots
and a local header-loading timing under `.agent-work/verification/`.
Use `--output PATH` to select another Git-ignored output directory.
Use [the benchmark guide](docs/benchmarking.md) for repeated timing comparisons. These are local measurements,
not a guarantee about server latency.

See [the architecture notes](docs/architecture.md) for ownership and tradeoffs.
