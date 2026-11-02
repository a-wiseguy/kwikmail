import hashlib
from dataclasses import replace

import pytest

from kwikmail.config import load_account, load_settings

FIELDS = 'host="mail.example.com"\nusername="you"\naddress="you@example.com"\n'


def config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_legacy_account_keeps_draft_identity(tmp_path):
    path = config(tmp_path, "[account]\n" + FIELDS)
    settings = load_settings(path)
    (entry,) = settings.accounts
    assert settings.default_key == entry.key == "default"
    assert entry.account == load_account(path)
    assert entry.draft_key == hashlib.sha256(b"mail.example.com:993:you").hexdigest()[:16]


def test_named_default_and_independent_stable_draft_identities(tmp_path):
    path = config(
        tmp_path,
        'default_account="work"\n[accounts.personal]\n'
        + FIELDS
        + '[accounts.work]\nlabel="Office"\n'
        + FIELDS,
    )
    settings = load_settings(path)
    personal, work = settings.accounts
    assert settings.default_key == "work" and work.label == "Office"
    assert work.account == load_account(path)
    assert personal.draft_key != work.draft_key
    assert work.draft_key.startswith("account-")
    path.write_text(
        path.read_text()
        .replace('label="Office"', 'label="Other"')
        .replace('username="you"', 'username="other"')
    )
    changed = load_settings(path)
    assert [entry.draft_key for entry in changed.accounts] == [
        entry.draft_key for entry in settings.accounts
    ]
    assert changed.accounts[1].account == replace(work.account, username="other")


@pytest.mark.parametrize(
    "text,match",
    [
        ("", "Configure"),
        ("accounts=[]", "Configure"),
        ('default_account="missing"\n[accounts.home]\n' + FIELDS, "default_account"),
        ("[accounts.home]\n" + FIELDS, "default_account"),
        ("default_account=true\n[accounts.home]\n" + FIELDS, "default_account"),
        ("[account]\n" + FIELDS + "[accounts.home]\n" + FIELDS, "either"),
        ('default_account="home"\n[account]\n' + FIELDS, "either"),
        ('default_account="home"\n[accounts]\nhome=1', "table"),
        ('default_account="../home"\n[accounts."../home"]\n' + FIELDS, "Account names"),
        ('default_account="home"\n[accounts.home]\nlabel=[]\n' + FIELDS, "label"),
        ('default_account="home"\n[accounts.home]\nlabel=" "\n' + FIELDS, "label"),
        ("[account]\n" + FIELDS + "imap_port=true", "imap_port"),
        ("[account]\n" + FIELDS + 'password_command="sh"', "array"),
        ("[account]\n" + FIELDS + "extra=1", "Invalid account"),
        ("unknown=true\n[account]\n" + FIELDS, "Unknown configuration"),
        ('[account]\nhost=""\nusername="you"\naddress="you@example.com"', "host"),
    ],
)
def test_invalid_configuration(tmp_path, text, match):
    with pytest.raises(ValueError, match=match):
        load_settings(config(tmp_path, text))


def test_duplicate_identity_rejected(tmp_path):
    path = config(
        tmp_path,
        'default_account="home"\n[accounts.home]\n' + FIELDS + "[accounts.home]\n" + FIELDS,
    )
    with pytest.raises(ValueError):
        load_settings(path)


def test_account_specific_plaintext_password_private_and_hidden(tmp_path, monkeypatch):
    path = config(
        tmp_path,
        'default_account="work"\n[accounts.personal]\n'
        + FIELDS
        + 'password="personal-secret"\n[accounts.work]\n'
        + FIELDS
        + 'password="work-secret"\nsmtp_password_env="SMTP_ONLY"\n',
    )
    path.chmod(0o600)
    monkeypatch.setenv("KWIKMAIL_PASSWORD", "irrelevant-environment-value")
    monkeypatch.setenv("SMTP_ONLY", "smtp-secret")
    settings = load_settings(path)
    personal, work = settings.accounts
    assert personal.account.password() == "personal-secret"
    assert work.account.password() == "work-secret"
    assert work.account.password(smtp=True) == "smtp-secret"
    assert "personal-secret" not in repr(settings)
    assert "work-secret" not in repr(settings)
    assert callable(work.account.password)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600") as error:
        load_settings(path)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("source", ['password_env="PASS"', 'password_command=["password"]'])
def test_plaintext_password_rejects_explicit_sources(tmp_path, source):
    path = config(tmp_path, "[account]\n" + FIELDS + 'password="private-secret"\n' + source)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="cannot be combined") as error:
        load_settings(path)
    assert "private-secret" not in str(error.value)


def test_empty_plaintext_placeholder_and_type_validation(tmp_path, monkeypatch):
    monkeypatch.delenv("KWIKMAIL_PASSWORD", raising=False)
    path = config(tmp_path, "[account]\n" + FIELDS + 'password=""')
    path.chmod(0o600)
    with pytest.raises(ValueError, match="before connecting"):
        load_account(path).password()
    path.write_text("[account]\n" + FIELDS + "password=123")
    with pytest.raises(ValueError, match="must be a string"):
        load_settings(path)


def test_legacy_environment_then_command_fallback(tmp_path, monkeypatch):
    import sys

    path = config(
        tmp_path,
        "[account]\n"
        + FIELDS
        + 'password_env="LEGACY_PASS"\npassword_command=['
        + repr(sys.executable)
        + ', "-c", "print(123)"]',
    )
    monkeypatch.setenv("LEGACY_PASS", "environment")
    assert load_account(path).password() == "environment"
    monkeypatch.delenv("LEGACY_PASS")
    assert load_account(path).password() == "123"
