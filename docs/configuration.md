# Connect your mailbox

Connect an IMAP/SMTP mailbox using a password, app password or OAuth.
See [common providers](providers.md) for presets, Gmail/Microsoft login and Proton Bridge.

Use the server's externally reachable mail hostname, matching its TLS certificate.
The default configuration uses IMAPS on port 993 and SMTP submission with STARTTLS
on port 587. Both authenticate with your full email address and mailbox password.
SMTP over implicit TLS on port 465 also works with `smtp_security = "tls"`.

```sh
mkdir -p ~/.config/kwikmail
cat > ~/.config/kwikmail/config.toml <<'TOML'
[account]
host = "mail.example.com"
username = "you@example.com"
address = "you@example.com"
name = "Your name"
password_env = "KWIKMAIL_PASSWORD"
sent_folder = "Sent"
TOML
chmod 600 ~/.config/kwikmail/config.toml
${EDITOR:-vi} ~/.config/kwikmail/config.toml
```

Set `host`, `username`, `address`, and `name`. See the [full example](../config.example.toml) for optional settings. Check that `sent_folder` matches a
folder on your server. `kwikmail --check` tests IMAP and SMTP authentication independently without sending. Set
`sent_folder = ""` if your server already files outgoing mail automatically.
Use `sent_folder = "auto"` to discover a folder advertising `\Sent`; discovery
requires exactly one selectable candidate. Kwikmail does not create remote folders.

Supply the password through a password manager command or environment variable.
For example, macOS Keychain can be used with the following config entry after
you create a generic password named `kwikmail` in Keychain Access:

```toml
password_command = ["security", "find-generic-password", "-s", "kwikmail", "-w"]
```

For a temporary interactive session, this Bash/Zsh-compatible prompt avoids
placing the password in shell history:

```sh
printf 'Mailbox password: '
read -rs KWIKMAIL_PASSWORD
printf '\n'
export KWIKMAIL_PASSWORD
kwikmail --check
kwikmail
unset KWIKMAIL_PASSWORD
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
validation. `kwikmail --account work --check` checks the selected account.

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
