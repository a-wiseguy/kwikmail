import hashlib
from dataclasses import replace

import pytest

from kwikmeel.config import load_account, load_settings

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
