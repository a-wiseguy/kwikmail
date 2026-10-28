import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from rapidfuzz import fuzz, process
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from .backend import ImapBackend
from .composer import Composer
from .demo import DemoBackend
from .mail import one_line
from .models import MailBody, MailHeader
from .storage import DraftStore
from .widgets import HELP, MailTable, MailView, Picker, ReaderScreen


class MailApp(App):
    TITLE = "kwikmeel"
    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("slash", "search", "Find"),
        Binding("s", "server_search", "Search server"),
        Binding("f", "folders", "Folders"),
        Binding("c", "compose", "Compose"),
        Binding("R", "refresh", "Refresh"),
        Binding("L", "more", "Older"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "safe_quit", "Quit"),
        Binding("ctrl+q,ctrl+c", "safe_quit", "Quit", show=False, priority=True),
        Binding("escape", "escape", "Back", show=False),
        Binding("ctrl+p,space", "commands", "Commands", show=False),
    ]

    def __init__(self, backend: ImapBackend | DemoBackend, drafts: DraftStore):
        super().__init__()
        self.backend = backend
        self.drafts = drafts
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kwikmeel-imap")
        self.folder = backend.account.inbox
        self.folder_names: list[str] = []
        self.headers: list[MailHeader] = []
        self.filtered_headers: list[MailHeader] = []
        self.validity = 0
        self.next_end = 0
        self.total = 0
        self.server_query = ""
        self.filter_query = ""
        self.search_mode = "local"
        self.generation = 0
        self.preview_serial = 0
        self.preview_timer = None
        self.filter_timer = None
        self.loading = False
        self.ready = False

    def compose(self) -> ComposeResult:
        account = (
            "DEMO MAILBOX"
            if isinstance(self.backend, DemoBackend)
            else self.backend.account.address
        )
        yield Static(
            Text.assemble(
                (" KWIKMEEL ", "bold #1a1b26 on #7aa2f7"), ("  /  " + account, "#9aa5ce")
            ),
            id="topbar",
            classes="topbar",
        )
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                yield Static("MAILBOXES", classes="section-title")
                yield OptionList(id="folders")
                yield Static(
                    "LOCAL FIRST\n\n/  fuzzy find\nf  jump folder\nc  compose\n\n?  all shortcuts",
                    id="sidebar-hint",
                    markup=False,
                )
            with Vertical(id="mail-list"):
                yield Static("INBOX", id="mailbox-title", markup=False)
                yield Input(placeholder="Fuzzy find loaded messages…", id="search")
                yield MailTable(id="messages", cursor_type="row", show_row_labels=False)
                yield Static("", id="list-info", markup=False)
            yield MailView(id="preview")
        yield Static("Connecting…", id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.table = self.query_one("#messages", MailTable)
        self.preview = self.query_one("#preview", MailView)
        self.search_input = self.query_one("#search", Input)
        self.status_widget = self.query_one("#status", Static)
        self.folder_list = self.query_one("#folders", OptionList)
        self.title_widget = self.query_one("#mailbox-title", Static)
        self.info_widget = self.query_one("#list-info", Static)
        self.search_input.display = False
        self.table.border_title = "Messages"
        self.preview.border_title = "Preview"
        self.preview.border_subtitle = "enter to read"
        self.table.focus()
        self.ready = True
        self.responsive()
        self.load_folder(reconnect=True)

    async def imap(self, method: str, *args):
        def invoke():
            try:
                return getattr(self.backend, method)(*args)
            except Exception:
                self.backend.close()
                raise

        return await asyncio.get_running_loop().run_in_executor(self.executor, invoke)

    async def on_unmount(self) -> None:
        # Queue shutdown after any running command; cancel pending preview work first.
        self.workers.cancel_all()
        self.executor.submit(self.backend.close)
        self.executor.shutdown(wait=False)

    def status(self, text: str) -> None:
        self.status_widget.update(one_line(text))

    def on_resize(self) -> None:
        if self.ready:
            self.responsive()
            self.set_timer(0.05, self.render_rows)

    def responsive(self) -> None:
        self.set_class(self.size.width < 110, "compact")
        self.set_class(self.size.width < 85, "narrow")

    @work(group="mailbox", exclusive=True)
    async def load_folder(self, more: bool = False, reconnect: bool = False) -> None:
        self.generation += 1
        generation = self.generation
        self.preview_serial += 1
        self.workers.cancel_group(self, "preview")
        self.workers.cancel_group(self, "read")
        self.loading = True
        folder = self.folder
        self.status(f"{'Searching' if self.server_query else 'Loading'} {folder}…")
        if not more:
            self.headers = []
            self.render_rows()
            await self.preview.clear_body("Loading mailbox…")
        try:
            if reconnect:
                await self.imap("close")
            if not self.folder_names or reconnect:
                self.folder_names = await self.imap("folders")
                self.folder_list.clear_options()
                self.folder_list.add_options(
                    Option(Text(one_line(name))) for name in self.folder_names
                )
            page = await self.imap(
                "page",
                folder,
                self.next_end if more else None,
                self.server_query,
                self.validity if more else None,
            )
            if generation != self.generation:
                return
            self.validity, self.total, self.next_end = page.validity, page.total, page.next_end
            existing = {header.uid for header in self.headers}
            self.headers.extend(header for header in page.headers if header.uid not in existing)
            self.render_rows()
            self.title_widget.update(
                f" {one_line(folder)}"
                + (f"  /  {one_line(self.server_query)}" if self.server_query else "")
            )
            if folder in self.folder_names:
                self.folder_list.highlighted = self.folder_names.index(folder)
            self.status("NORMAL  ·  / find  ·  enter read  ·  c compose  ·  ctrl+p commands")
            if not self.headers:
                await self.preview.clear_body(
                    "No messages match." if self.server_query else "Inbox zero."
                )
        except Exception as error:
            self.status(f"Connection failed: {error}  ·  R to retry")
            self.notify(str(error), title="Mail error", severity="error", timeout=8)
        finally:
            if generation == self.generation:
                self.loading = False

    def selected(self) -> MailHeader | None:
        if self.filtered_headers and 0 <= self.table.cursor_row < len(self.filtered_headers):
            return self.filtered_headers[self.table.cursor_row]
        return None

    def render_rows(self) -> None:
        previous = self.selected()
        if self.filter_query:
            choices = {header.uid: header.searchable for header in self.headers}
            ranked = process.extract(
                self.filter_query.casefold(),
                choices,
                scorer=fuzz.WRatio,
                score_cutoff=40,
                limit=500,
            )
            by_uid = {header.uid: header for header in self.headers}
            self.filtered_headers = [by_uid[uid] for _, _, uid in ranked]
        else:
            self.filtered_headers = list(self.headers)
        self.table.clear(columns=True)
        width = max(42, self.table.size.width - 2)
        sender_width = 20 if width >= 65 else 15
        for label, key, size in (
            ("", "seen", 1),
            ("From", "from", sender_width),
            ("Subject", "subject", max(16, width - sender_width - 16)),
            ("Date", "date", 7),
        ):
            self.table.add_column(label, key=key, width=size)
        for header in self.filtered_headers:
            sender = header.sender.split(" <")[0]
            style = "#c0caf5" if header.seen else "bold #e0e6ff"
            self.table.add_row(
                Text(" " if header.seen else "●", style="#9ece6a"),
                Text(sender, style=style),
                Text(header.subject, style=style),
                Text(header.date.strftime("%d %b") if header.date else "", style="#737eaa"),
                key=str(header.uid),
            )
        if previous:
            index = next(
                (i for i, header in enumerate(self.filtered_headers) if header.uid == previous.uid),
                0,
            )
            self.table.move_cursor(row=index)
        self.info_widget.update(
            f" {len(self.filtered_headers)} shown / {len(self.headers)} loaded / "
            f"{self.total} total" + ("   L older" if self.next_end else "")
        )
        if not self.filtered_headers and not self.loading:
            self.preview_serial += 1
            self.workers.cancel_group(self, "preview")
            text = "No messages match." if self.filter_query or self.server_query else "Inbox zero."
            self.run_worker(self.preview.clear_body(text), group="clear-preview", exclusive=True)

    @on(MailTable.RowHighlighted, "#messages")
    def highlight(self, event: MailTable.RowHighlighted) -> None:
        if self.preview_timer:
            self.preview_timer.stop()
        self.preview_serial += 1
        serial = self.preview_serial
        self.workers.cancel_group(self, "preview")
        header = self.selected()
        if header:
            self.preview_timer = self.set_timer(0.12, lambda: self.load_preview(header.uid, serial))

    @work(group="preview", exclusive=True)
    async def load_preview(self, uid: int, serial: int) -> None:
        if serial != self.preview_serial:
            return
        folder, validity = self.folder, self.validity
        try:
            await self.preview.clear_body("Loading message…")
            body = await self.imap("body", folder, validity, uid)
            if serial == self.preview_serial and folder == self.folder:
                await self.preview.show_body(body)
        except Exception as error:
            if serial == self.preview_serial:
                await self.preview.clear_body(f"Preview unavailable: {error}")

    @on(MailTable.RowSelected, "#messages")
    def open_message(self) -> None:
        header = self.selected()
        if header and not self.loading:
            self.read_message(self.folder, self.validity, header.uid)

    @work(group="read", exclusive=True)
    async def read_message(self, folder: str, validity: int, uid: int) -> None:
        try:
            body = await self.imap("body", folder, validity, uid)
            if folder != self.folder:
                return
            await self.push_screen(ReaderScreen(body))
            try:
                await self.imap("mark_seen", folder, validity, uid)
                self.headers = [
                    replace(header, seen=True) if header.uid == uid else header
                    for header in self.headers
                ]
                self.render_rows()
            except Exception as error:
                self.notify(f"Could not mark read: {error}", severity="warning")
        except Exception as error:
            self.notify(str(error), title="Cannot read message", severity="error")

    @on(OptionList.OptionSelected, "#folders")
    def folder_selected(self, event: OptionList.OptionSelected) -> None:
        self.switch_folder(self.folder_names[event.option_index])

    def switch_folder(self, folder: str | None) -> None:
        if folder:
            self.folder = folder
            self.server_query = self.filter_query = ""
            self.search_input.value = ""
            self.search_input.display = False
            self.table.focus()
            self.load_folder()

    def action_folders(self) -> None:
        self.push_screen(
            Picker("Jump to mailbox", {name: name for name in self.folder_names}),
            self.switch_folder,
        )

    def action_search(self) -> None:
        self.search_mode = "local"
        self.search_input.placeholder = "Fuzzy find loaded messages…"
        self.search_input.value = self.filter_query
        self.search_input.display = True
        self.search_input.focus()

    def action_server_search(self) -> None:
        self.search_mode = "server"
        self.search_input.placeholder = "Search this entire folder on the server · Enter to run"
        self.search_input.value = self.server_query
        self.search_input.display = True
        self.search_input.focus()

    @on(Input.Changed, "#search")
    def filter_changed(self, event: Input.Changed) -> None:
        if self.search_mode == "local":
            self.filter_query = event.value
            if self.filter_timer:
                self.filter_timer.stop()
            self.filter_timer = self.set_timer(0.04, self.render_rows)

    @on(Input.Submitted, "#search")
    def search_submitted(self, event: Input.Submitted) -> None:
        if self.search_mode == "server":
            self.server_query = event.value.strip()
            self.filter_query = ""
            self.load_folder()
        self.table.focus()

    def action_escape(self) -> None:
        had_server = bool(self.server_query)
        self.search_mode = "local"
        self.server_query = self.filter_query = ""
        self.search_input.value = ""
        self.search_input.display = False
        self.table.focus()
        self.load_folder() if had_server else self.render_rows()

    def action_refresh(self) -> None:
        self.load_folder(reconnect=True)

    def action_more(self) -> None:
        if self.next_end and not self.loading:
            self.load_folder(more=True)

    def action_compose(self) -> None:
        try:
            draft = self.drafts.load()
        except (OSError, ValueError) as error:
            self.notify(f"Cannot load draft: {error}", severity="error")
            return
        self.push_screen(Composer(draft))

    def action_help(self) -> None:
        self.push_screen(
            ReaderScreen(MailBody(0, "Keyboard reference", "kwikmeel", "you", "", HELP))
        )

    def action_commands(self) -> None:
        commands = {
            "search": "/  Fuzzy find messages",
            "server_search": "s  Search server",
            "folders": "f  Jump to mailbox",
            "compose": "c  Compose / resume draft",
            "refresh": "R  Refresh connection",
            "more": "L  Load older messages",
            "help": "?  Keyboard reference",
            "safe_quit": "q  Quit",
        }
        self.push_screen(Picker("Commands", commands), self.run_command)

    def run_command(self, command: str | None) -> None:
        if command:
            getattr(self, f"action_{command}")()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        mailbox_actions = {
            "search",
            "server_search",
            "folders",
            "compose",
            "refresh",
            "more",
            "help",
            "escape",
            "commands",
        }
        if action in mailbox_actions and len(self.screen_stack) > 1:
            return False
        return True

    def action_safe_quit(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, Composer):
                if screen.busy:
                    self.notify("Wait for sending to finish before quitting.", severity="warning")
                    return
                if not screen.delivered and not screen.action_save():
                    self.notify(
                        "Draft could not be saved. Keep editing or fix the save error.",
                        severity="error",
                    )
                    return
        self.exit()
