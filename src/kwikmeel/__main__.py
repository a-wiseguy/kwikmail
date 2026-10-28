import argparse
import hashlib
import sys
from pathlib import Path

from . import __version__
from .config import config_path, load_account, state_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Kwikmeel · keyboard-first terminal mail")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--demo", action="store_true", help="Explore a sample mailbox, no server needed"
    )
    parser.add_argument("--config", type=Path, default=config_path(), help="Account TOML file")
    parser.add_argument(
        "--check", action="store_true", help="Validate configuration and IMAP login"
    )
    args = parser.parse_args()
    from .backend import ImapBackend
    from .demo import DemoBackend

    try:
        backend = DemoBackend() if args.demo else ImapBackend(load_account(args.config))
    except FileNotFoundError:
        parser.exit(
            2,
            f"No config at {args.config}\nCopy config.example.toml there and edit it, "
            "or try: kwikmeel --demo\n",
        )
    except (ValueError, OSError) as error:
        parser.exit(2, f"Configuration error: {error}\n")
    if args.check:
        try:
            names = backend.folders()
            print(f"Connected. {len(names)} selectable folders.")
            print("\n".join(names))
        except Exception as error:
            parser.exit(1, f"Connection failed: {error}\n")
        finally:
            backend.close()
        return
    if not sys.stdin.isatty():
        parser.exit(2, "Run kwikmeel in an interactive terminal, or use --check.\n")
    from .app import MailApp
    from .storage import DraftStore

    identity = f"{backend.account.host}:{backend.account.imap_port}:{backend.account.username}"
    account_key = "demo" if args.demo else hashlib.sha256(identity.encode()).hexdigest()[:16]
    drafts = DraftStore(state_path() / account_key / "draft.json")
    MailApp(backend, drafts).run()


if __name__ == "__main__":
    main()
