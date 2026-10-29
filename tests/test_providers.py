from dataclasses import replace

import pytest

from kwikmail.config import load_settings


def read(tmp_path, fields):
    path = tmp_path / "config.toml"
    path.write_text('[account]\naddress="you@example.com"\n' + fields)
    return load_settings(path).accounts[0]


@pytest.mark.parametrize(
    "provider,host,smtp",
    [
        ("gmail", "imap.gmail.com", "smtp.gmail.com"),
        ("fastmail", "imap.fastmail.com", "smtp.fastmail.com"),
        ("icloud", "imap.mail.me.com", "smtp.mail.me.com"),
        ("yahoo", "imap.mail.yahoo.com", "smtp.mail.yahoo.com"),
        ("zoho", "imap.zoho.com", "smtp.zoho.com"),
        ("outlook", "outlook.office365.com", "smtp-mail.outlook.com"),
        ("microsoft365", "outlook.office365.com", "smtp.office365.com"),
    ],
)
def test_provider_defaults(tmp_path, provider, host, smtp):
    fields = f'provider="{provider}"\n'
    if provider in ("outlook", "microsoft365"):
        fields += 'oauth_client_id="example-client"\n'
    account = read(tmp_path, fields).account
    assert account.host == host and account.smtp_host == smtp
    assert account.imap_port == 993 and account.imap_security == "tls"
    assert account.smtp_port == 587 and account.smtp_security == "starttls"
    assert account.username == ("you" if provider == "icloud" else "you@example.com")
    assert account.smtp_username == ("you@example.com" if provider == "icloud" else "")
    assert account.sent_folder == ("" if provider in ("gmail", "yahoo") else "auto")


def test_explicit_overrides_and_legacy_identity(tmp_path):
    old = read(tmp_path, 'host="imap.gmail.com"\nusername="you@example.com"\n')
    preset = read(tmp_path, 'provider="gmail"\n')
    assert preset.draft_key == old.draft_key
    explicit = read(
        tmp_path,
        'provider="gmail"\nhost="custom"\nsmtp_port=465\n'
        'smtp_security="tls"\nsent_folder="Custom Sent"\nusername="other"\n',
    )
    assert explicit.account == replace(
        preset.account,
        host="custom",
        smtp_port=465,
        smtp_security="tls",
        sent_folder="Custom Sent",
        username="other",
    )


@pytest.mark.parametrize(
    "region,suffix",
    [("eu", "eu"), ("au", "com.au"), ("in", "in"), ("us", "com"), ("jp", "jp"), ("ca", "ca")],
)
def test_zoho_regions(tmp_path, region, suffix):
    account = read(tmp_path, f'provider="zoho"\nzoho_region="{region}"\n').account
    assert account.host == f"imap.zoho.{suffix}"
    assert account.smtp_host == f"smtp.zoho.{suffix}"


@pytest.mark.parametrize(
    "fields",
    [
        'provider="unknown"',
        "provider=[]",
        'provider="zoho"\nzoho_region="invalid"',
        'provider="gmail"\nzoho_region="eu"',
        'provider="gmail"\nauth="invalid"',
        'provider="outlook"',
        'provider="gmail"\nauth="oauth2"\noauth_client_id="client"',
        'provider="fastmail"\nauth="oauth2"',
        'provider="outlook"\noauth_client_id="client"\noauth_tenant="../../evil"',
        'provider="outlook"\noauth_client_id="client"\nusername="bad\\u0001name"',
    ],
)
def test_invalid_provider_auth_configuration(tmp_path, fields):
    with pytest.raises(ValueError):
        read(tmp_path, fields)
