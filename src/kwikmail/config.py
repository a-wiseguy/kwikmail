from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kwikmail/config.toml"


def state_path() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "kwikmail"


@dataclass(frozen=True)
class Account:
    host: str
    username: str
    address: str
    name: str = ""
    imap_port: int = 993
    imap_security: str = "tls"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_username: str = ""
    password_env: str = "KWIKMAIL_PASSWORD"
    smtp_password_env: str = ""
    password_command: tuple[str, ...] = ()
    sent_folder: str = "Sent"
    inbox: str = "INBOX"
    page_size: int = 100
    timeout: int = 15
    ca_file: str | None = None
    max_body_bytes: int = 2_000_000

    def password(self, smtp: bool = False) -> str:
        env = self.smtp_password_env if smtp and self.smtp_password_env else self.password_env
        value = os.environ.get(env)
        if value:
            return value
        if self.password_command and not (smtp and self.smtp_password_env):
            result = subprocess.run(
                self.password_command, capture_output=True, text=True, timeout=10, check=True
            )
            if result.stdout.strip():
                return result.stdout.rstrip("\r\n")
        raise ValueError(f"Set {env} or configure password_command before connecting.")


@dataclass(frozen=True)
class AccountEntry:
    key: str
    label: str
    account: Account
    draft_key: str


@dataclass(frozen=True)
class Settings:
    accounts: tuple[AccountEntry, ...]
    default_key: str


def load_settings(path: Path) -> Settings:
    with path.open("rb") as file:
        data = tomllib.load(file)
    unknown = data.keys() - {"account", "accounts", "default_account"}
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    if "account" in data:
        if "accounts" in data or "default_account" in data:
            raise ValueError("Use either [account] or [accounts.name] with default_account")
        account = _parse_account(data["account"], "account", path)
        identity = f"{account.host}:{account.imap_port}:{account.username}"
        draft_key = hashlib.sha256(identity.encode()).hexdigest()[:16]
        entry = AccountEntry("default", account.name or account.address, account, draft_key)
        return Settings((entry,), entry.key)
    accounts = data.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError("Configure [account] or at least one [accounts.name] table")
    default = data.get("default_account")
    if not isinstance(default, str) or default not in accounts:
        raise ValueError("default_account must name a configured account")
    entries = []
    for key, values in accounts.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", key):
            raise ValueError("Account names must use letters, digits, underscores or hyphens")
        if not isinstance(values, dict):
            raise ValueError(f"accounts.{key} must be a TOML table")
        values = dict(values)
        label = values.pop("label", key)
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"[accounts.{key}].label must be a nonempty string")
        account = _parse_account(values, f"accounts.{key}", path)
        draft_key = "account-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        entries.append(AccountEntry(key, label, account, draft_key))
    return Settings(tuple(entries), default)


def load_account(path: Path) -> Account:
    settings = load_settings(path)
    return next(entry.account for entry in settings.accounts if entry.key == settings.default_key)


def _parse_account(data: object, table: str, path: Path) -> Account:
    if not isinstance(data, dict):
        raise ValueError(f"{table} must be a TOML table")
    data = dict(data)
    for key in (
        "host",
        "username",
        "address",
        "name",
        "imap_security",
        "smtp_host",
        "smtp_security",
        "smtp_username",
        "password_env",
        "smtp_password_env",
        "sent_folder",
        "inbox",
        "ca_file",
    ):
        if key in data and not isinstance(data[key], str):
            raise ValueError(f"[{table}].{key} must be a string")
    for key in ("host", "username", "address"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"[{table}].{key} is required in {path}")
    if "password_command" in data:
        command = data["password_command"]
        if not isinstance(command, list) or not all(isinstance(arg, str) for arg in command):
            raise ValueError("password_command must be an array of command arguments")
        data["password_command"] = tuple(command)
    try:
        account = Account(**data)
    except TypeError as error:
        raise ValueError(f"Invalid account configuration: {error}") from error
    for key in ("imap_security", "smtp_security"):
        if getattr(account, key) not in ("tls", "starttls"):
            raise ValueError(f"{key} must be 'tls' or 'starttls'")
    for key, low, high in (
        ("imap_port", 1, 65535),
        ("smtp_port", 1, 65535),
        ("page_size", 10, 500),
        ("timeout", 1, 120),
        ("max_body_bytes", 10000, 10_000_000),
    ):
        value = getattr(account, key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}")
    return account
