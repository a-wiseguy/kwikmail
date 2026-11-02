import json
from datetime import UTC, date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest

from kwikmail import cli
from kwikmail.attachments import extract_attachments, save_attachments
from kwikmail.backend import ImapBackend, MailboxChanged
from kwikmail.config import Account
from kwikmail.exports import INVOICE_WORDS, date_range, matches


def message(subject="hello", body="ordinary text", filename=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg.set_content(body)
    if filename:
        msg.add_attachment(
            bytes(range(256)), maintype="application", subtype="pdf", filename=filename
        )
    return msg


def invoke(capsys, *args):
    cli.main(["--demo", "--json", *args])
    output = capsys.readouterr()
    assert not output.err
    return json.loads(output.out)


@pytest.mark.parametrize(
    "args, expected",
    [
        ({"quarter": "2024-Q1"}, (date(2024, 1, 1), date(2024, 4, 1))),
        ({"quarter": "2025-Q4"}, (date(2025, 10, 1), date(2026, 1, 1))),
        ({"last": "quarter", "today": date(2026, 1, 15)}, (date(2025, 10, 1), date(2026, 1, 1))),
        ({"last": "3months", "today": date(2024, 5, 31)}, (date(2024, 2, 29), date(2024, 6, 1))),
        ({"last": "7d", "today": date(2024, 3, 1)}, (date(2024, 2, 24), date(2024, 3, 2))),
    ],
)
def test_ranges(args, expected):
    assert date_range(**args) == expected


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"quarter": "2026-Q0"},
        {"last": "0d"},
        {"since": "2026-05-02", "before": "2026-05-01"},
        {"last": "quarter", "since": "2026-01-01"},
    ],
)
def test_bad_ranges(args):
    with pytest.raises(ValueError):
        date_range(**args)


def test_mime_download_names_bytes_and_exclusive_writes(tmp_path):
    msg = message(filename="../../invoice.pdf")
    msg.add_attachment(
        b"second", maintype="application", subtype="pdf", filename="../../invoice.pdf"
    )
    msg.add_attachment(
        b"inline", maintype="image", subtype="png", filename="logo.png", disposition="inline"
    )
    files = save_attachments(msg.as_bytes(), tmp_path)
    assert [Path(f["path"]).name for f in files] == [
        "001-invoice.pdf",
        "002-invoice.pdf",
        "003-logo.png",
    ]
    assert Path(files[0]["path"]).read_bytes() == bytes(range(256))
    assert all(Path(f["path"]).stat().st_mode & 0o777 == 0o600 for f in files)
    with pytest.raises(FileExistsError):
        save_attachments(msg.as_bytes(), tmp_path)
    assert Path(files[0]["path"]).read_bytes() == bytes(range(256))


def test_attached_message_not_flattened():
    msg = message()
    msg.add_attachment(message(subject="inner", filename="inner.pdf"))
    attachments = extract_attachments(msg.as_bytes())
    assert len(attachments) == 1
    assert attachments[0].name == "attachment.eml"
    assert b"Subject: inner" in attachments[0].data


def test_attachment_or_keyword_matching_full_decoded_text():
    assert matches(message(filename="random.bin").as_bytes(), "invoices", INVOICE_WORDS)
    assert matches(message(subject="FACTUUR 42").as_bytes(), "invoices", INVOICE_WORDS)
    assert matches(message(body="x" * 90_000 + " Payment").as_bytes(), "invoices", INVOICE_WORDS)
    assert not matches(message().as_bytes(), "invoices", INVOICE_WORDS)
    assert not matches(message(subject="invoice").as_bytes(), "attachments", INVOICE_WORDS)
    assert matches(message(body="custom").as_bytes(), "invoices", ["custom"])


class Imap:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []
        self.validity = 42
        self.size = len(raw)
        self.normalise_times = True

    def select_folder(self, folder, readonly):
        assert readonly
        self.calls.append(("select", folder))
        return {b"UIDVALIDITY": self.validity}

    def search(self, criteria):
        self.calls.append(("search", criteria))
        return [7, 3]

    def fetch(self, uids, fields):
        self.calls.append(("fetch", fields))
        if fields == ["INTERNALDATE"]:
            assert not self.normalise_times
            return {3: {b"INTERNALDATE": datetime(2026, 7, 1, tzinfo=timezone(timedelta(hours=2)))}}
        if fields == ["RFC822.SIZE"]:
            return {3: {b"RFC822.SIZE": self.size}}
        assert fields[0].startswith("BODY.PEEK[]")
        return {3: {b"BODY[]<0>": self.raw}}


def test_imap_readonly_dates_validity_and_complete_download():
    backend = ImapBackend(Account("test", "test", "test@example.com"))
    client = Imap(message(filename="invoice.pdf").as_bytes())
    backend.client = client
    start, stop = date(2026, 1, 1), date(2026, 4, 1)
    assert backend.range_uids("INBOX", start, stop) == (42, [3, 7])
    assert ("search", ["SINCE", start, "BEFORE", stop]) in client.calls
    assert backend.raw("INBOX", 42, 3) == client.raw
    assert backend.received_at("INBOX", 42, 3).isoformat() == "2026-07-01T00:00:00+02:00"
    assert client.normalise_times
    with pytest.raises(MailboxChanged):
        backend.received_at("INBOX", 43, 3)
    with pytest.raises(MailboxChanged):
        backend.raw("INBOX", 43, 3)
    with pytest.raises(ValueError, match="size limit"):
        backend.raw("INBOX", 42, 3, 5)
    client.size += 1
    with pytest.raises(ValueError, match="Incomplete"):
        backend.raw("INBOX", 42, 3)


def test_cli_pages_read_download_and_export(tmp_path, capsys):
    assert invoke(capsys, "accounts")[0]["key"] == "demo"
    assert "INBOX" in invoke(capsys, "folders")
    page = invoke(capsys, "list")
    assert len(page["headers"]) == 100
    assert invoke(capsys, "read", "240", "--validity", "1")["uid"] == 240
    files = invoke(
        capsys, "attachments", "240", "--validity", "1", "--output", str(tmp_path / "files")
    )
    assert Path(files[0]["path"]).read_bytes() == b"demo invoice\n"
    report = invoke(capsys, "export", "--last", "90d", "--output", str(tmp_path / "export"))
    assert report["complete"] and len(report["messages"]) == 48
    assert (report["examined"], report["matched"], report["failed"]) == (240, 48, 0)
    assert report["messages"][0]["internal_date"]
    assert report["messages"][0]["received_at"].endswith("+00:00")
    assert report["messages"][0]["uid"] == 5  # Beyond the first page.
    manifest = Path(report["directory"]) / "manifest.json"
    assert json.loads(manifest.read_text()) == report
    assert b"Content-Type:" in Path(report["messages"][0]["eml"]).read_bytes()
    again = invoke(capsys, "export", "--last", "90d", "--output", str(tmp_path / "export"))
    assert again["directory"] != report["directory"]


@pytest.mark.parametrize(
    "args",
    [
        ("read", "1"),
        ("read", "1", "--validity", "0"),
        ("search", "--quarter", "bad"),
        ("send",),
        ("unknown",),
    ],
)
def test_json_errors(capsys, args):
    with pytest.raises(SystemExit) as error:
        cli.main(["--demo", "--json", *args])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["exit_code"] == 2


def test_draft_response_and_send(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "state_path", lambda: tmp_path)
    response = invoke(capsys, "respond", "240", "--validity", "1")
    path = tmp_path / "response.json"
    path.write_text(json.dumps(response))
    assert invoke(capsys, "draft", "import", str(path))["saved"]
    assert invoke(capsys, "draft", "show")["subject"].startswith("Re:")
    outcome = invoke(capsys, "send", "--confirm")
    assert outcome["accepted"]
    assert "Demo only" in outcome["warnings"][0]
    assert invoke(capsys, "draft", "show")["to"] == ""


def test_failed_export_preserves_incomplete_manifest(tmp_path, capsys, monkeypatch):
    original = cli.DemoBackend.raw

    def fail(self, folder, validity, uid, max_bytes):
        if uid == 10:
            raise OSError("private server detail")
        return original(self, folder, validity, uid, max_bytes)

    monkeypatch.setattr(cli.DemoBackend, "raw", fail)
    with pytest.raises(SystemExit) as error:
        cli.main(["--demo", "--json", "export", "--last", "90d", "--output", str(tmp_path)])
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "private server detail" not in output.err
    report = json.loads(output.out)
    assert not report["complete"] and len(report["messages"]) == 1
    assert (report["examined"], report["matched"], report["failed"]) == (10, 1, 1)
    manifest = json.loads((Path(report["directory"]) / "manifest.json").read_text())
    assert not manifest["complete"]
    assert manifest == report


def test_export_checkpoints_each_uid_and_resumes_without_refetch(tmp_path, capsys, monkeypatch):
    original = cli.DemoBackend.raw
    fetched = []

    def interrupted(self, folder, validity, uid, max_bytes):
        fetched.append(uid)
        manifest = next(tmp_path.glob("kwikmail-*/manifest.json"))
        checkpoint = json.loads(manifest.read_text())
        assert checkpoint["examined"] == uid - 1
        if uid == 10:
            raise OSError("private server detail")
        return original(self, folder, validity, uid, max_bytes)

    monkeypatch.setattr(cli.DemoBackend, "raw", interrupted)
    with pytest.raises(SystemExit):
        cli.main(["--demo", "--json", "export", "--last", "90d", "--output", str(tmp_path)])
    report = json.loads(capsys.readouterr().out)
    directory = Path(report["directory"])
    first = report["messages"][0]
    original_bytes = Path(first["eml"]).read_bytes()
    fetched.clear()

    def resumed(self, folder, validity, uid, max_bytes):
        fetched.append(uid)
        return original(self, folder, validity, uid, max_bytes)

    monkeypatch.setattr(cli.DemoBackend, "raw", resumed)
    resumed_report = invoke(capsys, "export", "--last", "90d", "--resume", str(directory))
    assert fetched == list(range(10, 241))
    assert resumed_report["complete"]
    assert (resumed_report["examined"], resumed_report["matched"], resumed_report["failed"]) == (
        240,
        48,
        0,
    )
    assert resumed_report["messages"][0] == first
    assert Path(first["eml"]).read_bytes() == original_bytes
    assert len({item["uid"] for item in resumed_report["messages"]}) == 48
    assert json.loads((directory / "manifest.json").read_text()) == resumed_report


def test_resume_rejects_changed_options_and_uidvalidity(tmp_path, capsys, monkeypatch):
    report = invoke(capsys, "export", "--last", "90d", "--output", str(tmp_path))
    directory = Path(report["directory"])
    with pytest.raises(SystemExit):
        cli.main(
            [
                "--demo",
                "--json",
                "export",
                "--last",
                "90d",
                "--match",
                "all",
                "--resume",
                str(directory),
            ]
        )
    assert "same accounts" in json.loads(capsys.readouterr().err)["error"]
    assert json.loads((directory / "manifest.json").read_text()) == report
    original = cli.DemoBackend.range_uids
    monkeypatch.setattr(
        cli.DemoBackend, "range_uids", lambda self, *args: (2, original(self, *args)[1])
    )
    with pytest.raises(SystemExit):
        cli.main(["--demo", "--json", "export", "--last", "90d", "--resume", str(directory)])
    output = capsys.readouterr()
    assert "UIDVALIDITY" in json.loads(output.err)["error"]
    assert not json.loads(output.out)["complete"]


def test_received_timestamp_uses_server_date_not_sender_header(tmp_path, capsys, monkeypatch):
    received = datetime(2026, 7, 1, tzinfo=timezone(timedelta(hours=2)))
    monkeypatch.setattr(cli.DemoBackend, "received_at", lambda *args: received)
    report = invoke(capsys, "export", "--last", "90d", "--output", str(tmp_path))
    item = report["messages"][0]
    assert item["internal_date"] == received.isoformat()
    assert item["received_at"] == received.astimezone(UTC).isoformat()
    assert item["date"] != item["received_at"]


def test_resume_rejects_missing_or_changed_files_before_fetch(tmp_path, capsys, monkeypatch):
    report = invoke(capsys, "export", "--last", "90d", "--output", str(tmp_path))
    path = Path(report["messages"][0]["eml"])
    path.write_bytes(b"changed")
    monkeypatch.setattr(
        cli.DemoBackend, "raw", lambda *args: pytest.fail("must validate files before fetching")
    )
    with pytest.raises(SystemExit):
        cli.main(["--demo", "--json", "export", "--last", "90d", "--resume", report["directory"]])
    output = capsys.readouterr()
    assert not output.out
    assert "missing or changed" in json.loads(output.err)["error"]
    assert json.loads((Path(report["directory"]) / "manifest.json").read_text()) == report


def test_help_does_not_require_config(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "config_path", lambda: tmp_path / "missing.toml")
    cli.main(["export", "--help"])
    assert "--quarter" in capsys.readouterr().out


def test_multiaccount_export_and_selection(tmp_path, capsys, monkeypatch):
    from kwikmail.config import AccountEntry, Settings

    settings = Settings(
        tuple(
            AccountEntry(key, key, Account("demo", key, f"{key}@example.com"), key)
            for key in ("personal", "work")
        ),
        "personal",
    )
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "ImapBackend", lambda account, **kw: cli.DemoBackend(account=account))
    cli.main(["--account", "work", "--json", "search", "--last", "90d", "--match", "invoices"])
    report = json.loads(capsys.readouterr().out)
    assert {item["account"] for item in report["messages"]} == {"work"}
    cli.main(["--json", "export", "--all-accounts", "--last", "90d", "--output", str(tmp_path)])
    report = json.loads(capsys.readouterr().out)
    assert {item["account"] for item in report["messages"]} == {"personal", "work"}
    assert len(report["messages"]) == 96
    assert len({item["eml"] for item in report["messages"]}) == 96


def test_oauth_json_final_output(capsys, monkeypatch):
    from kwikmail.config import AccountEntry, Settings
    from kwikmail.oauth import OAuthTokens

    account = Account("demo", "demo", "demo@example.com", auth="oauth2", oauth_client_id="demo")
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda path: Settings((AccountEntry("work", "Work", account, "work"),), "work"),
    )
    monkeypatch.setattr(OAuthTokens, "login", lambda self, **kw: print("provider instructions"))
    monkeypatch.setattr(OAuthTokens, "logout", lambda self: None)
    cli.main(["--json", "login", "--no-browser"])
    output = capsys.readouterr()
    assert json.loads(output.out)["authorized"]
    assert "provider instructions" in output.err
    cli.main(["--json", "logout"])
    assert json.loads(capsys.readouterr().out)["logged_out"]
