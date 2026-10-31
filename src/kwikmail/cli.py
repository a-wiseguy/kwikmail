"""Scriptable mail operations. Global options precede the command."""

import asyncio
import json
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

import typer

from .attachments import extract_attachments, save_attachments
from .backend import ImapBackend, connection_error
from .config import Account, AccountEntry, Settings, config_path, load_settings, state_path
from .demo import DemoBackend
from .exports import (
    INVOICE_WORDS,
    date_range,
    export_directory,
    export_message,
    save_manifest,
    scan,
)
from .mail import make_response, parse_body
from .models import Draft
from .session import AccountSession
from .storage import DraftStore

app = typer.Typer(no_args_is_help=True, help="Mail commands for scripts and terminal use.")
drafts = typer.Typer(help="Manage the selected account's local draft.")
app.add_typer(drafts, name="draft")


class Match(StrEnum):
    all = "all"
    attachments = "attachments"
    invoices = "invoices"


def encode(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, (datetime, date, Path)):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def emit(ctx, value):
    # Text mode is indented JSON too: untrusted email never becomes terminal controls.
    typer.echo(
        json.dumps(value, default=encode, ensure_ascii=True, indent=None if ctx.obj["json"] else 2)
    )


@app.callback()
def setup(
    ctx: typer.Context,
    config: Path = typer.Option(None),
    account: str = typer.Option(None),
    demo: bool = False,
    json_output: bool = typer.Option(False, "--json"),
):
    """Use --json for compact machine-readable output; no command starts the TUI."""
    ctx.obj = {"config": config, "selected": account, "demo": demo, "json": json_output}


def settings_for(ctx):
    if "settings" in ctx.obj:
        return ctx.obj["settings"]
    if ctx.obj["demo"]:
        settings = Settings(
            (
                AccountEntry(
                    "demo",
                    "Demo",
                    Account("demo.local", "you@example.com", "you@example.com", "You"),
                    "cli-demo",
                ),
            ),
            "demo",
        )
    else:
        settings = load_settings(ctx.obj["config"] or config_path())
    selected = ctx.obj["selected"] or settings.default_key
    if selected not in {entry.key for entry in settings.accounts}:
        raise ValueError("Unknown account identity.")
    ctx.obj.update(settings=settings, selected=selected)
    return settings


def entry(ctx):
    return next(item for item in settings_for(ctx).accounts if item.key == ctx.obj["selected"])


def draft_store(ctx):
    return DraftStore(state_path() / entry(ctx).draft_key / "draft.json")


@contextmanager
def backend_for(ctx, selected=None):
    selected = selected or entry(ctx)
    backend = (
        DemoBackend(account=selected.account)
        if ctx.obj["demo"]
        else ImapBackend(selected.account, account_key=selected.draft_key)
    )
    try:
        yield backend
    finally:
        backend.close()


@app.command()
def accounts(ctx: typer.Context):
    """List account identities without credentials."""
    emit(
        ctx,
        [
            {
                "key": e.key,
                "label": e.label,
                "address": e.account.address,
                "selected": e.key == ctx.obj["selected"],
            }
            for e in settings_for(ctx).accounts
        ],
    )


@app.command()
def login(
    ctx: typer.Context, port: int = typer.Option(0, min=0, max=65535), no_browser: bool = False
):
    """Authorize OAuth; provider instructions go to stderr, final result to stdout."""
    import sys

    from .oauth import OAuthTokens

    selected = entry(ctx)
    if ctx.obj["demo"] or selected.account.auth != "oauth2":
        raise ValueError("Login requires an OAuth account.")
    with redirect_stdout(sys.stderr):
        OAuthTokens(selected.account, selected.draft_key).login(
            port=port, open_browser=not no_browser
        )
    emit(ctx, {"authorized": True, "account": selected.key})


@app.command()
def logout(ctx: typer.Context):
    """Remove local OAuth tokens; provider consent remains unchanged."""
    from .oauth import OAuthTokens

    selected = entry(ctx)
    if ctx.obj["demo"] or selected.account.auth != "oauth2":
        raise ValueError("Logout requires an OAuth account.")
    OAuthTokens(selected.account, selected.draft_key).logout()
    emit(ctx, {"logged_out": True, "account": selected.key})


@app.command()
def folders(ctx: typer.Context):
    """List selectable folders."""
    with backend_for(ctx) as backend:
        emit(ctx, backend.folders())


@app.command()
def status(ctx: typer.Context, folder: str = ""):
    """Get mailbox counts."""
    with backend_for(ctx) as backend:
        emit(ctx, backend.stats(folder or backend.account.inbox))


@app.command()
def check(ctx: typer.Context):
    """Check IMAP and SMTP authentication without sending mail."""
    with backend_for(ctx) as backend:
        imap, smtp = backend.check_imap(), backend.check_smtp()
        emit(ctx, {"imap": imap, "smtp": smtp})
        if not imap.ok or not smtp.ok:
            raise typer.Exit(1)


@app.command("list")
def list_messages(
    ctx: typer.Context,
    folder: str = "",
    query: str = "",
    end: int = typer.Option(None, min=0),
    validity: int = typer.Option(None, min=1),
):
    """List a page; pass next_end and validity to retrieve older messages."""
    with backend_for(ctx) as backend:
        emit(ctx, backend.page(folder or backend.account.inbox, end, query, validity))


@app.command()
def read(
    ctx: typer.Context,
    uid: int = typer.Argument(min=1),
    validity: int = typer.Option(..., min=1),
    folder: str = "",
):
    """Read a complete message without marking it read."""
    with backend_for(ctx) as backend:
        emit(ctx, parse_body(uid, backend.raw(folder or backend.account.inbox, validity, uid)))


@app.command()
def seen(
    ctx: typer.Context,
    uid: int = typer.Argument(min=1),
    validity: int = typer.Option(..., min=1),
    folder: str = "",
):
    """Mark one message read."""
    with backend_for(ctx) as backend:
        backend.mark_seen(folder or backend.account.inbox, validity, uid)
        emit(ctx, {"uid": uid, "seen": True})


@app.command()
def attachments(
    ctx: typer.Context,
    uid: int = typer.Argument(min=1),
    validity: int = typer.Option(..., min=1),
    folder: str = "",
    output: Path = typer.Option(None),
):
    """List attachments, or download all files with --output DIRECTORY."""
    with backend_for(ctx) as backend:
        raw = backend.raw(folder or backend.account.inbox, validity, uid)
        if output:
            emit(ctx, save_attachments(raw, output))
        else:
            emit(
                ctx,
                [
                    {"name": a.name, "content_type": a.content_type, "size": len(a.data)}
                    for a in extract_attachments(raw)
                ],
            )


def run_scan(
    ctx, since, before, quarter, last, folder, match, keyword, all_accounts, max_bytes, output=None
):
    start, stop = date_range(since=since, before=before, quarter=quarter, last=last)
    keywords = keyword or list(INVOICE_WORDS)
    if any(not word.strip() for word in keywords):
        raise ValueError("Keywords must not be empty.")
    directory = export_directory(output) if output else None
    report = {
        "since": str(start),
        "before": str(stop),
        "date_basis": "imap_internal_date",
        "match": match.value,
        "keywords": keywords,
        "complete": False,
        "messages": [],
    }
    if directory:
        report["directory"] = str(directory)
        save_manifest(directory, report)
    try:
        selected = settings_for(ctx).accounts if all_accounts else [entry(ctx)]
        for account in selected:
            with backend_for(ctx, account) as backend:
                mailbox = folder or account.account.inbox
                for validity, header, raw in scan(
                    backend, mailbox, start, stop, match.value, keywords, max_bytes
                ):
                    item = {
                        "account": account.key,
                        "folder": mailbox,
                        "validity": validity,
                        "uid": header.uid,
                        "subject": header.subject,
                        "sender": header.sender,
                        "date": str(header.date) if header.date else None,
                        "size": len(raw),
                        "attachments": [a.name for a in extract_attachments(raw)],
                    }
                    if directory:
                        item.update(export_message(directory, len(report["messages"]) + 1, raw))
                    report["messages"].append(item)
        report["complete"] = True
    finally:
        if directory:
            save_manifest(directory, report)
        if not report["complete"]:
            report["error"] = "Scan failed; results are incomplete."
            emit(ctx, report)
    emit(ctx, report)


@app.command()
def search(
    ctx: typer.Context,
    since: str = None,
    before: str = None,
    quarter: str = None,
    last: str = None,
    folder: str = "",
    match: Match = Match.all,
    keyword: list[str] = typer.Option(None),
    all_accounts: bool = False,
    max_bytes: int = typer.Option(50_000_000, min=1),
):
    """Fetch a date range; filter by attachments or invoice keywords."""
    run_scan(ctx, since, before, quarter, last, folder, match, keyword, all_accounts, max_bytes)


@app.command("export")
def export_mail(
    ctx: typer.Context,
    output: Path = typer.Option(...),
    since: str = None,
    before: str = None,
    quarter: str = None,
    last: str = None,
    folder: str = "",
    match: Match = Match.invoices,
    keyword: list[str] = typer.Option(None),
    all_accounts: bool = False,
    max_bytes: int = typer.Option(50_000_000, min=1),
):
    """Export EML, attachments and a manifest. Default: attachments OR invoice keywords."""
    run_scan(
        ctx, since, before, quarter, last, folder, match, keyword, all_accounts, max_bytes, output
    )


@drafts.command("show")
def draft_show(ctx: typer.Context):
    emit(ctx, draft_store(ctx).load())


@drafts.command("save")
def draft_save(
    ctx: typer.Context,
    to: str = "",
    cc: str = "",
    subject: str = "",
    body_file: Path = typer.Option(..., exists=True, dir_okay=False),
):
    """Replace the local draft using a UTF-8 body file."""
    draft = Draft(to=to, cc=cc, subject=subject, body=body_file.read_text())
    draft_store(ctx).save(draft)
    emit(ctx, {"saved": True})


@drafts.command("import")
def draft_import(
    ctx: typer.Context, input_file: Path = typer.Argument(exists=True, dir_okay=False)
):
    """Import a JSON draft, including threading fields from respond."""
    draft_store(ctx).save(DraftStore(input_file).load())
    emit(ctx, {"saved": True})


@drafts.command("clear")
def draft_clear(ctx: typer.Context):
    draft_store(ctx).clear()
    emit(ctx, {"cleared": True})


@app.command()
def respond(
    ctx: typer.Context,
    uid: int = typer.Argument(min=1),
    validity: int = typer.Option(..., min=1),
    folder: str = "",
    kind: str = "reply",
):
    """Return a reply, reply_all or forward draft as JSON; does not save or send."""
    if kind not in {"reply", "reply_all", "forward"}:
        raise ValueError("Kind must be reply, reply_all or forward.")
    with backend_for(ctx) as backend:
        body = parse_body(uid, backend.raw(folder or backend.account.inbox, validity, uid))
        emit(
            ctx,
            make_response(
                body, kind=kind, own_addresses=frozenset({backend.account.address.casefold()})
            ),
        )


@app.command()
def send(ctx: typer.Context, confirm: bool = False):
    """Send the saved draft. Requires --confirm; reports final SMTP acceptance."""
    if not confirm:
        raise ValueError("Review with draft show, then use send --confirm.")

    async def deliver():
        with backend_for(ctx) as backend:
            selected = entry(ctx)
            session = AccountSession(selected.key, selected.label, backend, draft_store(ctx))
            try:
                return await session.send(draft_store(ctx).load())
            finally:
                await session.close()

    outcome = asyncio.run(deliver())
    emit(ctx, outcome)
    if not outcome.accepted:
        raise typer.Exit(1)


def main(args=None):
    import sys

    args = sys.argv[2:] if args is None else args
    try:
        code = app(args=args, prog_name="kwikmail cli", standalone_mode=False)
        if code:
            raise SystemExit(code)
    except typer.TyperException as error:
        _error(args, error.format_message(), error.exit_code)
    except (ValueError, FileNotFoundError) as error:
        _error(args, str(error), 2)
    except Exception as error:
        _error(args, connection_error(error, "CLI"), 1)


def _error(args, message, code):
    if "--json" in args:
        typer.echo(json.dumps({"error": message, "exit_code": code}, ensure_ascii=True), err=True)
    else:
        typer.echo(json.dumps({"error": message}, ensure_ascii=True), err=True)
    raise SystemExit(code)
