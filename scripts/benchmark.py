"""Deterministic synthetic IMAP and headless Textual performance checks."""

import argparse
import asyncio
import cProfile
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUERIES = {"backup": 0, "bakcup": 0, "zoe": 0, "café": 1, "qqqqqqqqqqqqqqqq": None}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(checkout, *args):
    return subprocess.check_output(["git", "-C", str(checkout), *args], text=True).strip()


def ignored_output(output):
    output = output.resolve()
    parent = output
    while not parent.exists():
        parent = parent.parent
    root = Path(git(parent, "rev-parse", "--show-toplevel"))
    relative = output.relative_to(root) / "probe.json"
    subprocess.run(["git", "-C", str(root), "check-ignore", "-q", str(relative)], check=True)
    output.mkdir(parents=True, exist_ok=True)
    return output


def metadata(checkout):
    lock = checkout / "uv.lock"
    installed = sorted(
        f"{item.metadata['Name']}=={item.version}" for item in importlib.metadata.distributions()
    )
    return {
        "installed_distributions_sha256": digest("\n".join(installed).encode()),
        "sha": git(checkout, "rev-parse", "HEAD"),
        "dirty": git(checkout, "status", "--short"),
        "lock_sha256": digest(lock.read_bytes()),
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "cores": os.cpu_count(),
        "load": list(os.getloadavg()),
        "versions": {
            package: importlib.metadata.version(package)
            for package in ("textual", "rich", "rapidfuzz", "imapclient", "markdownify")
        },
        "environment": {key: os.environ.get(key) for key in ("LANG", "LC_ALL", "TZ", "TERM")},
        "geometry": [150, 45],
        "local_timezone": list(time.tzname),
        "page_size": 100,
        "cache_policy": "application defaults; clear before cold body measurement",
    }


def raw_header(uid, size):
    subject = (
        "Backup verification"
        if uid == size
        else "Café rendezvous"
        if uid == size - 1
        else f"Fixture message {uid:06d}"
    )
    sender = "Zoe <zoe@fixture.invalid>" if uid == size else "Fixture <mail@fixture.invalid>"
    return (
        f"From: {sender}\r\nTo: reader@fixture.invalid\r\nSubject: {subject}\r\n"
        "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        f"Message-ID: <fixture-{uid}@fixture.invalid>\r\n\r\n"
    ).encode()


def raw_bodies():
    prefix = (
        b"From: writer@fixture.invalid\r\nTo: reader@fixture.invalid\r\n"
        b"Subject: Body fixture\r\nMIME-Version: 1.0\r\n"
    )
    plain = (
        prefix + b"Content-Type: text/plain; charset=utf-8\r\n\r\n# Fixture\r\n\r\nbody-marker\r\n"
    )
    html = (
        prefix + b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<h1>Fixture</h1><p>body-marker</p><script>secret-script</script>"
    )
    nested = (
        prefix + b'Content-Type: multipart/mixed; boundary="outer"\r\n\r\n--outer\r\n'
        b'Content-Type: multipart/alternative; boundary="inner"\r\n\r\n--inner\r\n'
        b"Content-Type: text/plain\r\n\r\nbody-marker\r\n--inner--\r\n--outer\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="fixture.bin"\r\n\r\nbytes\r\n'
        b"--outer--\r\n"
    )
    large = prefix + b"Content-Type: text/plain\r\n\r\nbody-marker " + b"a" * 90_000
    return {"plain": plain, "html": html, "nested": nested, "large": large}


class FixtureClient:
    def __init__(self, size, delay=0):
        self.size = size
        self.headers = {uid: raw_header(uid, size) for uid in range(1, size + 1)}
        self.payloads = raw_bodies()
        self.counts = Counter()
        self.delay = delay
        self.body_started = threading.Event()
        self.body_release = None
        self.use_uid = True

    def list_folders(self):
        self.counts["commands"] += 1
        return [((), "/", "INBOX")]

    def select_folder(self, folder, readonly=True):
        assert folder == "INBOX" and readonly
        self.counts["commands"] += 1
        return {b"EXISTS": self.size, b"UIDVALIDITY": 1}

    def folder_status(self, folder, fields):
        assert folder == "INBOX"
        self.counts["commands"] += 1
        values = {
            b"MESSAGES": self.size,
            b"UNSEEN": self.size,
            b"UIDVALIDITY": 1,
            b"UIDNEXT": self.size + 1,
        }
        return {key: values[key] for key in (field.encode() for field in fields)}

    def fetch(self, selected, fields):
        self.counts["commands"] += 1
        if isinstance(selected, str):
            assert not self.use_uid, "header fetch must use a bounded sequence range"
            start, end = map(int, selected.split(":"))
            self.counts["headers"] += end - start + 1
            rows = {}
            header_field = next(field for field in fields if field.startswith("BODY.PEEK"))
            response_field = header_field.replace("BODY.PEEK", "BODY").encode()
            for uid in range(start, end + 1):
                raw = self.headers[uid]
                self.counts["bytes"] += len(raw)
                rows[uid] = {
                    b"UID": uid,
                    response_field: raw,
                    b"FLAGS": (),
                    b"RFC822.SIZE": len(raw),
                }
            return rows
        assert len(selected) == 1
        uid = selected[0]
        self.body_started.set()
        if self.body_release is not None:
            assert self.body_release.wait(15), "blocked fixture body was not released"
        if self.delay:
            time.sleep(self.delay)
        raw = self.payloads[list(self.payloads)[(uid - 1) % 4]]
        subject = self.headers[uid].split(b"Subject: ", 1)[1].split(b"\r\n", 1)[0]
        raw = raw.replace(b"Subject: Body fixture", b"Subject: " + subject)
        self.counts["bodies"] += 1
        self.counts["bytes"] += len(raw)
        return {uid: {b"BODY[]": raw, b"RFC822.SIZE": len(raw)}}

    def shutdown(self):
        pass


async def until(predicate, timeout=15):
    deadline = time.perf_counter() + timeout
    while not predicate():
        if time.perf_counter() >= deadline:
            raise TimeoutError("completion condition not reached")
        await asyncio.sleep(0.001)


async def frame(app):
    done = asyncio.get_running_loop().create_future()
    app.refresh()
    app.call_after_refresh(lambda: done.set_result(None) if not done.done() else None)
    await asyncio.wait_for(done, 15)


def viewport(app):
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


def thread_header(uid, size):
    root = ((uid - 1) // 10) * 10 + 1
    raw = raw_header(uid, size)
    subject = "zqxvplmn" if uid == size - 1 else f"Conversation {root:06d} message {uid:06d}"
    before, rest = raw.split(b"Subject: ", 1)
    _, after = rest.split(b"\r\n", 1)
    raw = before + b"Subject: " + subject.encode() + b"\r\n" + after
    if uid != root:
        ancestry = f"References: <fixture-{root}@fixture.invalid>\r\n".encode()
        raw = raw.replace(b"\r\n\r\n", b"\r\n" + ancestry + b"\r\n")
    return raw


class Recorder:
    def __init__(self):
        self.results = []

    async def measure(self, name, operation, counts=None, **details):
        before = counts.copy() if counts is not None else Counter()
        wall, cpu = time.perf_counter(), time.process_time()
        result = {"name": name, "retries": 0, "timeouts": 0, **details}
        try:
            work = await operation()
            result.update(status="ok", work=work)
        except Exception as error:
            result.update(
                status="failed",
                error=f"{type(error).__name__}: {error}",
                work={},
                timeouts=int(isinstance(error, TimeoutError)),
            )
        result.update(
            ms=(time.perf_counter() - wall) * 1000,
            cpu_ms=(time.process_time() - cpu) * 1000,
            counts=dict(counts - before) if counts is not None else {},
        )
        self.results.append(result)
        return result


async def suite(size, delays):
    from rapidfuzz import fuzz, process
    from textual.widgets import Markdown, Static

    from kwikmail.app import MailApp
    from kwikmail.backend import ImapBackend
    from kwikmail.config import Account
    from kwikmail.mail import parse_body, parse_header
    from kwikmail.storage import DraftStore

    class Backend(ImapBackend):
        def connection(self):
            return client

        def close(self):
            pass

        def drain(self):
            pass

    client = FixtureClient(size)
    backend = Backend(Account("fixture.invalid", "reader", "reader@fixture.invalid"))
    record = Recorder()
    with tempfile.TemporaryDirectory() as tmp:
        drafts = DraftStore(Path(tmp) / "draft.json")
        session = None
        if importlib.util.find_spec("kwikmail.session") is not None:
            from kwikmail.models import MessageRef
            from kwikmail.session import AccountSession

            session = AccountSession("fixture", "Fixture", backend, drafts)
            app = MailApp([session], "fixture")
        else:
            app = MailApp(backend, drafts)

        async def mail_call(method, *args):
            if session is None:
                return await app.imap(method, *args)
            if method == "drain":
                return await session.folders()
            if method == "body":
                return await session.body(MessageRef(session.key, *args))
            raise ValueError(f"Unsupported benchmark operation: {method}")

        def invalidate_preview():
            if session is not None:
                app.invalidate_selection()
            else:
                if app.preview_timer:
                    app.preview_timer.stop()
                app.preview_serial += 1
                app.workers.cancel_group(app, "preview")

        started = time.perf_counter()
        startup_cpu = time.process_time()
        async with app.run_test(size=(150, 45)) as pilot:

            async def startup():
                await until(
                    lambda: hasattr(app, "table") and not app.loading and app.table.row_count == 100
                )
                await frame(app)
                assert [row.uid for row in app.headers] == list(range(size, size - 100, -1))
                assert client.counts["headers"] == 100 and app.total == size
                assert app.table.get_row_at(0)[2].plain == "Backup verification"
                assert "Backup verification" in viewport(app)
                return {"headers": 100, "visible_rows": min(100, app.table.size.height - 1)}

            result = await record.measure("headers.first_page", startup, client.counts, size=size)
            result["ms"] = (time.perf_counter() - started) * 1000
            result["cpu_ms"] = (time.process_time() - startup_cpu) * 1000
            result["counts"] = dict(client.counts)
            if result["status"] != "ok":
                return record.results
            if size > 100:

                async def paging():
                    app.action_more()
                    await until(lambda: not app.loading and app.table.row_count == 200)
                    await frame(app)
                    assert [row.uid for row in app.headers] == list(range(size, size - 200, -1))
                    return {"headers": 200, "new_headers": 100}

                await record.measure("headers.next_page", paging, client.counts, size=size)

            invalidate_preview()
            await mail_call("drain")
            app.headers = [parse_header(uid, client.headers[uid], ()) for uid in range(size, 0, -1)]
            app.filter_query = "backup"
            app.render_rows()
            await frame(app)
            app.action_search()
            await frame(app)
            render_count = [0]
            original_render = app.render_rows

            def observed_render(*args, **kwargs):
                original_render(*args, **kwargs)
                render_count[0] += 1

            app.render_rows = observed_render
            for query, offset in QUERIES.items():
                expected = None if offset is None else size - offset

                def check_rows(expected=expected):
                    assert app.table.row_count == len(app.filtered_headers)
                    if expected is None:
                        assert not app.filtered_headers
                    else:
                        assert app.filtered_headers[0].uid == expected
                        assert app.table.get_row_at(0)[2].plain == app.filtered_headers[0].subject
                        assert app.filtered_headers[0].subject in viewport(app)
                    return {
                        "headers_scored": size,
                        "rows": app.table.row_count,
                        "top_uid": expected,
                    }

                async def extract(query=query, expected=expected):
                    choices = {header.uid: header.searchable for header in app.headers}
                    ranked = process.extract(
                        query, choices, scorer=fuzz.WRatio, limit=500, score_cutoff=40
                    )
                    assert (ranked[0][2] if ranked else None) == expected
                    return {"headers_scored": size, "matches": len(ranked)}

                await record.measure("fuzzy.extract", extract, size=size, query=query)

                app.search_input.value = ""
                await until(lambda: app.filter_query == "")
                if app.filter_timer:
                    app.filter_timer.stop()

                async def search(query=query, expected=expected, check_rows=check_rows):
                    previous = render_count[0]
                    app.search_input.value = query
                    await until(lambda: app.filter_query == query and render_count[0] > previous)
                    await frame(app)
                    return check_rows()

                await record.measure("fuzzy.input_to_frame", search, size=size, query=query)

                async def render(check_rows=check_rows):
                    app.render_rows()
                    await frame(app)
                    return check_rows()

                await record.measure("fuzzy.render", render, size=size, query=query)

            invalidate_preview()
            await mail_call("drain")
            await until(
                lambda: (
                    not any(worker.group in ("preview", "clear-preview") for worker in app.workers)
                )
            )
            await frame(app)
            for kind, raw in client.payloads.items():

                async def parse(raw=raw, kind=kind):
                    body = parse_body(1, raw)
                    assert "body-marker" in body.markdown and "secret-script" not in body.markdown
                    assert body.truncated == (kind == "large")
                    if kind == "nested":
                        assert body.attachments == ("fixture.bin",)
                    return {
                        "bytes_parsed": len(raw),
                        "markdown_chars": len(body.markdown),
                        "truncated": body.truncated,
                    }

                await record.measure("mime.parse", parse, kind=kind, size=size)
                body = parse_body(1, raw)

                async def body_render(body=body):
                    pending = sum(
                        worker.group in ("preview", "clear-preview") for worker in app.workers
                    )
                    assert pending == 0, "body rendering overlaps pending preview work"
                    await app.preview.show_body(body)
                    await frame(app)
                    assert "body-marker" in app.preview.query_one(Markdown)._markdown
                    assert "body-marker" in viewport(app)
                    return {
                        "markdown_chars": len(body.markdown),
                        "rendered_blocks": len(app.preview.query_one(Markdown).children),
                        "pending_preview_workers": pending,
                    }

                await record.measure("body.render", body_render, kind=kind, size=size)

            invalidate_preview()
            await mail_call("drain")
            backend.bodies.clear()

            async def fetch_render(cached):
                before = client.counts.copy()
                body = await mail_call("body", "INBOX", 1, 1)
                await app.preview.show_body(body)
                await frame(app)
                assert "body-marker" in app.preview.query_one(Markdown)._markdown
                assert "body-marker" in viewport(app)
                assert client.counts["bodies"] - before["bodies"] == (0 if cached else 1)
                return {
                    "bodies_rendered": 1,
                    "cache_hit": cached,
                    "markdown_chars": len(body.markdown),
                }

            await record.measure(
                "body.cold_to_frame", lambda: fetch_render(False), client.counts, size=size
            )
            await record.measure(
                "body.cached_to_frame", lambda: fetch_render(True), client.counts, size=size
            )

            async def eviction():
                backend.bodies.clear()
                for uid in range(1, 26):
                    await mail_call("body", "INBOX", 1, uid)
                before = client.counts["bodies"]
                body = await mail_call("body", "INBOX", 1, 1)
                assert "body-marker" in body.markdown
                assert client.counts["bodies"] == before + 1
                return {"unique_bodies": 25, "evicted_refetches": 1}

            await record.measure("body.cache_eviction", eviction, client.counts, size=size)
            app.filter_query = ""
            app.headers = app.headers[:100]
            app.search_input.value = ""
            app.search_input.display = False
            app.render_rows()
            app.table.focus()
            await frame(app)
            if app.preview_timer:
                app.preview_timer.stop()
            await mail_call("drain")
            backend.bodies.clear()
            for delay_ms in delays:
                backend.bodies.clear()
                client.delay = delay_ms / 1000
                client.body_started.clear()
                pending = asyncio.create_task(mail_call("body", "INBOX", 1, 2))
                await until(client.body_started.is_set)
                gaps = []
                stopped = asyncio.Event()

                async def heartbeat(gaps=gaps, stopped=stopped):
                    previous = time.perf_counter()
                    while not stopped.is_set():
                        await asyncio.sleep(0.005)
                        now = time.perf_counter()
                        gaps.append((now - previous) * 1000)
                        previous = now

                task = asyncio.create_task(heartbeat())
                app.table.move_cursor(row=0)
                await frame(app)
                pending_at_dispatch = not pending.done()

                async def navigation(pending_at_dispatch=pending_at_dispatch, pending=pending):
                    await pilot.press("down", "down", "down")
                    await frame(app)
                    assert app.table.cursor_row == 3 and app.selected().uid == size - 3
                    return {
                        "keys": 3,
                        "selected_uid": app.selected().uid,
                        "body_pending_at_dispatch": pending_at_dispatch,
                        "body_pending_at_frame": not pending.done(),
                    }

                result = await record.measure(
                    "network.navigation_to_frame",
                    navigation,
                    client.counts,
                    delay_ms=delay_ms,
                    size=size,
                )
                try:
                    await pending
                    await until(
                        lambda: (
                            app.selected().subject in str(app.preview.query_one(Static).render())
                        )
                    )
                    await frame(app)
                    result["work"]["latest_preview_verified"] = True
                except Exception as error:
                    result.update(
                        status="failed",
                        error=f"latest preview: {type(error).__name__}: {error}",
                        timeouts=int(isinstance(error, TimeoutError)),
                    )
                finally:
                    stopped.set()
                    await task
                result["heartbeat_max_gap_ms"] = max(gaps, default=0)
                result["heartbeat_ticks"] = len(gaps)
                client.delay = 0
                invalidate_preview()
                await mail_call("drain")
    if session is not None:
        record.results.extend(await integrated_suite(size))
    return record.results


async def integrated_suite(size):
    from kwikmail.app import MailApp
    from kwikmail.backend import ImapBackend
    from kwikmail.config import Account
    from kwikmail.mail import parse_header
    from kwikmail.models import MessageRef
    from kwikmail.session import AccountSession
    from kwikmail.storage import DraftStore
    from kwikmail.threads import group_threads

    class Backend(ImapBackend):
        def __init__(self, client, address):
            super().__init__(Account("fixture.invalid", "reader", address))
            self.fixture = client

        def connection(self):
            return self.fixture

        def close(self):
            pass

    primary, secondary = FixtureClient(size), FixtureClient(size)
    secondary.headers = {
        uid: raw.replace(b"Subject: ", b"Subject: Secondary ")
        for uid, raw in secondary.headers.items()
    }
    record = Recorder()
    with tempfile.TemporaryDirectory() as tmp:
        sessions = [
            AccountSession(
                key,
                key.title(),
                Backend(client, f"{key}@fixture.invalid"),
                DraftStore(Path(tmp) / f"{key}.json"),
            )
            for key, client in (("primary", primary), ("secondary", secondary))
        ]
        app = MailApp(sessions, "primary")
        async with app.run_test(size=(150, 45)):
            await until(
                lambda: hasattr(app, "table") and not app.loading and app.table.row_count == 100
            )
            await frame(app)

            async def switched(key, subject):
                app.switch_account(key)
                await until(
                    lambda: app.active_key == key and not app.loading and app.table.row_count == 100
                )
                await frame(app)
                assert [row.uid for row in app.headers] == list(range(size, size - 100, -1))
                assert app.selected().subject == subject
                assert subject in viewport(app)
                assert f"{key}@fixture.invalid" in viewport(app)
                assert not app.filter_query and not app.server_query
                assert app.next_end == size - 100
                return {
                    "accounts": 2,
                    "headers": 100,
                    "active_account": key,
                    "selected_uid": app.selected().uid,
                    "overlapping_uids": 100,
                }

            app.filter_query = "backup"
            app.render_rows()
            await record.measure(
                "account.switch_to_frame",
                lambda: switched("secondary", "Secondary Backup verification"),
                secondary.counts,
                size=size,
            )
            await switched("primary", "Backup verification")
            app.invalidate_selection()
            await sessions[0].folders()
            sessions[0].backend.bodies.clear()
            primary.body_started.clear()
            primary.body_release = threading.Event()
            try:
                ref = MessageRef("primary", "INBOX", 1, size)
                app.load_preview(app.ticket("preview"), ref)
                await until(primary.body_started.is_set)

                async def isolated():
                    work = await switched("secondary", "Secondary Backup verification")
                    assert not primary.body_release.is_set()
                    assert primary.counts["bodies"] == before
                    await until(
                        lambda: (
                            "Secondary Backup verification"
                            in str(app.preview.query_one("Static").render())
                        )
                    )
                    await frame(app)
                    assert "Secondary Backup verification" in viewport(app)
                    return {
                        **work,
                        "blocked_account": "primary",
                        "old_body_pending_at_frame": primary.counts["bodies"] == before,
                        "new_account_preview_verified": True,
                    }

                before = primary.counts["bodies"]
                result = await record.measure(
                    "account.slow_isolation_to_frame", isolated, secondary.counts, size=size
                )
                primary.body_release.set()
                try:
                    await sessions[0].folders()
                    await frame(app)
                    assert primary.counts["bodies"] == before + 1
                    assert app.active_key == "secondary"
                    assert "Secondary Backup verification" in str(
                        app.preview.query_one("Static").render()
                    )
                    result["work"]["late_old_body_ignored"] = True
                except Exception as error:
                    result.update(
                        status="failed", error=f"late body: {type(error).__name__}: {error}"
                    )
            finally:
                primary.body_release.set()

            app.invalidate_selection()
            await sessions[1].folders()
            headers = [
                parse_header(uid, thread_header(uid, size), ()) for uid in range(size, 0, -1)
            ]

            groups_count = (size + 9) // 10
            top_root = ((size - 1) // 10) * 10 + 1
            top_count = size - top_root + 1

            async def grouping():
                groups = group_threads(headers)
                assert len(groups) == groups_count
                assert sorted(h.uid for group in groups for h in group.members) == list(
                    range(1, size + 1)
                )
                assert all(1 <= len(group.members) <= 10 for group in groups)
                return {
                    "headers_grouped": size,
                    "conversations": len(groups),
                    "maximum_messages_per_conversation": 10,
                }

            await record.measure("conversation.group", grouping, size=size)
            app.headers = headers
            app.grouping = False
            app.filter_query = ""
            app.render_rows()
            await frame(app)

            async def group_render():
                app.action_threads()
                await frame(app)
                assert app.grouping and app.table.row_count == groups_count
                assert len(app.thread_rows) == groups_count
                assert app.thread_rows[0].count == top_count
                assert app.thread_rows[0].unread == top_count
                assert f"Conversation {top_root:06d}" in viewport(app)
                return {"headers_grouped": size, "rows": app.table.row_count}

            await record.measure("conversation.group_to_frame", group_render, size=size)

            async def expand():
                app.table.move_cursor(row=0)
                app.action_expand()
                await frame(app)
                assert app.table.row_count == groups_count + top_count - 1
                assert [row.header.uid for row in app.thread_rows[:top_count]] == list(
                    range(size, top_root - 1, -1)
                )
                assert app.selected().uid == size
                assert f"Conversation {top_root:06d}" in viewport(app)
                return {
                    "expanded_members": top_count,
                    "rows": app.table.row_count,
                    "selected_uid": size,
                }

            await record.measure("conversation.expand_to_frame", expand, size=size)
            app.action_expand()
            app.action_search()
            await frame(app)

            async def filter_thread():
                app.search_input.value = "zqxvplmn"
                await until(lambda: app.filter_query == "zqxvplmn" and app.table.row_count == 1)
                await frame(app)
                assert [h.uid for h in app.filtered_headers] == [size - 1]
                members = min(10, size - ((size - 2) // 10) * 10)
                assert app.thread_rows[0].count == members
                assert app.selected().uid == size - 1
                assert "zqxvplmn" in viewport(app)
                return {
                    "headers_scored": size,
                    "rows": 1,
                    "matching_uid": size - 1,
                    "conversation_members": members,
                }

            await record.measure("conversation.filter_to_frame", filter_thread, size=size)
    return record.results


def summarize(rounds):
    grouped = {}
    for run in rounds:
        for result in run.get("results", []):
            key = json.dumps(
                {
                    key: result[key]
                    for key in ("name", "size", "query", "kind", "delay_ms")
                    if key in result
                },
                sort_keys=True,
            )
            grouped.setdefault(key, []).append(result)
    summary = []
    for key, values in grouped.items():
        successful = [value["ms"] for value in values if value["status"] == "ok"]
        summary.append(
            {
                **json.loads(key),
                "samples": len(successful),
                "failures": len(values) - len(successful),
                "median_ms": statistics.median(successful) if successful else None,
                "min_ms": min(successful) if successful else None,
                "max_ms": max(successful) if successful else None,
            }
        )
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, action="append")
    parser.add_argument("--output", type=Path, default=ROOT / ".agent-work/benchmark")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--delay-ms", type=int, nargs="+", default=[0, 50, 300])
    parser.add_argument("--smoke", action="store_true", help="one round; no performance conclusion")
    parser.add_argument(
        "--profile", action="store_true", help="separate profiling run; no timing summary"
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (
        args.samples < 5
        or any(size < 100 for size in args.sizes)
        or any(delay < 0 for delay in args.delay_ms)
    ):
        parser.error("samples must be >=5, sizes >=100 and delay >=0")
    if args.worker:
        checkout = args.checkout[0].resolve()
        sys.path.insert(0, str(checkout / "src"))
        import kwikmail

        assert Path(kwikmail.__file__).resolve().is_relative_to(checkout / "src")
        environment = metadata(checkout)
        profiler = cProfile.Profile() if args.profile else None
        if profiler:
            profiler.enable()
        results = []
        for size in args.sizes:
            results.extend(asyncio.run(suite(size, args.delay_ms)))
        if profiler:
            profiler.disable()
            profiler.dump_stats(args.output)
        environment["load_after"] = list(os.getloadavg())
        print(json.dumps({"metadata": environment, "results": results}))
        return int(any(result["status"] != "ok" for result in results))
    output = ignored_output(args.output)
    checkouts = [path.resolve() for path in args.checkout or [ROOT]]
    locks = {digest((path / "uv.lock").read_bytes()) for path in checkouts}
    if len(locks) != 1:
        parser.error("checkouts must share uv.lock; use fixed dependencies before comparing")
    rounds = [[] for _ in checkouts]
    failed = False
    for sample in range(1 if args.smoke or args.profile else args.samples):
        for index, checkout in enumerate(checkouts):
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--checkout",
                str(checkout),
                "--sizes",
                *map(str, args.sizes),
                "--delay-ms",
                *map(str, args.delay_ms),
            ]
            if args.profile:
                command += ["--profile", "--output", str(output / f"profile-{index}.prof")]
            process = subprocess.run(command, text=True, capture_output=True)
            (output / f"run-{sample}-{index}.stderr").write_text(process.stderr)
            try:
                result = json.loads(process.stdout)
            except json.JSONDecodeError:
                result = {
                    "failure": "worker failed before producing results",
                    "returncode": process.returncode,
                }
            result.update(sample=sample, target=index, returncode=process.returncode)
            rounds[index].append(result)
            (output / f"run-{sample}-{index}.json").write_text(json.dumps(result, indent=2) + "\n")
            failed |= process.returncode != 0
            print(
                f"round={sample + 1} target={index} "
                f"status={'failed' if process.returncode else 'ok'}",
                flush=True,
            )
    report = {
        "schema": 2,
        "configuration": {"sizes": args.sizes, "delays_ms": args.delay_ms, "samples": args.samples},
        "mode": "profile" if args.profile else "smoke" if args.smoke else "timing",
        "verdict": (
            "inconclusive; inspect correctness, variation, tuning and separate profiles "
            "before claiming a difference"
        ),
        "scope": (
            "synthetic IMAPClient responses; actual parsing/cache/app; "
            "headless viewport refresh; no TLS or real terminal"
        ),
        "fixture_sha256": digest(
            b"".join(
                str(size).encode() + raw_header(uid, size)
                for size in args.sizes
                for uid in range(1, size + 1)
            )
            + b"".join(raw_bodies().values())
        ),
        "thread_fixture_sha256": digest(
            b"".join(thread_header(uid, size) for size in args.sizes for uid in range(1, size + 1))
        ),
        "harness_sha256": digest(Path(__file__).read_bytes()),
        "sample_order": "round-robin checkout order",
        "unsupported": {
            "legacy_accounts_and_threads": "new-capability scenarios run only on integrated UI",
            "tls": "run separate local TLS protocol tests",
        },
        "worker_failures": sum(
            run.get("returncode", 0) != 0 for target in rounds for run in target
        ),
        "failures": sum(
            result["status"] != "ok"
            for target in rounds
            for run in target
            for result in run.get("results", [])
        ),
        "targets": [
            {
                "checkout": str(checkout),
                "runs": runs,
                "summary": [] if args.profile else summarize(runs),
            }
            for checkout, runs in zip(checkouts, rounds, strict=True)
        ],
    }
    (output / "benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
