import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from kwikmail.config import Account
from kwikmail.oauth import LoginRequired, OAuthError, OAuthPolicyError, OAuthTokens


def account(provider="gmail"):
    return Account(
        "imap.example.com",
        "you@example.com",
        "you@example.com",
        provider=provider,
        auth="oauth2",
        oauth_client_id="client",
        oauth_client_secret_env="GOOGLE_SECRET",
    )


def seed(tokens, *, expired=True):
    credentials = Credentials(
        "old-token",
        refresh_token="refresh-token",
        token_uri="unused",
        client_id="client",
        client_secret="secret",
        expiry=datetime.now() + timedelta(hours=-1 if expired else 1),
    )
    with tokens._locked():
        tokens._write(credentials.to_json())


class Response:
    status = 200
    data = b'{"access_token":"fresh-token","expires_in":3600,"refresh_token":"rotated"}'
    headers = {}


def test_google_cached_and_expired_tokens(tmp_path, monkeypatch):
    tokens = OAuthTokens(account(), "home", tmp_path / "oauth")
    calls = []

    def request(**kwargs):
        calls.append(kwargs)
        return Response()

    monkeypatch.setattr("google.auth.transport.requests.Request", lambda: request)
    seed(tokens, expired=False)
    assert tokens.access_token() == "old-token" and not calls
    seed(tokens)
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(lambda _: tokens.access_token(), range(6))) == ["fresh-token"] * 6
    assert len(calls) == 1
    assert calls[0]["timeout"] == 15
    assert b"refresh_token=refresh-token" in calls[0]["body"]
    assert json.loads(tokens.path.read_text())["refresh_token"] == "rotated"
    assert tokens.path.stat().st_mode & 0o777 == 0o600
    assert tokens.path.parent.stat().st_mode & 0o777 == 0o700
    assert tokens.path.with_suffix(".json.lock").stat().st_mode & 0o777 == 0o600
    tokens.logout()
    with pytest.raises(LoginRequired):
        tokens.access_token()


def _process_refresh(root, counter):
    import google.auth.transport.requests

    def request(**kwargs):
        with counter.get_lock():
            counter.value += 1
        return Response()

    google.auth.transport.requests.Request = lambda: request
    assert OAuthTokens(account(), "home", root).access_token() == "fresh-token"


def test_refresh_lock_across_processes(tmp_path):
    root = tmp_path / "oauth"
    seed(OAuthTokens(account(), "home", root))
    context = multiprocessing.get_context("spawn")
    count = context.Value("i", 0)
    processes = [context.Process(target=_process_refresh, args=(root, count)) for _ in range(3)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        if process.is_alive():
            process.terminate()
            pytest.fail("refresh process did not finish")
        assert process.exitcode == 0
    assert count.value == 1


def test_cache_binding_and_logout_isolation(tmp_path):
    original = account()
    tokens = OAuthTokens(original, "home", tmp_path)
    variants = [OAuthTokens(original, "work", tmp_path)]
    for key, value in [
        ("oauth_client_id", "other"),
        ("oauth_tenant", "other"),
        ("username", "other"),
        ("oauth_username", "other"),
        ("host", "other"),
    ]:
        variants.append(OAuthTokens(replace(original, **{key: value}), "home", tmp_path))
    seed(tokens)
    for variant in variants:
        assert tokens.path != variant.path
        variant.logout()
    assert tokens.path.exists()


@pytest.mark.parametrize("retryable,error_type", [(False, LoginRequired), (True, OAuthError)])
def test_refresh_failure_is_safe(tmp_path, monkeypatch, retryable, error_type):
    tokens = OAuthTokens(account(), "home", tmp_path)
    seed(tokens)

    def fail(*args):
        raise RefreshError("private-provider-response", retryable=retryable)

    monkeypatch.setattr(Credentials, "refresh", fail)
    with pytest.raises(error_type) as error:
        tokens.access_token()
    assert "private-provider-response" not in str(error.value)


def test_google_login_identity_and_options(tmp_path, monkeypatch):
    tokens = OAuthTokens(account(), "home", tmp_path)
    credentials = Credentials(
        "new-token",
        refresh_token="refresh",
        token_uri="unused",
        client_id="client",
        client_secret="secret",
        id_token="id",
    )
    options = {}

    def run(**kwargs):
        options.update(kwargs)
        return credentials

    monkeypatch.setenv("GOOGLE_SECRET", "secret")
    monkeypatch.setattr(
        "google_auth_oauthlib.flow.InstalledAppFlow.from_client_config",
        lambda *a, **kw: SimpleNamespace(run_local_server=run),
    )
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda *a, **kw: {"email_verified": True, "email": "you@example.com"},
    )
    tokens.login(port=8765, open_browser=False)
    assert options["host"] == "127.0.0.1" and options["port"] == 8765
    assert options["open_browser"] is False and options["timeout_seconds"] == 300
    assert options["access_type"] == "offline" and options["prompt"] == "consent"
    before = tokens.path.read_text()
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda *a, **kw: {"email_verified": True, "email": "wrong@example.com"},
    )
    with pytest.raises(OAuthError, match="identity"):
        tokens.login()
    assert tokens.path.read_text() == before


def test_microsoft_login_refresh_and_policy(tmp_path, monkeypatch, capsys):
    import msal

    tokens = OAuthTokens(account("outlook"), "home", tmp_path)
    results = {"result": {"access_token": "token"}, "username": "you@example.com"}
    seen = []

    class App:
        def __init__(self, client_id, *, authority, token_cache, timeout):
            self.cache = token_cache
            seen.append((client_id, authority, timeout))

        def initiate_device_flow(self, *, scopes):
            assert scopes and not any(scope == "offline_access" for scope in scopes)
            return {"user_code": "test-code"}

        def acquire_token_by_device_flow(self, flow):
            self.cache.has_state_changed = True
            return results["result"]

        def get_accounts(self, *, username):
            return [{"username": username}] if username == results["username"] else []

        def acquire_token_silent_with_error(self, scopes, *, account):
            return results["result"]

    monkeypatch.setattr(msal, "PublicClientApplication", App)
    with pytest.raises(LoginRequired):
        tokens.access_token()
    assert not seen
    tokens.login()
    assert "test-code" in capsys.readouterr().out
    assert seen == [("client", "https://login.microsoftonline.com/common", 15)]
    assert tokens.access_token() == "token"
    results["result"] = {"error": "invalid_grant", "error_description": "secret"}
    with pytest.raises(LoginRequired):
        tokens.access_token()
    results["result"] = {"error": "invalid_grant", "error_codes": [53003]}
    with pytest.raises(OAuthPolicyError):
        tokens.access_token()
    results["result"] = {"access_token": "other"}
    results["username"] = "wrong@example.com"
    with pytest.raises(OAuthError, match="identity"):
        tokens.login()


def test_corrupt_cache_sanitized(tmp_path):
    tokens = OAuthTokens(account(), "home", tmp_path)
    with tokens._locked():
        tokens._write("private-corrupt-content")
    with pytest.raises(OAuthError) as error:
        tokens.access_token()
    assert "private-corrupt-content" not in str(error.value)
