from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kwikmeel/config.toml"


def state_path() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "kwikmeel"


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
    password_env: str = "KWIKMEEL_PASSWORD"
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


def load_account(path: Path) -> Account:
    with path.open("rb") as file:
        data = tomllib.load(file).get("account", {})
    if not isinstance(data, dict):
        raise ValueError("account must be a TOML table")
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
            raise ValueError(f"[account].{key} must be a string")
    for key in ("host", "username", "address"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"[account].{key} is required in {path}")
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
