# Connect common email providers

Provider presets fill in IMAP/SMTP endpoints, TLS modes, usernames and Sent-copy
behaviour. Explicit settings override presets. Existing configurations without
`provider` keep their defaults and draft locations.

## App-password accounts

Add an account to `~/.config/kwikmail/config.toml`:

```toml
[account]
provider = "gmail"
address = "you@gmail.com"
password_command = ["pass", "mail/kwikmail"]
```

Use an app password, not the account's normal login password. `password_env` also
works; see [configuration](configuration.md). Named `[accounts.personal]` tables
accept the same settings. Keep each account's credentials separate.

| Provider | `provider` | Requirements | Provider verification |
| --- | --- | --- | --- |
| Gmail | `gmail` | 2-step verification and app-password eligibility | Documented |
| Fastmail | `fastmail` | App password; plan with IMAP access | Documented |
| iCloud | `icloud` | App-specific password | Documented |
| Yahoo | `yahoo` | App password | Documented |
| Zoho | `zoho` | IMAP enabled; eligible plan; regional endpoints | Documented |
| Outlook.com / Hotmail | `outlook` | OAuth registration and login | Documented |
| Microsoft 365 | `microsoft365` | OAuth registration; tenant permits IMAP and SMTP AUTH | Documented |
| Proton Mail | No preset; Bridge endpoints | Paid plan, local Bridge and its generated credentials | Documented |

“Documented” means setup instructions exist, not that a real account has passed
verification. Local tests cover password and XOAUTH2 transport, TLS certificate
validation, token refresh and Sent-copy behaviour. Provider consent, delivery and
server-side Sent copies require a live account check below.

Gmail's app-password option can be unavailable under Workspace policies, Advanced
Protection or some 2-step verification configurations. Fastmail Basic plans lack
IMAP access. iCloud defaults to the address's local part for IMAP and the full
address for SMTP; override `username` if needed.

Zoho accepts `zoho_region = "us"`, `"eu"`, `"in"`, `"au"`, `"jp"` or `"ca"`.
For organization accounts or other regions, use the exact `host` and `smtp_host`
shown in Zoho's account settings; these can differ from the regional defaults.

Official setup references:
[Gmail](https://support.google.com/accounts/answer/185833),
[Fastmail](https://www.fastmail.help/hc/en-us/articles/1500000278342-Server-names-and-ports),
[iCloud](https://support.apple.com/102525),
[Yahoo](https://help.yahoo.com/kb/SLN4075.html),
[Zoho](https://www.zoho.com/mail/help/imap-access.html).

## Microsoft OAuth

1. Register an application in Microsoft Entra. For Outlook.com, choose an account
   audience that includes personal Microsoft accounts. For Microsoft 365, choose
   an audience that includes the intended tenant.
2. Enable public client flows for device-code login. Add delegated **Office 365
   Exchange Online** permissions `IMAP.AccessAsUser.All` and `SMTP.Send`. Microsoft
   Graph mail permissions do not authorize IMAP/SMTP.
3. Configure the application ID and account:

   ```toml
   [account]
   provider = "outlook"
   address = "you@outlook.com"
   oauth_client_id = "your-application-id"
   ```

   For work/school mail, use `provider = "microsoft365"`. `oauth_tenant` defaults
   to `"common"`; set a tenant ID or domain to restrict the directory. Both presets
   default to `auth = "oauth2"`.
4. Run `kwikmail --login`. Open the device-login page on any browser and enter the
   displayed code. Login works from SSH without a callback tunnel. MSAL requests
   offline access and refreshes tokens automatically.
5. Run `kwikmail --check` to authenticate both protocols without sending mail.

The signed-in identity must match `username`, which defaults to `address`. If the
login identity differs from the mailbox address, set `oauth_username` to the
primary sign-in identity. This does not grant mailbox or sender permissions.

Tenant consent, conditional access and mailbox protocol settings can block login
or either protocol. Kwikmail reports recognized policy blocks separately and
never changes tenant settings. Ask the administrator to assess protocol access;
OAuth alone does not bypass a disabled SMTP AUTH service.

References: [Microsoft OAuth protocol and scopes](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth),
[SMTP AUTH policy](https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/authenticated-client-smtp-submission).

## Gmail OAuth

1. Create a Google Cloud OAuth application and configure its consent screen.
   Create a **Desktop app** OAuth client. For testing, add the account as a test
   user. Supply your own registration; Kwikmail does not distribute a shared one.
2. Configure Gmail OAuth:

   ```toml
   [account]
   provider = "gmail"
   address = "you@gmail.com"
   auth = "oauth2"
   oauth_client_id = "your-client-id.apps.googleusercontent.com"
   oauth_client_secret_env = "KWIKMAIL_GOOGLE_CLIENT_SECRET"
   ```

3. Supply the desktop client's secret through the named environment variable.
   Keep it out of shell history and tracked configuration. Run `kwikmail --login`.
   The browser returns to a callback bound only to `127.0.0.1`. Login waits up to
   five minutes. The signed-in, verified email must match `username`, or
   `oauth_username` when configured.
4. Run `kwikmail --check`.

For SSH, forward a fixed loopback port from the browser machine:

```sh
ssh -L 8765:127.0.0.1:8765 user@server
kwikmail --login --no-browser --login-port 8765
```

Open the printed URL in the local browser. The callback crosses the tunnel.
Keep login URLs and device codes private. The callback port defaults to a free
port for local use; `--login-port` selects one explicitly.

IMAP/SMTP needs the restricted `https://mail.google.com/` scope. Login also
requests OpenID/email scopes to verify account identity. External applications
in Testing normally receive refresh tokens that expire after seven days.
Personal-use exceptions do not remove consent warnings, account policy or token
expiry rules. Public distribution requires a separate assessment of Google's
verification and minimum-scope policies; a Gmail API adapter may be necessary.

References: [desktop callback support](https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration),
[token expiry](https://developers.google.com/identity/protocols/oauth2#expiration),
[verification](https://support.google.com/cloud/answer/13463817).

## Token storage and logout

`kwikmail --account work --login` authorizes one named account. Normal startup
and `--check` never open a login flow. Expired access tokens refresh before
protocol authentication; revoked authorization requires another `--login`.

Caches live below `$XDG_STATE_HOME/kwikmail/oauth`, defaulting to
`~/.local/state/kwikmail/oauth`. They contain sensitive tokens and, for Google,
the desktop client secret. Files are plaintext with owner-only permissions,
inside an owner-only directory. They are separate from draft files. Cache keys
bind account identity, mailbox endpoints and OAuth registration. Changes to these
settings require a new login; old caches remain until removed.

Refresh and replacement run under a per-cache process lock. Writes replace the
cache atomically. Another operation can time out while login owns the lock;
finish login and retry. Tokens and raw provider errors stay out of diagnostics.

`kwikmail --account work --logout` removes that account's current local cache.
It does not revoke provider consent or disconnect another running client. Close
other instances before logout. Revoke access in the provider's account settings
to invalidate authorization elsewhere. Remove obsolete cache files after closing
clients if registration or account settings have changed.

## Sent copies

- Gmail and Yahoo presets set `sent_folder = ""` because the service files
  outgoing mail. Kwikmail performs no IMAP append.
- Other presets use `sent_folder = "auto"`. Kwikmail appends to the single
  selectable folder advertising the IMAP `\Sent` attribute.
- If discovery finds no folder or several candidates, set an explicit folder
  name. Kwikmail never creates a remote folder or guesses a localized name.
- If server settings already save outgoing mail, use `sent_folder = ""`,
  including Fastmail accounts with SMTP-copy rules enabled.

Explicit `sent_folder` always overrides a preset. Check the result with one
message before regular use. A successful SMTP submission stays accepted if saving
its Sent copy fails. Kwikmail never resubmits it automatically.

## Proton Mail Bridge

Install and start [Proton Mail Bridge](https://proton.me/support/imap-smtp-and-pop3-setup)
on the machine running Kwikmail. Bridge requires an eligible paid plan. Use the
local ports and generated mailbox password displayed by Bridge, not the Proton
account password. Export Bridge's certificate and supply it as `ca_file`:

```toml
[account]
host = "127.0.0.1"
username = "you@proton.me"
address = "you@proton.me"
imap_port = 1143
imap_security = "starttls"
smtp_port = 1025
smtp_security = "starttls"
password_command = ["pass", "mail/proton-bridge"]
ca_file = "/path/to/bridge-certificate.pem"
sent_folder = ""
```

Ports and TLS modes above are examples; copy the actual Bridge settings.
The endpoint must match the certificate's hostname. Certificate and hostname
verification stay enabled. If trust fails, correct the certificate/endpoint
rather than disabling verification. Bridge must remain running; keep its
listeners on loopback. On SSH hosts, Bridge runs on the remote host too.

## Verify a provider account

1. Run `kwikmail --account NAME --check`; both IMAP and SMTP must authenticate.
2. Open folders and a message, then reply to a mailbox you control.
3. Confirm receipt in that mailbox, including sender and reply threading.
4. Confirm exactly one copy appears in the expected Sent folder.
5. For OAuth, restart the client after access-token expiry and confirm refresh.

SMTP acceptance alone does not prove delivery. Mark a provider **live verified**
only after these account checks. Run `uv run pytest` for reproducible local
verification; fixtures contact local TLS servers and mocked OAuth services only.
