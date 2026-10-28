from dataclasses import replace
from typing import TYPE_CHECKING

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Input, Label, Markdown, Static, TextArea

from .mail import build_message
from .models import Draft
from .widgets import ConfirmSend

if TYPE_CHECKING:
    from .session import AccountSession


class Composer(Screen):
    BINDINGS = [
        Binding("escape", "save_close", "Save & close", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+enter,ctrl+x", "review_send", "Send", priority=True),
        Binding("ctrl+p", "preview", "Preview", priority=True),
        Binding("ctrl+tab", "app.focus_next", "Next field", priority=True),
        Binding("alt+down,alt+right", "app.focus_next", "Next field", priority=True),
        Binding("alt+up,alt+left", "app.focus_previous", "Previous field", priority=True),
    ]

    def __init__(self, session: "AccountSession", draft: Draft):
        super().__init__()
        self.session = session
        self.initial = replace(draft)
        self.busy = False
        self.delivered = False
        self.autosave_timer = None
        self.preview_timer = None

    def compose(self) -> ComposeResult:
        yield Static(
            f" KWIKMEEL  /  COMPOSE  ·  {self.session.label} <{self.session.account.address}>",
            classes="topbar",
            markup=False,
        )
        with Vertical(id="compose-fields"):
            yield Label("To")
            yield Input(self.initial.to, placeholder="Name <person@example.com>", id="to")
            yield Label("Cc")
            yield Input(self.initial.cc, placeholder="Optional", id="cc")
            yield Label("Subject")
            yield Input(self.initial.subject, placeholder="What's on your mind?", id="subject")
        with Horizontal(id="compose-content"):
            yield TextArea(self.initial.body, soft_wrap=True, tab_behavior="focus", id="editor")
            yield Markdown(self.initial.body, open_links=False, id="compose-preview")
        yield Static("Draft autosaves locally", id="compose-status", markup=False)
        yield Static(
            " esc  save & close    ctrl+s  save    ctrl+x  send    ctrl+p  preview",
            classes="bottom-hint",
        )

    def on_mount(self) -> None:
        self.query_one("#compose-preview").display = False
        self.query_one("#editor").border_title = "Markdown"
        self.query_one("#to").focus()

    def draft(self) -> Draft:
        return replace(
            self.initial,
            to=self.query_one("#to", Input).value,
            cc=self.query_one("#cc", Input).value,
            subject=self.query_one("#subject", Input).value,
            body=self.query_one("#editor", TextArea).text,
        )

    def status(self, text: str) -> None:
        self.query_one("#compose-status", Static).update(text)

    @on(Input.Changed)
    @on(TextArea.Changed)
    def changed(self) -> None:
        if not self.is_mounted or self.busy or self.delivered:
            return
        if self.autosave_timer:
            self.autosave_timer.stop()
        self.autosave_timer = self.set_timer(0.6, self.action_save)
        if self.query_one("#compose-preview").display:
            if self.preview_timer:
                self.preview_timer.stop()
            self.preview_timer = self.set_timer(0.2, self.update_preview)

    def action_save(self) -> bool:
        if self.busy or self.delivered:
            return False
        try:
            self.session.drafts.save(self.draft())
        except OSError as error:
            self.status(f"Draft could not be saved: {error}")
            return False
        self.status("Draft saved locally")
        return True

    def action_save_close(self) -> None:
        if self.busy:
            self.status("Sending in progress. Please wait for the result.")
        elif self.delivered or self.action_save():
            self.app.pop_screen()

    async def update_preview(self) -> None:
        await self.query_one("#compose-preview", Markdown).update(self.draft().body)

    async def action_preview(self) -> None:
        preview = self.query_one("#compose-preview", Markdown)
        preview.display = not preview.display
        if preview.display:
            await self.update_preview()

    def action_review_send(self) -> None:
        if self.busy or self.delivered:
            return
        draft = self.draft()
        try:
            _, recipients = build_message(self.session.account, draft)
        except ValueError as error:
            self.status(str(error))
            return
        if not self.action_save():
            return
        self.app.push_screen(
            ConfirmSend(
                f"{self.session.label} <{self.session.account.address}>",
                ", ".join(recipients),
                draft.subject,
            ),
            lambda confirmed: self.send_draft(draft) if confirmed else None,
        )

    @work(group="send", exclusive=True)
    async def send_draft(self, draft: Draft) -> None:
        if self.busy or self.delivered:
            return
        self.busy = True
        if self.autosave_timer:
            self.autosave_timer.stop()
        for widget in self.query("Input, TextArea"):
            widget.disabled = True
        self.status("Sending…")
        try:
            outcome = await self.session.send(draft)
        except Exception as error:
            self.send_failed(str(error))
            return
        if not outcome.accepted:
            self.send_failed(outcome.error)
            return
        self.delivered = True
        warnings = list(outcome.warnings)
        if outcome.refused:
            warnings.append(
                "Recipients refused: "
                + ", ".join(outcome.refused)
                + ". Other recipients accepted; do not resend to everyone."
            )
        self.busy = False
        if warnings:
            self.status("Accepted by SMTP. " + " ".join(warnings) + " Press Esc to close.")
        else:
            self.app.pop_screen()
            self.app.notify("Accepted by SMTP. Recipient delivery is not confirmed.", title="Sent")

    def send_failed(self, error: str) -> None:
        self.busy = False
        for widget in self.query("Input, TextArea"):
            widget.disabled = False
        self.status(
            f"Send failed: {error}. Draft kept. If connection dropped, verify delivery "
            "before retrying."
        )

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.focused, Input) and event.key in {"up", "down"}:
            event.stop()
            event.prevent_default()
            self.focus_next() if event.key == "down" else self.focus_previous()
