from rapidfuzz import fuzz, process
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from .mail import one_line
from .models import MailBody


class MailTable(DataTable):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "scroll_top", "First", show=False),
        Binding("G", "scroll_bottom", "Last", show=False),
        Binding("ctrl+d", "page_down", "Page down", show=False),
        Binding("ctrl+u", "page_up", "Page up", show=False),
    ]


class MailView(Vertical):
    def compose(self) -> ComposeResult:
        yield Static("Select a message to preview", classes="message-meta", markup=False)
        with VerticalScroll(classes="message-scroll"):
            yield Markdown("", open_links=False)

    async def show_body(self, body: MailBody) -> None:
        meta = Text(body.subject + "\n", style="bold #c0caf5")
        meta.append(f"From  {body.sender}\nTo    {body.to}\nDate  {body.date}", style="#7aa2f7")
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
        ("j", "line(1)", "Down"),
        ("k", "line(-1)", "Up"),
        ("ctrl+d", "page(1)", "Page down"),
        ("ctrl+u", "page(-1)", "Page up"),
        ("g", "edge(False)", "Top"),
        ("G", "edge(True)", "Bottom"),
    ]

    def __init__(self, body: MailBody):
        super().__init__()
        self.body = body

    def compose(self) -> ComposeResult:
        yield Static(" KWIKMEEL  /  READER", classes="topbar")
        yield MailView(id="reader-view")
        yield Static(
            " esc  back    j/k  scroll    ctrl+d/u  page    g/G  top/bottom", classes="bottom-hint"
        )

    def on_mount(self) -> None:
        self.run_worker(self.query_one(MailView).show_body(self.body))
        self.query_one(VerticalScroll).focus()

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

    def __init__(self, recipients: str, subject: str):
        super().__init__()
        self.recipients = recipients
        self.subject = subject

    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-dialog"):
            yield Static("Send this message?", classes="dialog-title")
            yield Static(
                f"To: {self.recipients}\nSubject: {self.subject or '(no subject)'}", markup=False
            )
            yield Static("\n y / ctrl+enter  send    n / esc  keep editing", classes="muted")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)


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
| `Tab` / `Shift+Tab` | Move between panes |
| `Ctrl+P` or `Space` | Command picker |
| `q` / `Ctrl+Q` | Quit |

## Composer

`Tab` moves between fields. In the body use `Ctrl+Tab` to move focus.
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
