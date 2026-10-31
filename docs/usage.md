# Use kwikmail

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
forwards. `d` downloads attachments. `q` or `Esc` returns to the mailbox.

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
`~/.local/state/kwikmail/<account-id>/draft.json`, or `XDG_STATE_HOME`. Drafts are
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
messages show a truncation notice. When a plain-text alternative exists, it is preferred.
HTML-only mail is converted to Markdown in both the preview and reader. Tables become
stacked text in source order so newsletter layouts fit the terminal. Headings, lists,
and safe links are preserved. Images display their alternative text without downloading
resources; tracking pixels are omitted. Active content, document styles, and elements
hidden by inline styles or the `hidden` attribute are removed. CSS layouts and colors
are not reproduced. Links do not launch automatically. Attachments are listed when MIME
information is available. Press `d` in the reader to download the complete
attachments. Outgoing attachments are not supported.

The sidebar disappears below 110 columns; below 85 columns the message list fills
the screen and Enter opens the reader. A terminal of at least 80 × 30 is recommended.

The client has no IMAP IDLE/push, automatic polling, offline mail store, delete/move,
or outgoing attachments. For Gmail/Microsoft OAuth and provider presets, see
[provider setup](providers.md). Use `R` for new mail. Paging uses sequence positions;
concurrent expunges can change those positions, so refresh if another client removes messages while paging. Server-wide
search collects matching UIDs and can take longer on a large mailbox.

## Attachment downloads and scripts

Press `d` in the reader to save attachments beneath `~/Downloads/kwikmail/`.
Each download uses a new directory shown in the notification. For scripting,
JSON output and quarterly invoice exports, see the [CLI guide](cli.md).
