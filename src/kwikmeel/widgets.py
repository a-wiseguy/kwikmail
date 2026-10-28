from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

from rapidfuzz import fuzz, process
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from .mail import one_line
from .models import MailBody, MessageRef

if TYPE_CHECKING:
    from .session import AccountSession


class MailTable(DataTable):
    BINDINGS = [
        Binding("left", "app.pane(-1)", "Previous pane", show=False, priority=True),
        Binding("right", "app.pane(1)", "Next pane", show=False, priority=True),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "scroll_top", "First", show=False),
        Binding("G", "scroll_bottom", "Last", show=False),
        Binding("ctrl+d", "page_down", "Page down", show=False),
        Binding("ctrl+u", "page_up", "Page up", show=False),
    ]


class MailScroll(VerticalScroll):
    BINDINGS = [
        Binding("left", "app.pane(-1)", "Previous pane", show=False, priority=True),
        Binding("right", "app.pane(1)", "Next pane", show=False, priority=True),
    ]


class MailView(Vertical):
    def compose(self) -> ComposeResult:
        yield Static("Select a message to preview", classes="message-meta", markup=False)
        with MailScroll(classes="message-scroll"):
            yield Markdown("", open_links=False)

    async def show_body(self, body: MailBody) -> None:
        meta = Text(body.subject + "\n", style="bold #c0caf5")
        date = body.date
        try:
            date = parsedate_to_datetime(date).astimezone().strftime("%a, %d %b %Y %H:%M %Z")
        except (TypeError, ValueError, OverflowError):
            pass
        meta.append(f"From  {body.sender}\nTo    {body.to}\nDate  {date}", style="#7aa2f7")
        if body.attachments:
            meta.append("\nFiles " + ", ".join(body.attachments), style="#e0af68")
        if body.truncated:
            meta.append("\nLarge message: showing a limited preview.", style="#e0af68")
        self.query_one(Static).update(meta)
        await self.query_one(Markdown).update(body.markdown)
        self.query_one(VerticalScroll).scroll_home(animate=False)

    async def clear_body(self, text: str) -> None:
        self.query_one(Static).update(text)
        await self.query_one(Markdown).update("")


class ReaderScreen(Screen):
    BINDINGS = [
        ("escape,q", "back", "Back"),
        ("j,down", "line(1)", "Down"),
        ("k,up", "line(-1)", "Up"),
        ("ctrl+d", "page(1)", "Page down"),
        ("ctrl+u", "page(-1)", "Page up"),
        ("g", "edge(False)", "Top"),
        ("G", "edge(True)", "Bottom"),
        ("r", "respond('reply')", "Reply"),
        ("a", "respond('reply_all')", "Reply all"),
        ("w", "respond('forward')", "Forward"),
    ]

    def __init__(
        self,
        body: MailBody,
        session: "AccountSession | None" = None,
        ref: MessageRef | None = None,
    ):
        super().__init__()
        self.body = body
        self.session = session
        self.ref = ref

    def compose(self) -> ComposeResult:
        yield Static(" KWIKMEEL  /  READER", classes="topbar")
        yield MailView(id="reader-view")
        yield Static(
            " esc  back    ↑/↓  scroll    r  reply    a  reply all    w  forward"
            if self.session
            else " esc  back    ↑/↓  scroll    ctrl+d/u  page",
            classes="bottom-hint",
        )

    def on_mount(self) -> None:
        self.run_worker(self.query_one(MailView).show_body(self.body))
        self.query_one(VerticalScroll).focus()

    def action_respond(self, kind: str) -> None:
        if self.session is not None:
            self.app.start_response(self.session, self.body, kind)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_line(self, direction: int) -> None:
        self.query_one(VerticalScroll).scroll_relative(y=direction * 3, animate=False)

    def action_page(self, direction: int) -> None:
        scroller = self.query_one(VerticalScroll)
        scroller.scroll_relative(y=direction * max(1, scroller.size.height // 2), animate=False)

    def action_edge(self, bottom: bool) -> None:
        scroller = self.query_one(VerticalScroll)
        scroller.scroll_end(animate=False) if bottom else scroller.scroll_home(animate=False)


class Picker(ModalScreen[str | None]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        Binding("ctrl+j,down", "move(1)", "Next", priority=True),
        Binding("ctrl+k,up", "move(-1)", "Previous", priority=True),
    ]

    def __init__(self, title: str, choices: dict[str, str]):
        super().__init__()
        self.title_text = title
        self.choices = choices
        self.matches = list(choices)

    def compose(self) -> ComposeResult:
        with Vertical(classes="picker-dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            yield Input(placeholder="Type to narrow…", id="picker-input")
            yield OptionList(id="picker-options")
            yield Static(" ctrl+j/k  navigate    enter  select    esc  cancel", classes="muted")

    def on_mount(self) -> None:
        self.fill("")
        self.query_one(Input).focus()

    def fill(self, value: str) -> None:
        if value:
            ranked = process.extract(
                value.casefold(),
                {key: text.casefold() for key, text in self.choices.items()},
                scorer=fuzz.WRatio,
                score_cutoff=35,
                limit=50,
            )
            self.matches = [key for _, _, key in ranked]
        else:
            self.matches = list(self.choices)
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(
            Option(Text(one_line(self.choices[key])), id=key) for key in self.matches
        )
        options.highlighted = 0 if self.matches else None

    def on_input_changed(self, event: Input.Changed) -> None:
        self.fill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        index = self.query_one(OptionList).highlighted
        if index is not None and self.matches:
            self.dismiss(self.matches[index])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_move(self, delta: int) -> None:
        options = self.query_one(OptionList)
        if self.matches:
            options.highlighted = ((options.highlighted or 0) + delta) % len(self.matches)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmSend(ModalScreen[bool]):
    BINDINGS = [("escape,n", "cancel", "Cancel"), ("ctrl+enter,y", "confirm", "Send")]

    def __init__(self, sender: str, recipients: str, subject: str):
        super().__init__()
        self.sender = sender
        self.recipients = recipients
        self.subject = subject

    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-dialog"):
            yield Static("Send this message?", classes="dialog-title")
            yield Static(
                f"From: {self.sender}\nTo: {self.recipients}\n"
                f"Subject: {self.subject or '(no subject)'}",
                markup=False,
            )
            yield Static("\n y / ctrl+enter  send    n / esc  keep editing", classes="muted")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)


class AccountSearchInput(Input):
    def check_consume_key(self, key: str, character: str | None) -> bool:
        return key != "R" and super().check_consume_key(key, character)


class AccountPicker(Picker):
    BINDINGS = [
        *Picker.BINDINGS,
        Binding("R", "refresh_accounts", "Refresh inboxes", priority=True),
        Binding("ctrl+d", "details", "Details", priority=True),
        Binding("ctrl+t", "connections", "Test connections", priority=True),
        Binding("ctrl+e", "test_email", "Test email", priority=True),
    ]

    def __init__(self, sessions: tuple["AccountSession", ...], selected_key: str | None = None):
        self.sessions = {session.key: session for session in sessions}
        self.selected_key = selected_key
        self.pending: set[str] = set()
        super().__init__("Accounts", {})
        self.update_choices()

    def compose(self) -> ComposeResult:
        with Vertical(classes="picker-dialog account-dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            yield AccountSearchInput(placeholder="Find account…", id="picker-input")
            yield OptionList(id="picker-options")
            yield Static(
                " ↑/↓ select · enter switch · R refresh · ctrl+d details\n"
                " ctrl+t connections · ctrl+e test email · esc back",
                classes="muted",
            )

    def on_mount(self) -> None:
        super().on_mount()
        self.action_refresh_accounts()

    def update_choices(self) -> None:
        choices = {}
        for key, session in self.sessions.items():
            state = session.status
            stats = state.inbox
            counts = "total unknown · unread unknown · new unknown"
            if stats is not None:
                new = str(stats.new_count) if stats.new_count is not None else "unknown"
                counts = f"total {stats.total} · unread {stats.unread} · new {new}"
            checked = (
                state.last_sync.astimezone().strftime("%d %b %H:%M:%S")
                if state.last_sync
                else "unknown"
            )
            flag = " · checking…" if key in self.pending else ""
            stale = " · stale" if state.stats_stale else ""
            error = f"\n  error {one_line(state.last_error)}" if state.last_error else ""
            choices[key] = (
                f"{session.label} <{session.account.address}>  ·  {state.connection}{flag}\n"
                f"  {counts}{stale} · checked {checked}{error}"
            )
        self.choices = choices

    def fill(self, value: str) -> None:
        current = self.selected_session()
        selected_key = current.key if current else self.selected_key
        super().fill(value)
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(Option(Text(self.choices[key]), id=key) for key in self.matches)
        options.highlighted = (
            self.matches.index(selected_key)
            if selected_key in self.matches
            else 0
            if self.matches
            else None
        )

    def on_screen_resume(self) -> None:
        if self.is_mounted:
            self.refresh_choices()

    def selected_session(self) -> "AccountSession | None":
        index = self.query_one(OptionList).highlighted
        return self.sessions[self.matches[index]] if index is not None and self.matches else None

    def refresh_choices(self) -> None:
        session = self.selected_session()
        self.update_choices()
        self.fill(self.query_one(Input).value)
        if session and session.key in self.matches:
            self.query_one(OptionList).highlighted = self.matches.index(session.key)
        self.app.update_account_bar()

    def action_refresh_accounts(self) -> None:
        for session in self.sessions.values():
            if session.key not in self.pending:
                self.pending.add(session.key)
                self.refresh_account(session)
        self.refresh_choices()

    @work()
    async def refresh_account(self, session: "AccountSession") -> None:
        try:
            await session.refresh_inbox()
        except Exception:
            pass
        finally:
            self.pending.discard(session.key)
            if self.is_mounted and self.app.screen is self:
                self.refresh_choices()

    def action_details(self) -> None:
        session = self.selected_session()
        if session:
            state = session.status
            account = session.account
            self.app.push_screen(
                ReaderScreen(
                    MailBody(
                        0,
                        f"Account {session.label}",
                        session.account.address,
                        "",
                        "",
                        f"Account identity `{session.key}`\n\n"
                        f"Sender {account.name} <{account.address}>\n\n"
                        f"IMAP `{account.host}:{account.imap_port}` · {account.imap_security}\n\n"
                        f"SMTP `{account.smtp_host or account.host}:{account.smtp_port}` "
                        f"· {account.smtp_security}\n\n"
                        f"Send state **{state.send_state}**\n\n"
                        f"Connection **{state.connection}**\n\n"
                        f"Last successful inbox check `{state.last_sync or 'unknown'}`\n\n"
                        f"Inbox statistics {'stale' if state.stats_stale else 'current'}\n\n"
                        f"Last error {state.last_error or 'none'}\n\n"
                        "Use **Ctrl+T** in the account picker to test IMAP and SMTP "
                        "authentication. "
                        "Use **Ctrl+E** to prepare a test email for review and confirmation.",
                    )
                )
            )

    def action_connections(self) -> None:
        session = self.selected_session()
        if session and session.key not in self.pending:
            self.pending.add(session.key)
            self.refresh_choices()
            self.test_connections(session)

    @work()
    async def test_connections(self, session: "AccountSession") -> None:
        try:
            report = await session.check_connections()
            if not self.is_mounted or self.app.screen is not self:
                return
            lines = []
            for name, result in (("IMAP", report.imap), ("SMTP", report.smtp)):
                lines.append(f"{name} authentication {'passed' if result.ok else 'failed'}")
                if result.error:
                    lines.append(one_line(result.error))
            lines.append("No email was sent. TLS certificate verification remains enabled.")
            self.app.push_screen(
                ReaderScreen(
                    MailBody(
                        0,
                        f"Connection test · {session.label}",
                        session.account.address,
                        "",
                        "",
                        "\n\n".join(lines),
                    )
                )
            )
        except Exception as error:
            if self.is_mounted and self.app.screen is self:
                self.app.notify(
                    str(error), title=f"Connection test · {session.label}", severity="error"
                )
        finally:
            self.pending.discard(session.key)
            if self.is_mounted and self.app.screen is self:
                self.refresh_choices()

    def action_test_email(self) -> None:
        session = self.selected_session()
        if session:
            self.app.test_email(session)


HELP = """# The keyboard is the interface

| Key | Mailbox |
| --- | --- |
| `j` / `k`, arrows | Next / previous message |
| `g` / `G` | First / last loaded message |
| `Ctrl+D` / `Ctrl+U` | Page down / up |
| `/` | Fuzzy filter loaded senders and subjects |
| `s` | Search all messages in this folder on the server |
| `Enter` | Read and mark as read |
| `Esc` | Clear search / return to messages |
| `f` | Fuzzy folder picker |
| `c` | Compose / resume saved draft |
| `R` | Refresh and reconnect |
| `L` | Load older messages |
| `Left` / `Right`, `Tab` | Move between visible panes |
| `a` | Accounts, inbox statistics, and diagnostics |
| `t` | Toggle conversations among loaded headers |
| `e` | Expand or collapse the selected conversation |
| `Ctrl+P` or `Space` | Command picker |
| `q` / `Ctrl+Q` | Quit |

## Reader

`Up` and `Down` scroll. `r` replies, `a` replies to all, and `w` forwards.
A saved draft can be resumed, replaced, or kept unchanged.

## Composer

`Tab` moves between fields. `Up` and `Down` move between single-line fields.
The body keeps native cursor navigation. `Alt+Up` and `Alt+Down` move focus from editors.
In the body use `Ctrl+Tab` to move focus.
`Ctrl+S` saves. `Esc` saves and closes. `Ctrl+Enter` or `Ctrl+X` reviews the
recipients before sending. `Ctrl+P` toggles a Markdown preview.
Drafts autosave after a pause in typing and are stored locally as plain text.

## Search

`/` is fuzzy and local. Load more with `L` to include older headers.
`s` sends a text search to IMAP, including message bodies. Press Enter to run it.
It uses the server's substring matching, not fuzzy matching.

Attachments are listed when available, but opening or adding them is outside v1.
Links and remote images never open automatically. No remote content is downloaded.
"""
