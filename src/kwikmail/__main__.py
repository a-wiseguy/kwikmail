import argparse
import asyncio
import sys
from pathlib import Path

from . import __version__
from .config import Account, AccountEntry, Settings, config_path, load_settings, state_path


def main() -> None:
    if sys.argv[1:2] == ["cli"]:
        from .cli import main as cli_main

        cli_main()
        return
    parser = argparse.ArgumentParser(
        description="Kwikmail · keyboard-first terminal mail",
        epilog="Scriptable commands: kwikmail cli --help",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--demo", action="store_true", help="Explore two sample accounts, no server needed"
    )
    parser.add_argument("--config", type=Path, default=config_path(), help="Account TOML file")
    parser.add_argument("--account", help="Select a configured account identity")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="Test IMAP and SMTP authentication")
    actions.add_argument(
        "--login", action="store_true", help="Authorize the selected OAuth account"
    )
    actions.add_argument(
        "--logout", action="store_true", help="Remove the selected local OAuth cache"
    )
    parser.add_argument(
        "--login-port", type=int, default=0, help="Google callback port, 0 selects one"
    )
    parser.add_argument("--no-browser", action="store_true", help="Print Google login URL only")
    args = parser.parse_args()
    if not 0 <= args.login_port <= 65535:
        parser.error("--login-port must be between 0 and 65535")
    if args.demo and (args.login or args.logout):
        parser.error("OAuth login/logout are unavailable in demo mode")
    from .backend import ImapBackend
    from .demo import DemoBackend
    from .session import AccountSession
    from .storage import DraftStore

    try:
        if args.demo:
            settings = Settings(
                (
                    AccountEntry(
                        "personal",
                        "Personal demo",
                        Account("demo.local", "you@example.com", "you@example.com", "You"),
                        "demo-personal",
                    ),
                    AccountEntry(
                        "work",
                        "Work demo",
                        Account("demo.local", "work@example.com", "work@example.com", "Work"),
                        "demo-work",
                    ),
                ),
                "personal",
            )
        else:
            settings = load_settings(args.config)
        selected = args.account or settings.default_key
        if selected not in {entry.key for entry in settings.accounts}:
            raise ValueError(f"Unknown account {selected!r}. Use a configured account identity.")
        if args.login or args.logout:
            from .oauth import OAuthError, OAuthTokens

            entry = next(item for item in settings.accounts if item.key == selected)
            if entry.account.auth != "oauth2":
                raise ValueError("--login/--logout require auth = 'oauth2'")
            tokens = OAuthTokens(entry.account, entry.draft_key)
            try:
                if args.logout:
                    tokens.logout()
                    print("Local OAuth tokens removed. Provider consent is unchanged.")
                else:
                    tokens.login(port=args.login_port, open_browser=not args.no_browser)
                    print("OAuth login saved. Run --check to test IMAP and SMTP.")
            except OAuthError as error:
                parser.exit(1, f"{error}\n")
            except OSError:
                parser.exit(1, "OAuth token storage unavailable.\n")
            except KeyboardInterrupt:
                parser.exit(1, "Login cancelled.\n")
            return
        sessions = [
            AccountSession(
                entry.key,
                entry.label,
                DemoBackend(account=entry.account)
                if args.demo
                else ImapBackend(entry.account, account_key=entry.draft_key),
                DraftStore(state_path() / entry.draft_key / "draft.json"),
            )
            for entry in settings.accounts
        ]
    except FileNotFoundError:
        parser.exit(
            2,
            f"No config at {args.config}\nCopy config.example.toml there and edit it, "
            "or try: kwikmail --demo\n",
        )
    except (ValueError, OSError) as error:
        parser.exit(2, f"Configuration error: {error}\n")
    if args.check:

        async def check():
            session = next(item for item in sessions if item.key == selected)
            try:
                report = await session.check_connections()
                for protocol, result in (("IMAP", report.imap), ("SMTP", report.smtp)):
                    print(
                        f"{protocol}: {'authenticated' if result.ok else 'failed ' + result.error}"
                    )
                return report.imap.ok and report.smtp.ok
            finally:
                await asyncio.gather(*(item.close() for item in sessions))

        if not asyncio.run(check()):
            parser.exit(1)
        return
    if not sys.stdin.isatty():
        parser.exit(2, "Run kwikmail in an interactive terminal, or use --check.\n")
    from .app import MailApp

    MailApp(sessions, selected).run()


if __name__ == "__main__":
    main()
