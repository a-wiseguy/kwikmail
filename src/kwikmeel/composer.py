import asyncio
from typing import TYPE_CHECKING, cast

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Input, Label, Markdown, Static, TextArea

from .mail import build_message
from .models import Draft
from .widgets import ConfirmSend

if TYPE_CHECKING:
    from .app import MailApp


class Composer(Screen):
    BINDINGS = [
        Binding("escape", "save_close", "Save & close", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+enter,ctrl+x", "review_send", "Send", priority=True),
        Binding("ctrl+p", "preview", "Preview", priority=True),
        Binding("ctrl+tab", "focus_next", "Next field", priority=True),
    ]

    def __init__(self, draft: Draft):
        super().__init__()
        self.initial = draft
        self.busy = False
        self.delivered = False
        self.autosave_timer = None
        self.preview_timer = None

    @property
    def mail_app(self) -> "MailApp":
        return cast("MailApp", self.app)

    def compose(self) -> ComposeResult:
        yield Static(" KWIKMEEL  /  COMPOSE", classes="topbar")
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
        return Draft(
            self.query_one("#to", Input).value,
            self.query_one("#cc", Input).value,
            self.query_one("#subject", Input).value,
            self.query_one("#editor", TextArea).text,
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
            self.mail_app.drafts.save(self.draft())
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
            _, recipients = build_message(self.mail_app.backend.account, draft)
        except ValueError as error:
            self.status(str(error))
            return
        if not self.action_save():
            return
        self.app.push_screen(
            ConfirmSend(", ".join(recipients), draft.subject),
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
            raw, result = await asyncio.to_thread(self.mail_app.backend.send, draft)
        except Exception as error:
            self.busy = False
            for widget in self.query("Input, TextArea"):
                widget.disabled = False
            self.status(
                f"Send failed: {error}. Draft kept. If connection dropped, verify delivery "
                "before retrying."
            )
            return
        # SMTP has accepted this message. Never offer to resend because APPEND failed.
        self.delivered = True
        warnings = [result.warning] if result.warning else []
        try:
            self.mail_app.drafts.clear()
        except OSError as error:
            warnings.append(f"Remove the saved draft manually: {error}")
        if result.refused:
            warnings.append(
                "Not delivered to: "
                + ", ".join(result.refused)
                + ". Other recipients accepted; do not resend to everyone."
            )
        try:
            await self.mail_app.imap("append_sent", raw)
        except Exception as error:
            warnings.append(f"Message accepted by SMTP, but saving the Sent copy failed: {error}")
        self.busy = False
        if warnings:
            self.status("Sent. " + " ".join(warnings) + " Press Esc to close.")
        else:
            self.app.pop_screen()
            self.app.notify("Message sent and draft cleared.", title="Sent")
