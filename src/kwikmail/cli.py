"""Scriptable mail operations. Global options precede the command."""

import asyncio
import json
from contextlib import contextmanager, nullcontext, redirect_stdout
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

import typer
from filelock import FileLock, Timeout

from .attachments import extract_attachments, save_attachments
from .backend import ImapBackend, MailboxChanged, connection_error
from .config import Account, AccountEntry, Settings, config_path, load_settings, state_path
from .demo import DemoBackend
from .exports import (
    INVOICE_WORDS,
    date_range,
    export_directory,
    export_message,
    matches,
    save_manifest,
    validate_export_files,
)
from .mail import make_response, parse_body, parse_header
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
    ctx,
    since,
    before,
    quarter,
    last,
    folder,
    match,
    keyword,
    all_accounts,
    max_bytes,
    output=None,
    resume=None,
):
    start, stop = date_range(since=since, before=before, quarter=quarter, last=last)
    keywords = keyword or list(INVOICE_WORDS)
    if any(not word.strip() for word in keywords):
        raise ValueError("Keywords must not be empty.")
    selected = settings_for(ctx).accounts if all_accounts else [entry(ctx)]
    selection = [
        {
            "account": account.key,
            "host": account.account.host,
            "port": account.account.imap_port,
            "security": account.account.imap_security,
            "username": account.account.username,
            "address": account.account.address,
            "folder": folder or account.account.inbox,
        }
        for account in selected
    ]
    request = {
        "since": str(start),
        "before": str(stop),
        "match": match.value,
        "keywords": keywords,
        "max_bytes": max_bytes,
        "selection": selection,
    }
    directory = (
        resume.resolve() if resume else export_directory(output).resolve() if output else None
    )
    lock = FileLock(directory / ".export.lock", timeout=0) if directory else nullcontext()
    try:
        with lock:
            _scan_report(ctx, selected, start, stop, request, directory, resume)
    except Timeout:
        raise ValueError("Export directory is already in use.") from None


def _scan_report(ctx, selected, start, stop, request, directory, resume):
    if resume:
        report = json.loads((directory / "manifest.json").read_text())
        if report.get("version") != 2 or any(report.get(k) != v for k, v in request.items()):
            raise ValueError("Resume requires the same accounts, folders, range and scan options.")
        if Path(report.get("directory", "")).resolve() != directory:
            raise ValueError("Resume directory does not match the manifest.")
        validate_export_files(directory, report)
    else:
        report = {
            **request,
            "version": 2,
            "date_basis": "imap_internal_date",
            "complete": False,
            "messages": [],
            "mailboxes": [],
        }
        if directory:
            report["directory"] = str(directory.resolve())
    report["complete"] = False
    report.pop("error", None)

    def checkpoint():
        states = [state for box in report["mailboxes"] for state in box["progress"].values()]
        report.update(
            examined=len(states), matched=len(report["messages"]), failed=states.count("failed")
        )
        if directory:
            save_manifest(directory, report)

    checkpoint()
    try:
        for account, identity in zip(selected, request["selection"], strict=True):
            mailbox = identity["folder"]
            with backend_for(ctx, account) as backend:
                validity, uids = backend.range_uids(mailbox, start, stop)
                saved = next(
                    (box for box in report["mailboxes"] if box["identity"] == identity), None
                )
                if saved is None:
                    saved = {
                        "identity": identity,
                        "validity": validity,
                        "uids": uids,
                        "progress": {},
                    }
                    report["mailboxes"].append(saved)
                    checkpoint()
                elif saved["validity"] != validity:
                    raise MailboxChanged("Mailbox UIDVALIDITY changed; start a new export.")
                for uid in saved["uids"]:
                    if saved["progress"].get(str(uid)) in ("matched", "unmatched"):
                        continue
                    try:
                        raw = backend.raw(mailbox, validity, uid, request["max_bytes"])
                        internal_date = backend.received_at(mailbox, validity, uid)
                        if matches(raw, request["match"], request["keywords"]):
                            header = parse_header(uid, raw, (), len(raw))
                            item = {
                                "account": account.key,
                                "folder": mailbox,
                                "validity": validity,
                                "uid": uid,
                                "subject": header.subject,
                                "sender": header.sender,
                                "date": str(header.date) if header.date else None,
                                "internal_date": internal_date.isoformat(),
                                "received_at": internal_date.astimezone(UTC).isoformat(),
                                "size": len(raw),
                                "attachments": [a.name for a in extract_attachments(raw)],
                            }
                            if directory:
                                item.update(
                                    export_message(directory, len(report["messages"]) + 1, raw)
                                )
                            report["messages"].append(item)
                            saved["progress"][str(uid)] = "matched"
                        else:
                            saved["progress"][str(uid)] = "unmatched"
                    except Exception:
                        saved["progress"][str(uid)] = "failed"
                        checkpoint()
                        raise
                    checkpoint()
        report["complete"] = True
    finally:
        if not report["complete"]:
            report["error"] = "Scan failed; results are incomplete."
        checkpoint()
        if not report["complete"]:
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
    output: Path = typer.Option(None),
    resume: Path = typer.Option(None, exists=True, file_okay=False),
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
    if bool(output) == bool(resume):
        raise ValueError("Supply exactly one of --output or --resume.")
    run_scan(
        ctx,
        since,
        before,
        quarter,
        last,
        folder,
        match,
        keyword,
        all_accounts,
        max_bytes,
        output,
        resume,
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
