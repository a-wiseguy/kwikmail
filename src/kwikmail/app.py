from collections.abc import Sequence
from dataclasses import dataclass, replace

from rapidfuzz import fuzz, process
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from .composer import Composer
from .mail import make_response, one_line
from .models import Draft, MailBody, MailHeader, MessageRef
from .session import AccountSession
from .threads import Conversation, flatten_threads, group_threads
from .widgets import HELP, AccountPicker, ConfirmDelete, MailTable, MailView, Picker, ReaderScreen


@dataclass(frozen=True)
class ViewTicket:
    session: AccountSession
    epoch: int
    slot: str
    serial: int


class MailApp(App):
    TITLE = "kwikmail"
    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("slash", "search", "Find"),
        Binding("s", "server_search", "Server", show=False),
        Binding("f", "folders", "Folders"),
        Binding("c", "compose", "Compose"),
        Binding("a", "accounts", "Accounts"),
        Binding("t", "threads", "Conversations", show=False),
        Binding("e", "expand", "Expand", show=False),
        Binding("R", "refresh", "Refresh"),
        Binding("L", "more", "Older", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "safe_quit", "Quit"),
        Binding("ctrl+q,ctrl+c", "safe_quit", "Quit", show=False, priority=True),
        Binding("escape", "escape", "Back", show=False),
        Binding("ctrl+p", "commands", "Commands", show=False),
        Binding("V", "visual", "Select range"),
        Binding("space", "toggle_selected", "Select", show=False),
        Binding("ctrl+a", "select_all", "Select all", show=False),
        Binding("d", "trash", "Trash"),
        Binding("D", "delete", "Delete", show=False),
        Binding("m", "move", "Move"),
        Binding("u", "mark_unread", "Unread", show=False),
        Binding("i", "mark_read", "Read", show=False),
        Binding("left,alt+left", "pane(-1)", "Previous pane", show=False),
        Binding("right,alt+right", "pane(1)", "Next pane", show=False),
    ]

    def __init__(self, sessions: Sequence[AccountSession], default_key: str):
        super().__init__()
        self.sessions = {session.key: session for session in sessions}
        if len(self.sessions) != len(sessions) or default_key not in self.sessions:
            raise ValueError("Accounts need unique identities and a valid default account.")
        self.active_key = default_key
        self.folder = self.session.account.inbox
        self.folder_names: list[str] = []
        self.headers: list[MailHeader] = []
        self._group_source: tuple[MailHeader, ...] = ()
        self._groups: tuple[Conversation, ...] = ()
        self._group_by_uid: dict[int, Conversation] = {}
        self.filtered_headers: list[MailHeader] = []
        self.thread_rows = ()
        self.grouping = True
        self.expanded: set[str] = set()
        self.validity = self.next_end = self.result_total = 0
        self.total: int | None = None
        self.mailbox_error = ""
        self.unread: int | None = None
        self.server_query = self.filter_query = ""
        self.search_mode = "local"
        self.epoch = 0
        self.serials: dict[str, int] = {}
        self.preview_timer = self.filter_timer = None
        self.loading = False
        self.ready = False
        self.selected_uids: set[int] = set()
        self.visual_anchor: int | None = None
        self.mutating = False

    @property
    def session(self) -> AccountSession:
        return self.sessions[self.active_key]

    def ticket(self, slot: str) -> ViewTicket:
        serial = self.serials.get(slot, 0) + 1
        self.serials[slot] = serial
        return ViewTicket(self.session, self.epoch, slot, serial)

    def current(self, ticket: ViewTicket) -> bool:
        return (
            self.session is ticket.session
            and self.epoch == ticket.epoch
            and self.serials.get(ticket.slot) == ticket.serial
        )

    def invalidate_selection(self) -> None:
        self.ticket("preview")
        self.ticket("read")
        if self.preview_timer:
            self.preview_timer.stop()
        self.workers.cancel_group(self, "preview")
        self.workers.cancel_group(self, "read")

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar", classes="topbar", markup=False)
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                yield Static("MAILBOXES", classes="section-title")
                yield OptionList(id="folders")
                yield Static(
                    "/  fuzzy find\nf  folders\na  accounts\nt  conversations\ne  expand\n\n"
                    "ctrl+p  commands",
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
        self.topbar_widget = self.query_one("#topbar", Static)
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
        self.update_account_bar()
        self.load_folder()

    async def on_unmount(self) -> None:
        import asyncio

        self.workers.cancel_all()
        await asyncio.gather(*(session.close() for session in self.sessions.values()))

    def status(self, text: str) -> None:
        self.status_widget.update(one_line(text))
        self.update_account_bar()

    def update_account_bar(self) -> None:
        session = self.session
        state = session.status
        inbox = state.inbox
        stats = "inbox unknown" if inbox is None else f"inbox {inbox.total} · unread {inbox.unread}"
        stale = " · stale" if state.stats_stale else ""
        self.topbar_widget.update(
            f" KWIKMAIL  /  {session.label} <{session.account.address}>  ·  "
            f"{state.connection}  ·  {stats}{stale}"
        )

    def on_resize(self) -> None:
        if self.ready:
            self.responsive()
            self.set_timer(0.05, self.render_rows)

    def responsive(self) -> None:
        self.set_class(self.size.width < 110, "compact")
        self.set_class(self.size.width < 85, "narrow")

    def load_folder(
        self,
        more: bool = False,
        reconnect: bool = False,
        focus_uid: int | None = None,
    ) -> None:
        self.epoch += 1
        self.clear_selected()
        self.invalidate_selection()
        ticket = self.ticket("mailbox")
        self.loading = True
        selected_uid = (
            focus_uid
            if focus_uid is not None
            else (self.selected().uid if more and self.selected() else None)
        )
        if not more:
            self.total = self.unread = None
            self.mailbox_error = ""
            self.headers = []
            self.filtered_headers = []
            self.thread_rows = ()
            self.render_rows()
        self.status(f"{'Searching' if self.server_query else 'Loading'} {self.folder}…")
        self._load_folder(
            ticket,
            self.folder,
            self.next_end if more else None,
            self.server_query,
            self.validity if more else None,
            reconnect,
            selected_uid,
        )

    @work(group="mailbox", exclusive=True)
    async def _load_folder(
        self,
        ticket: ViewTicket,
        folder: str,
        end: int | None,
        query: str,
        validity: int | None,
        reconnect: bool,
        selected_uid: int | None,
    ) -> None:
        session = ticket.session
        try:
            if not self.current(ticket):
                return
            if end is None:
                await self.preview.clear_body("Loading mailbox…")
            if reconnect:
                await session.reconnect()
            if not self.current(ticket):
                return
            if not self.folder_names or reconnect:
                names = await session.folders()
                if not self.current(ticket):
                    return
                self.folder_names = names
                self.folder_list.clear_options()
                self.folder_list.add_options(Option(Text(one_line(name))) for name in names)
            snapshot = await session.mailbox(folder, end, query, validity)
            if not self.current(ticket):
                return
            page, stats = snapshot.page, snapshot.stats
            self.validity, self.total, self.next_end = page.validity, stats.total, page.next_end
            self.unread = stats.unread
            self.result_total = page.total
            existing = {header.uid for header in self.headers}
            self.headers.extend(header for header in page.headers if header.uid not in existing)
            self.render_rows(selected_uid)
            self.title_widget.update(
                f" {one_line(folder)}" + (f"  /  {one_line(query)}" if query else "")
            )
            if folder in self.folder_names:
                self.folder_list.highlighted = self.folder_names.index(folder)
            self.status("/ find  ·  enter read  ·  t conversations  ·  ctrl+p commands")
            if not self.headers:
                await self.preview.clear_body("No messages match." if query else "Inbox zero.")
        except Exception as error:
            if self.current(ticket):
                self.mailbox_error = f"Mailbox unavailable: {error}"
                await self.preview.clear_body(self.mailbox_error)
                if not self.current(ticket):
                    return
                self.render_rows()
                self.status(f"Connection failed: {error}  ·  R to retry")
                self.notify(str(error), title="Mail error", severity="error", timeout=8)
        finally:
            if self.current(ticket):
                self.loading = False
                self.highlight(None)

    def selected(self) -> MailHeader | None:
        if self.filtered_headers and 0 <= self.table.cursor_row < len(self.filtered_headers):
            return self.filtered_headers[self.table.cursor_row]
        return None

    def conversation_groups(self) -> tuple[Conversation, ...]:
        source = tuple(self.headers)
        if source != self._group_source:
            self._groups = group_threads(source)
            self._group_by_uid = {
                header.uid: group for group in self._groups for header in group.members
            }
            self._group_source = source
        return self._groups

    def render_rows(self, selected_uid: int | None = None) -> None:
        if selected_uid is None:
            previous = self.selected()
            selected_uid = previous.uid if previous else None
        previous_order = tuple(header.uid for header in self.filtered_headers)
        matches = None
        matched_headers = self.headers
        if self.filter_query:
            ranked = process.extract(
                self.filter_query.casefold(),
                {header.uid: header.searchable for header in self.headers},
                scorer=fuzz.WRatio,
                score_cutoff=40,
                limit=500,
            )
            by_uid = {header.uid: header for header in self.headers}
            matched_headers = [by_uid[uid] for _, _, uid in ranked]
            matches = {header.uid for header in matched_headers}
        if self.grouping:
            groups = self.conversation_groups()
            if matches is not None:
                rank = {header.uid: index for index, header in enumerate(matched_headers)}
                unmatched = len(rank)

                def relevance(header):
                    return rank.get(header.uid, unmatched)

                matched_groups = {}
                for header in matched_headers:
                    group = self._group_by_uid[header.uid]
                    matched_groups.setdefault(group.key, group)
                groups = tuple(
                    replace(group, members=tuple(sorted(group.members, key=relevance)))
                    for group in matched_groups.values()
                )
            for group in groups:
                if selected_uid and any(h.uid == selected_uid for h in group.members):
                    if group.members[0].uid != selected_uid:
                        self.expanded.add(group.key)
            self.thread_rows = flatten_threads(groups, self.expanded, matches)
            self.filtered_headers = [row.header for row in self.thread_rows]
        else:
            self.thread_rows = ()
            self.filtered_headers = list(matched_headers)
        if previous_order != tuple(header.uid for header in self.filtered_headers):
            self.clear_selected()
        self.table.border_title = "Conversations [loaded/unread]" if self.grouping else "Messages"
        self.table.clear(columns=True)
        width = max(30, self.table.size.width - 2)
        sender_width = 18 if width >= 65 else 12
        date_width = 12 if width >= 65 else 6
        for label, key, size in (
            ("", "seen", 1),
            ("From", "from", sender_width),
            ("Subject", "subject", max(12, width - sender_width - date_width - 10)),
            ("Date", "date", date_width),
        ):
            self.table.add_column(label, key=key, width=size)
        for index, header in enumerate(self.filtered_headers):
            subject = header.subject
            unseen = not header.seen
            if self.grouping:
                row = self.thread_rows[index]
                if row.depth:
                    subject = "  ↳ " + subject
                elif row.count > 1:
                    subject = ("▾ " if row.expanded else "▸ ") + (
                        f"[{row.count}/{row.unread}] " + subject
                    )
                    unseen = bool(row.unread)
            style = "bold #e0e6ff" if unseen else "#c0caf5"
            self.table.add_row(
                Text("●" if unseen else " ", style="#9ece6a"),
                Text(header.sender.split(" <")[0], style=style),
                Text(subject, style=style),
                Text(
                    header.date.astimezone().strftime("%d %b %H:%M" if date_width > 6 else "%d %b")
                    if header.date
                    else "",
                    style="#9aa5ce",
                ),
                key=str(header.uid),
            )
        index = next(
            (i for i, header in enumerate(self.filtered_headers) if header.uid == selected_uid), 0
        )
        self.table.move_cursor(row=index)
        self.paint_selection()
        result = (
            f"{len(matched_headers)} matches · " if self.filter_query or self.server_query else ""
        )
        if self.server_query:
            result += f"{self.result_total} server matches · "
        unread = str(self.unread) if self.unread is not None else "unknown"
        grouping = " · grouped loaded" if self.grouping else ""
        self.info_widget.update(
            f" {result}{len(self.headers)} loaded · "
            f"{self.total if self.total is not None else 'unknown'} total · {unread} unread"
            + grouping
            + (" · L older" if self.next_end else "")
        )
        if not self.filtered_headers and not self.loading:
            self.invalidate_selection()
            text = self.mailbox_error or (
                "No messages match."
                if self.filter_query or self.server_query
                else "Inbox zero."
                if self.total is not None
                else "Mailbox not loaded."
            )
            self.clear_preview(self.ticket("preview"), text)

    @work(group="clear-preview", exclusive=True)
    async def clear_preview(self, ticket: ViewTicket, text: str) -> None:
        if self.current(ticket):
            await self.preview.clear_body(text)

    @on(MailTable.RowHighlighted, "#messages")
    def highlight(self, event: MailTable.RowHighlighted) -> None:
        self.invalidate_selection()
        self.extend_selection()
        header = self.selected()
        if header and not self.loading:
            ticket = self.ticket("preview")
            ref = MessageRef(ticket.session.key, self.folder, self.validity, header.uid)
            self.preview_timer = self.set_timer(0.12, lambda: self.load_preview(ticket, ref))

    @work(group="preview", exclusive=True)
    async def load_preview(self, ticket: ViewTicket, ref: MessageRef) -> None:
        if not self.current(ticket):
            return
        try:
            await self.preview.clear_body("Loading message…")
            body = await ticket.session.body(ref)
            if self.current(ticket):
                await self.preview.show_body(body)
        except Exception as error:
            if self.current(ticket):
                await self.preview.clear_body(f"Preview unavailable: {error}")

    @on(MailTable.RowSelected, "#messages")
    def open_message(self) -> None:
        header = self.selected()
        if header and not self.loading:
            self.read_message(MessageRef(self.session.key, self.folder, self.validity, header.uid))

    def read_message(self, ref: MessageRef) -> None:
        self._read_message(self.ticket("read"), ref)

    @work(group="read", exclusive=True)
    async def _read_message(self, ticket: ViewTicket, ref: MessageRef) -> None:
        try:
            if not self.current(ticket):
                return
            body = await ticket.session.body(ref)
            if not self.current(ticket):
                return
            reader = ReaderScreen(body, ticket.session, ref)
            await self.push_screen(reader)
            if not self.current(ticket):
                if self.screen is reader:
                    self.pop_screen()
                return
            try:
                was_unread = any(h.uid == ref.uid and not h.seen for h in self.headers)
                await ticket.session.mark_seen(ref)
                if not self.current(ticket):
                    return
                self.headers = [
                    replace(header, seen=True) if header.uid == ref.uid else header
                    for header in self.headers
                ]
                if was_unread and self.unread is not None:
                    self.unread = max(0, self.unread - 1)
                self.render_rows(ref.uid)
                self.update_account_bar()
            except Exception as error:
                if self.current(ticket):
                    self.notify(f"Could not mark read: {error}", severity="warning")
        except Exception as error:
            if self.current(ticket):
                self.notify(str(error), title="Cannot read message", severity="error")

    @on(OptionList.OptionSelected, "#folders")
    def folder_selected(self, event: OptionList.OptionSelected) -> None:
        self.switch_folder(self.folder_names[event.option_index])

    def switch_folder(self, folder: str | None) -> None:
        if folder:
            self.folder = folder
            self.expanded.clear()
            self.server_query = self.filter_query = ""
            self.search_input.value = ""
            self.search_input.display = False
            self.table.focus()
            self.load_folder()

    def switch_account(self, key: str | None) -> None:
        if key and key in self.sessions:
            self.active_key = key
            self.folder_names = []
            self.folder_list.clear_options()
            self.validity = self.next_end = self.result_total = 0
            self.total = None
            self.unread = None
            self.switch_folder(self.session.account.inbox)

    def clear_selected(self) -> None:
        self.selected_uids.clear()
        self.visual_anchor = None

    def paint_selection(self) -> None:
        for header in self.filtered_headers:
            selected = header.uid in self.selected_uids
            self.table.update_cell(
                str(header.uid),
                "seen",
                Text(
                    "✓" if selected else "●" if not header.seen else " ",
                    style="bold #e0af68" if selected else "#9ece6a",
                ),
            )
        mode = "visual · " if self.visual_anchor is not None else ""
        self.table.border_subtitle = (
            f"{mode}{len(self.selected_uids)} selected" if self.selected_uids else ""
        )

    def extend_selection(self) -> None:
        if self.visual_anchor is None:
            return
        order = [header.uid for header in self.filtered_headers]
        if self.visual_anchor not in order:
            self.clear_selected()
        else:
            start, stop = sorted((order.index(self.visual_anchor), self.table.cursor_row))
            self.selected_uids = set(order[start : stop + 1])
        self.paint_selection()

    def action_visual(self) -> None:
        header = self.selected()
        if header is None:
            return
        if self.visual_anchor is not None:
            self.visual_anchor = None
        else:
            self.visual_anchor = header.uid
            self.extend_selection()
        self.paint_selection()

    def action_toggle_selected(self) -> None:
        header = self.selected()
        if header:
            self.visual_anchor = None
            self.selected_uids.symmetric_difference_update({header.uid})
            self.paint_selection()

    def action_select_all(self) -> None:
        self.visual_anchor = None
        self.selected_uids = {header.uid for header in self.filtered_headers}
        self.paint_selection()

    def selected_refs(self) -> tuple[MessageRef, ...]:
        uids = self.selected_uids or ({self.selected().uid} if self.selected() else set())
        return tuple(
            MessageRef(self.session.key, self.folder, self.validity, header.uid)
            for header in self.filtered_headers
            if header.uid in uids
        )

    def action_trash(self) -> None:
        self.request_message_action("trash", self.selected_refs(), self.session)

    def action_delete(self) -> None:
        self.request_message_action("delete", self.selected_refs(), self.session)

    def action_move(self) -> None:
        self.request_message_action("move", self.selected_refs(), self.session)

    def action_mark_read(self) -> None:
        self.request_message_action("read", self.selected_refs(), self.session)

    def action_mark_unread(self) -> None:
        self.request_message_action("unread", self.selected_refs(), self.session)

    def request_message_action(
        self,
        kind: str,
        refs: tuple[MessageRef, ...],
        session: AccountSession,
    ) -> None:
        if not refs or self.loading or self.mutating:
            return
        context = (session, self.folder, self.validity, self.epoch)
        if any(
            (ref.account, ref.folder, ref.validity) != (session.key, self.folder, self.validity)
            for ref in refs
        ):
            return

        def chosen(value) -> None:
            if not value or not self.message_context_current(context) or self.mutating:
                return
            self.mutating = True
            self.mutate_messages(context, kind, refs, value if kind == "move" else None)

        if kind == "move":
            choices = {name: name for name in self.folder_names if name != self.folder}
            self.push_screen(Picker(f"Move {len(refs)} message(s) to", choices), chosen)
        elif kind == "delete":
            self.push_screen(ConfirmDelete(len(refs), self.folder), chosen)
        else:
            chosen(True)

    def message_context_current(self, context) -> bool:
        return context == (self.session, self.folder, self.validity, self.epoch)

    @work(group="mutation")
    async def mutate_messages(self, context, kind, refs, destination) -> None:
        session = context[0]
        try:
            if kind == "move":
                await session.move(refs, destination)
            elif kind == "trash":
                await session.trash(refs)
            elif kind == "delete":
                await session.delete_permanently(refs)
            else:
                await session.set_seen(refs, kind == "read")
            if self.message_context_current(context):
                if isinstance(self.screen, ReaderScreen):
                    self.pop_screen()
                self.notify(f"{len(refs)} message(s): {kind}")
                targets = {ref.uid for ref in refs}
                row = self.table.cursor_row
                candidates = self.filtered_headers[row:] + list(
                    reversed(self.filtered_headers[:row])
                )
                focus_uid = (
                    refs[0].uid
                    if kind in {"read", "unread"}
                    else next(
                        (header.uid for header in candidates if header.uid not in targets), None
                    )
                )
                self.load_folder(focus_uid=focus_uid)
        except Exception as error:
            self.notify(f"Message action failed: {error}", severity="error", timeout=10)
            if self.message_context_current(context):
                self.load_folder()
        finally:
            self.mutating = False
            if self.ready:
                self.update_account_bar()

    def action_accounts(self) -> None:
        self.push_screen(
            AccountPicker(tuple(self.sessions.values()), self.active_key), self.switch_account
        )

    def action_folders(self) -> None:
        self.push_screen(
            Picker("Jump to mailbox", {name: name for name in self.folder_names}),
            self.switch_folder,
        )

    def action_threads(self) -> None:
        self.clear_selected()
        self.grouping = not self.grouping
        self.render_rows()
        self.status(
            "Conversations use loaded messages only." if self.grouping else "Individual messages."
        )

    def action_expand(self) -> None:
        if not self.grouping or not self.thread_rows:
            return
        self.clear_selected()
        row = self.thread_rows[self.table.cursor_row]
        if row.thread_key in self.expanded:
            self.expanded.remove(row.thread_key)
            group = next(g for g in self.conversation_groups() if g.key == row.thread_key)
            self.render_rows(group.members[0].uid)
        else:
            self.expanded.add(row.thread_key)
            self.render_rows()

    def action_pane(self, direction: int) -> None:
        if isinstance(self.focused, Input):
            return
        panes = [self.folder_list, self.table, self.preview.query_one(".message-scroll")]
        panes = [pane for pane in panes if pane.display and pane.visible and pane.region.width > 0]
        if len(panes) < 2:
            return
        index = next((i for i, pane in enumerate(panes) if pane is self.focused), 1)
        panes[(index + direction) % len(panes)].focus()

    def action_search(self) -> None:
        self.clear_selected()
        self.paint_selection()
        self.search_mode = "local"
        self.search_input.placeholder = "Fuzzy find loaded messages…"
        self.search_input.value = self.filter_query
        self.search_input.display = True
        self.search_input.focus()

    def action_server_search(self) -> None:
        self.clear_selected()
        self.paint_selection()
        self.search_mode = "server"
        self.search_input.placeholder = "Search entire folder on server · Enter to run"
        self.search_input.value = self.server_query
        self.search_input.display = True
        self.search_input.focus()

    @on(Input.Changed, "#search")
    def filter_changed(self, event: Input.Changed) -> None:
        if self.search_mode == "local":
            if self.ready and self.filter_query != event.value:
                self.clear_selected()
                self.paint_selection()
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
        if self.selected_uids or self.visual_anchor is not None:
            self.clear_selected()
            self.paint_selection()
            return
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
        self.start_draft(self.session)

    def start_draft(self, session: AccountSession, prepared: Draft | None = None) -> None:
        try:
            saved = session.drafts.load()
        except (OSError, ValueError) as error:
            self.notify(f"Cannot load draft: {error}", severity="error")
            return
        if prepared is not None and not saved.empty:

            def chosen(choice):
                if choice == "resume":
                    self.push_screen(Composer(session, saved))
                elif choice == "replace":
                    self.push_screen(Composer(session, prepared))

            self.push_screen(
                Picker(
                    "A saved draft exists",
                    {
                        "resume": "Resume saved draft",
                        "replace": "Replace saved draft with this message",
                        "cancel": "Cancel and keep saved draft",
                    },
                ),
                chosen,
            )
        else:
            self.push_screen(Composer(session, prepared if prepared is not None else saved))

    def start_response(self, session: AccountSession, body: MailBody, kind: str) -> None:
        draft = make_response(body, kind=kind, own_addresses=frozenset({session.account.address}))
        self.start_draft(session, draft)

    def test_email(self, session: AccountSession) -> None:
        self.start_draft(
            session,
            Draft(
                to=session.account.address,
                subject="Kwikmail connection test",
                body="This is a test email sent from Kwikmail. "
                "SMTP acceptance does not confirm delivery.",
            ),
        )

    def action_help(self) -> None:
        self.push_screen(
            ReaderScreen(MailBody(0, "Keyboard reference", "kwikmail", "you", "", HELP))
        )

    def action_commands(self) -> None:
        commands = {
            "trash": "d  Move selected messages to Trash",
            "delete": "D  Permanently delete selected messages",
            "move": "m  Move selected messages to a folder",
            "visual": "V  Select a live range",
            "select_all": "ctrl+a  Select all visible messages",
            "mark_unread": "u  Mark selected messages unread",
            "mark_read": "i  Mark selected messages read",
            "search": "/  Fuzzy find messages",
            "server_search": "s  Search server",
            "folders": "f  Jump to mailbox",
            "accounts": "a  Accounts and diagnostics",
            "compose": "c  Compose / resume draft",
            "threads": "t  Toggle conversations",
            "expand": "e  Expand / collapse conversation",
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
        selection_actions = {
            "visual",
            "toggle_selected",
            "select_all",
            "trash",
            "delete",
            "move",
            "mark_read",
            "mark_unread",
        }
        if action in selection_actions:
            return (
                self.ready
                and len(self.screen_stack) == 1
                and self.focused is self.table
                and not self.loading
                and not self.mutating
            )
        mailbox_actions = {
            "search",
            "server_search",
            "folders",
            "accounts",
            "compose",
            "threads",
            "expand",
            "refresh",
            "more",
            "help",
            "escape",
            "commands",
            "pane",
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
