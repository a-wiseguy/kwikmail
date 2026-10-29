import sys

import pytest

from kwikmail.__main__ import main
from kwikmail.oauth import OAuthError, OAuthTokens


def invoke(tmp_path, monkeypatch, *args):
    path = tmp_path / "config.toml"
    path.write_text(
        '[account]\nprovider="outlook"\naddress="you@example.com"\noauth_client_id="client"\n'
    )
    monkeypatch.setattr(sys, "argv", ["kwikmail", "--config", str(path), *args])
    main()


def test_login_and_logout_do_not_connect_mail(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(OAuthTokens, "login", lambda self, **kw: calls.append(kw))
    invoke(tmp_path, monkeypatch, "--login", "--no-browser", "--login-port", "8765")
    assert calls == [{"port": 8765, "open_browser": False}]
    assert "Run --check" in capsys.readouterr().out
    monkeypatch.setattr(OAuthTokens, "logout", lambda self: calls.append("logout"))
    invoke(tmp_path, monkeypatch, "--logout")
    assert calls[-1] == "logout"


@pytest.mark.parametrize(
    "args", [("--login", "--check"), ("--demo", "--login"), ("--login-port", "65536")]
)
def test_invalid_cli_actions(tmp_path, monkeypatch, args):
    with pytest.raises(SystemExit) as result:
        invoke(tmp_path, monkeypatch, *args)
    assert result.value.code == 2


def test_cli_auth_error(tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise OAuthError("OAuth service unavailable; retry later.")

    monkeypatch.setattr(OAuthTokens, "login", fail)
    with pytest.raises(SystemExit) as result:
        invoke(tmp_path, monkeypatch, "--login")
    assert result.value.code == 1
    assert "retry later" in capsys.readouterr().err
