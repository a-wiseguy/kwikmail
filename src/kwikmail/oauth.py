"""Interactive authorization and locked, account-bound token persistence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from filelock import FileLock
from filelock import Timeout as LockTimeout

from .config import Account, state_path

MICROSOFT_SCOPES = [
    "https://outlook.office.com/IMAP.AccessAsUser.All",
    "https://outlook.office.com/SMTP.Send",
]
GOOGLE_SCOPES = [
    "https://mail.google.com/",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


class OAuthError(ValueError):
    pass


class LoginRequired(OAuthError):
    def __init__(self):
        super().__init__("OAuth login required; run kwikmail --account NAME --login.")


class OAuthPolicyError(OAuthError):
    def __init__(self):
        super().__init__("OAuth access blocked; check app registration, consent and tenant policy.")


def _microsoft_result(result: dict | None) -> str:
    if result and result.get("access_token"):
        return result["access_token"]
    if result and (set(result.get("error_codes", [])) & {53003, 65001, 700016}):
        raise OAuthPolicyError()
    if result and result.get("error") in {
        "access_denied",
        "unauthorized_client",
        "invalid_client",
        "invalid_scope",
        "consent_required",
        "admin_policy_enforced",
    }:
        raise OAuthPolicyError()
    raise LoginRequired()


class OAuthTokens:
    def __init__(self, account: Account, identity: str, root: Path | None = None):
        self.account = account
        binding = [
            identity,
            account.provider,
            account.oauth_client_id,
            account.oauth_tenant,
            account.oauth_username,
            account.username,
            account.smtp_username,
            account.address,
            account.host,
            account.smtp_host,
        ]
        digest = hashlib.sha256(json.dumps(binding).encode()).hexdigest()
        self.path = (root if root is not None else state_path() / "oauth") / f"{digest}.json"

    @contextmanager
    def _locked(self):
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent.is_symlink() or self.path.is_symlink():
            raise OAuthError("OAuth cache must not be a symbolic link.")
        parent.chmod(0o700)
        try:
            with FileLock(str(self.path) + ".lock", timeout=self.account.timeout, mode=0o600):
                yield
        except LockTimeout:
            raise OAuthError("OAuth cache busy; finish login or retry later.") from None

    def _read(self) -> str:
        try:
            self.path.chmod(0o600)
            return self.path.read_text()
        except FileNotFoundError:
            return ""

    def _write(self, text: str) -> None:
        fd, name = tempfile.mkstemp(prefix=".tokens-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def logout(self) -> None:
        with self._locked():
            self.path.unlink(missing_ok=True)

    def login(self, *, port: int = 0, open_browser: bool = True) -> None:
        self._run(interactive=True, port=port, open_browser=open_browser)

    def access_token(self) -> str:
        return self._run(interactive=False)

    def _run(self, *, interactive: bool, port: int = 0, open_browser: bool = True) -> str:
        try:
            with self._locked():
                if self.account.provider == "gmail":
                    return self._google(interactive, port, open_browser)
                return self._microsoft(interactive)
        except OAuthError:
            raise
        except (OSError, TimeoutError):
            raise OAuthError("OAuth connection or token storage failed; retry later.") from None
        except Exception:
            raise OAuthError("OAuth failed; check registration or run --login again.") from None

    def _microsoft(self, interactive: bool) -> str:
        import msal

        cache = msal.SerializableTokenCache()
        saved = self._read()
        if not saved and not interactive:
            raise LoginRequired()
        if saved and not interactive:
            cache.deserialize(saved)
        app = msal.PublicClientApplication(
            self.account.oauth_client_id,
            authority=f"https://login.microsoftonline.com/{self.account.oauth_tenant}",
            token_cache=cache,
            timeout=self.account.timeout,
        )
        username = self.account.oauth_username or self.account.username
        if interactive:
            flow = app.initiate_device_flow(scopes=MICROSOFT_SCOPES)
            if "user_code" not in flow:
                _microsoft_result(flow)
            print(
                f"Open https://microsoft.com/devicelogin and enter {flow['user_code']}", flush=True
            )
            result = app.acquire_token_by_device_flow(flow)
            token = _microsoft_result(result)
            if not app.get_accounts(username=username):
                raise OAuthError("Signed-in identity differs from oauth_username or username.")
        else:
            accounts = app.get_accounts(username=username)
            if len(accounts) != 1:
                raise LoginRequired()
            result = app.acquire_token_silent_with_error(MICROSOFT_SCOPES, account=accounts[0])
            token = _microsoft_result(result)
        if cache.has_state_changed:
            self._write(cache.serialize())
        return token

    def _google(self, interactive: bool, port: int, open_browser: bool) -> str:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google.oauth2.id_token import verify_oauth2_token
        from google_auth_oauthlib.flow import InstalledAppFlow

        request = partial(Request(), timeout=self.account.timeout)
        if interactive:
            secret = os.environ.get(self.account.oauth_client_secret_env, "")
            if not secret:
                raise OAuthError("Set oauth_client_secret_env before Google login.")
            flow = InstalledAppFlow.from_client_config(
                {
                    "installed": {
                        "client_id": self.account.oauth_client_id,
                        "client_secret": secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                },
                scopes=GOOGLE_SCOPES,
                autogenerate_code_verifier=True,
            )
            credentials = flow.run_local_server(
                host="127.0.0.1",
                port=port,
                open_browser=open_browser,
                timeout_seconds=300,
                authorization_prompt_message="Open this URL in your browser: {url}",
                access_type="offline",
                prompt="consent",
                login_hint=self.account.oauth_username or self.account.username,
            )
            claims = verify_oauth2_token(
                credentials.id_token, request, audience=self.account.oauth_client_id
            )
            expected = self.account.oauth_username or self.account.username
            if (
                not claims.get("email_verified")
                or claims.get("email", "").casefold() != expected.casefold()
            ):
                raise OAuthError("Signed-in identity differs from oauth_username or username.")
            if not credentials.refresh_token:
                raise LoginRequired()
        else:
            saved = self._read()
            if not saved:
                raise LoginRequired()
            credentials = Credentials.from_authorized_user_info(json.loads(saved), GOOGLE_SCOPES)
            if not credentials.valid:
                try:
                    credentials.refresh(request)
                except RefreshError as error:
                    if error.retryable:
                        raise OAuthError("OAuth service unavailable; retry later.") from None
                    raise LoginRequired() from None
        self._write(credentials.to_json())
        if not credentials.token:
            raise LoginRequired()
        return credentials.token
