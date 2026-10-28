import json
from dataclasses import replace
from email import policy
from email.parser import BytesParser

import pytest

from kwikmeel.config import Account
from kwikmeel.mail import (
    addresses,
    build_message,
    make_response,
    message_ids,
    parse_body,
    parse_header,
)
from kwikmeel.models import Draft, MailBody, MailHeader
from kwikmeel.storage import DraftStore

OWN = frozenset({"me@example.com", "alias@example.com"})
ACCOUNT = Account("localhost", "me", "me@example.com", "Me")
ORIGINAL = MailBody(
    7,
    "Plans",
    "Mira <mira@example.com>",
    "Me <me@example.com>, Noor <noor@example.com>",
    "Wed, 28 Oct 2026 12:30:00 +0100",
    "Hello\n\n**Ready**?",
    cc="Other <other@example.com>",
    message_id="<source@example.com>",
    references=("<root@example.com>", "<previous@example.com>"),
)


def response(original=ORIGINAL, kind="reply"):
    return make_response(original, kind=kind, own_addresses=OWN)


def recipients(value):
    return [address.casefold() for _, address in addresses(value)]


def test_existing_positional_models_and_empty_draft_remain_compatible():
    assert MailHeader(1, "subject", "sender", None, False).message_id == ""
    assert MailBody(1, "subject", "sender", "to", "date", "body").references == ()
    assert Draft("to", "cc", "subject", "body").in_reply_to == ""
    assert Draft(in_reply_to="<parent@example.com>", references=("<root@example.com>",)).empty


def test_parsers_capture_reply_headers_and_unfold_references():
    raw = (
        b"From: Mira <mira@example.com>\r\n"
        b"To: me@example.com\r\nCc: noor@example.com\r\n"
        b"Reply-To: team@example.com\r\nMessage-ID: <source@example.com>\r\n"
        b"In-Reply-To: <previous@example.com>\r\n"
        b"References: <root@example.com>\r\n\t<previous@example.com>\r\n\r\nHi"
    )
    header = parse_header(7, raw, (b"\\Seen",))
    body = parse_body(7, raw)
    assert header.message_id == body.message_id == "<source@example.com>"
    assert header.references == body.references == ("<root@example.com>", "<previous@example.com>")
    assert header.in_reply_to == body.in_reply_to == "<previous@example.com>"
    assert body.cc == "noor@example.com"
    assert body.reply_to == "team@example.com"


def test_message_ids_ignore_malformed_and_control_characters():
    value = (
        "garbage <no-domain> <bad id@example.com> <bad\x1b@example.com> "
        "<.bad@example.com> <bad..id@example.com> <bad@example..com> <ok@example.com>"
    )
    assert message_ids(value) == ("<ok@example.com>",)
    assert message_ids("<" + "x" * 255 + "@example.com>") == ()
    assert message_ids("x" * 16_384 + "<late@example.com>") == ()


def test_message_ids_deduplicate_and_bound_ancestry():
    value = " ".join(f"<m{i}@example.com>" for i in range(100))
    result = message_ids(value)
    assert len(result) == 50
    assert result[0] == "<m0@example.com>"
    assert result[1] == "<m51@example.com>"
    assert result[-1] == "<m99@example.com>"
    assert message_ids("<a@example.com> <a@example.com>") == ("<a@example.com>",)


@pytest.mark.parametrize("reply_to", ["team@example.com", "Team <team@example.com>"])
def test_reply_prefers_reply_to(reply_to):
    draft = response(replace(ORIGINAL, reply_to=reply_to))
    assert recipients(draft.to) == ["team@example.com"]
    assert draft.cc == ""
    assert draft.subject == "Re: Plans"
    assert draft.in_reply_to == "<source@example.com>"
    assert draft.references == (
        "<root@example.com>",
        "<previous@example.com>",
        "<source@example.com>",
    )
    assert "> Hello\n>\n> **Ready**?" in draft.body


@pytest.mark.parametrize("reply_to", ["", "broken", "team@example.com\r\nBcc: bad@example.com"])
def test_reply_falls_back_to_sender_for_absent_or_invalid_reply_to(reply_to):
    assert recipients(response(replace(ORIGINAL, reply_to=reply_to)).to) == ["mira@example.com"]


def test_reply_all_deduplicates_case_insensitively_across_to_cc_and_own_aliases():
    original = replace(
        ORIGINAL,
        reply_to="Mira <MIRA@example.com>",
        to="me@example.com, noor@example.com, mira@example.com, ALIAS@example.com",
        cc="NOOR@example.com, other@example.com, Me <ME@example.com>",
    )
    draft = response(original, "reply_all")
    assert recipients(draft.to) == ["mira@example.com", "noor@example.com"]
    assert recipients(draft.cc) == ["other@example.com"]


@pytest.mark.parametrize("kind", ["reply", "reply_all"])
def test_self_sent_reply_targets_original_recipients(kind):
    original = replace(ORIGINAL, sender="Me <ME@example.com>")
    draft = response(original, kind)
    assert recipients(draft.to) == ["noor@example.com"]
    assert recipients(draft.cc) == (["other@example.com"] if kind == "reply_all" else [])


def test_self_sent_reply_still_honors_external_reply_to():
    original = replace(ORIGINAL, sender="me@example.com", reply_to="team@example.com")
    assert recipients(response(original).to) == ["team@example.com"]


def test_reply_with_no_valid_recipient_stays_editable():
    draft = response(replace(ORIGINAL, sender="unknown", reply_to="invalid"))
    assert draft.to == ""
    with pytest.raises(ValueError):
        build_message(ACCOUNT, draft)


@pytest.mark.parametrize(("kind", "subject"), [("reply", "rE: Plans"), ("forward", "Fw: Plans")])
def test_subject_prefix_is_not_duplicated(kind, subject):
    assert response(replace(ORIGINAL, subject=subject), kind).subject == subject


def test_replies_fall_back_to_in_reply_to_ancestry_and_keep_parent_last():
    original = replace(ORIGINAL, references=(), in_reply_to="<previous@example.com>")
    assert response(original).references == ("<previous@example.com>", "<source@example.com>")
    original = replace(ORIGINAL, references=("<source@example.com>", "<root@example.com>"))
    assert response(original).references == ("<root@example.com>", "<source@example.com>")
    assert response(replace(ORIGINAL, message_id="malformed")).in_reply_to == ""


def test_forward_quotes_visible_headers_without_reply_relationship():
    draft = response(ORIGINAL, "forward")
    assert draft.to == draft.cc == draft.in_reply_to == ""
    assert draft.references == ()
    assert draft.subject == "Fwd: Plans"
    for header in ("From: Mira", "To: Me", "Cc: Other", "Subject: Plans", "Date: Wed"):
        assert header in draft.body
    assert "> Hello" in draft.body
    draft.to = "friend@example.com"
    message, _ = build_message(ACCOUNT, draft)
    assert "In-Reply-To" not in message and "References" not in message


@pytest.mark.parametrize("kind", ["reply", "reply_all", "forward"])
def test_response_marks_truncation_and_omitted_attachments(kind):
    draft = response(
        replace(ORIGINAL, truncated=True, attachments=("notes.pdf", "picture.png")), kind
    )
    assert "[Original message truncated.]" in draft.body
    assert "[Attachments omitted: notes.pdf, picture.png]" in draft.body
    if not draft.to:
        draft.to = "friend@example.com"
    message, _ = build_message(ACCOUNT, draft)
    assert list(message.iter_attachments()) == []


def test_response_bounds_quoted_source_and_sanitizes_terminal_controls():
    original = replace(ORIGINAL, markdown="x" * 80_001 + "secret-tail\x1b", subject="Hi\n\x1b")
    draft = response(original)
    assert "secret-tail" not in draft.body
    assert "[Original message truncated.]" in draft.body
    assert "\x1b" not in draft.body + draft.subject
    assert draft.subject == "Re: Hi"


def test_draft_metadata_survives_save_edit_reload_and_emitted_mime(tmp_path):
    store = DraftStore(tmp_path / "account/draft.json")
    draft = response()
    store.save(draft)
    restored = store.load()
    restored.to = "friend@example.com"
    restored.subject = "Re: Updated plans"
    restored.body = "New answer\n" + restored.body
    store.save(restored)
    recovered = DraftStore(store.path).load()
    assert recovered == restored
    emitted, envelope = build_message(ACCOUNT, recovered)
    wire = BytesParser(policy=policy.default).parsebytes(emitted.as_bytes())
    assert str(wire["In-Reply-To"]) == "<source@example.com>"
    assert str(wire["References"]).split() == [
        "<root@example.com>",
        "<previous@example.com>",
        "<source@example.com>",
    ]
    assert str(wire["From"]) == "Me <me@example.com>"
    assert envelope == ["friend@example.com"]
    assert wire.get_body(("plain",)).get_content().startswith("New answer")


def test_legacy_draft_load_does_not_rewrite_file(tmp_path):
    path = tmp_path / "draft.json"
    raw = '{"to":"friend@example.com", "cc":"", "subject":"Hi", "body":"Text"}'
    path.write_text(raw)
    assert DraftStore(path).load() == Draft("friend@example.com", "", "Hi", "Text")
    assert path.read_text() == raw


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"unknown": "value"},
        {"references": "<x@example.com>"},
        {"references": [1]},
        {"in_reply_to": []},
        {"to": None},
    ],
)
def test_draft_schema_rejects_invalid_metadata(tmp_path, data):
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        DraftStore(path).load()


def test_emitted_metadata_cannot_inject_headers_and_bounds_references():
    draft = Draft(
        to="friend@example.com",
        in_reply_to="<source@example.com>\r\nBcc: hidden@example.com",
        references=tuple(f"<m{i}@example.com>" for i in range(100)) + ("junk\r\nX-Evil: value",),
    )
    message, envelope = build_message(ACCOUNT, draft)
    wire = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
    assert "Bcc" not in wire and "X-Evil" not in wire
    assert envelope == ["friend@example.com"]
    assert str(wire["In-Reply-To"]) == "<source@example.com>"
    references = str(wire["References"]).split()
    assert len(references) == 50
    assert references[0] == "<m0@example.com>"
    assert references[-1] == "<source@example.com>"


def test_missing_ancestry_does_not_invent_reply_headers():
    original = replace(ORIGINAL, message_id="", references=(), in_reply_to="")
    message, _ = build_message(ACCOUNT, response(original))
    assert "In-Reply-To" not in message and "References" not in message
