from email.message import EmailMessage

from markdown_it import MarkdownIt

from kwikmail.mail import parse_body


def html_body(html):
    message = EmailMessage()
    message.set_content(html, subtype="html")
    return parse_body(1, message.as_bytes()).markdown


def test_newsletter_layout_reads_in_order_without_table_columns():
    spacers = '<td width="1">&nbsp;</td>' * 30
    result = html_body(
        '<table role="presentation"><tr>'
        + spacers
        + "<td><table><tr><td><h1>Network maintenance</h1></td></tr>"
        "<tr><td><p>Work starts at 09:00.</p><p>Stay connected.</p></td></tr></table></td>"
        '<td><a href="https://example.com/details">Read details</a></td>'
        "</tr></table>"
    )
    assert "# Network maintenance" in result
    assert "Work starts at 09:00." in result and "Stay connected." in result
    assert "[Read details](https://example.com/details)" in result
    assert result.index("Network maintenance") < result.index("Work starts") < result.index("Read")
    assert not any(t.type == "table_open" for t in MarkdownIt("gfm-like").parse(result))
    assert len(result) < 250


def test_html_removes_hidden_content_and_keeps_image_descriptions():
    result = html_body(
        "<head><title>Internal title</title><style>.x { color: red; }</style></head>"
        '<div style="display: none !important">Hidden preview</div>'
        "<div hidden><p>Hidden subtree</p></div>"
        '<div style="visibility:hidden">Invisible text</div>'
        '<div style="mso-hide:all">Outlook preview</div>'
        "<script>evil()</script><iframe>Embedded page</iframe>"
        '<p>Hello &amp; welcome</p><img src="cid:logo" alt="Company logo">'
        '<a href="https://example.com"><img src="https://example.com/banner" '
        'alt="See your invoice"></a><img width="1" height="1" '
        'src="https://tracker.invalid/pixel" alt="tracking">'
    )
    assert "Hello & welcome" in result
    assert "Company logo" in result
    assert "[See your invoice](https://example.com)" in result
    for excluded in (
        "Hidden",
        "Invisible",
        "Outlook",
        "Internal",
        "evil",
        "Embedded",
        "tracker",
        "tracking",
        "banner",
        "cid:",
    ):
        assert excluded not in result


def test_data_table_keeps_headers_and_values_without_a_wide_grid():
    result = html_body(
        "<table><tr><th>Service</th><th>Status</th></tr>"
        "<tr><td>Internet</td><td>Available</td></tr></table>"
        "<ul><li>First step</li><li>Second step</li></ul>"
    )
    assert all(text in result for text in ("Service", "Status", "Internet", "Available"))
    assert "|" not in result
    assert "* First step" in result and "* Second step" in result


def test_unsafe_links_keep_their_visible_text():
    result = html_body(
        '<a href="javascript:alert(1)">Unsafe action</a>'
        '<a href="file:///etc/passwd">Local file</a>'
        '<a href="https://example.com/help">Help</a>'
        '<a href="mailto:support@example.com">Email support</a>'
    )
    assert "Unsafe action" in result and "Local file" in result
    assert "javascript:" not in result and "file:" not in result
    assert "[Help](https://example.com/help)" in result
    assert "[Email support](mailto:support@example.com)" in result


async def test_newsletter_wraps_in_preview_and_reader(tmp_path):
    from textual.widgets import Markdown

    from kwikmail.app import MailApp
    from kwikmail.demo import DemoBackend
    from kwikmail.session import AccountSession
    from kwikmail.storage import DraftStore
    from kwikmail.widgets import ReaderScreen

    message = EmailMessage()
    message.set_content(
        "<table><tr>"
        + "<td>&nbsp;</td>" * 30
        + "<td><h1>Network maintenance</h1><p>"
        + "Your internet connection remains available during the maintenance window. " * 4
        + '</p><a href="https://example.com/help">Contact support</a></td></tr></table>',
        subtype="html",
    )
    body = parse_body(1, message.as_bytes())
    app = MailApp(
        [AccountSession("demo", "Demo", DemoBackend(), DraftStore(tmp_path / "draft.json"))],
        "demo",
    )
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        app.invalidate_selection()
        await app.preview.show_body(body)
        await pilot.pause()
        preview = app.preview.query_one(Markdown)
        assert not preview.query("MarkdownTable")
        assert preview.virtual_size.width <= preview.size.width
        assert preview.size.height < 35
        await app.push_screen(ReaderScreen(body))
        await pilot.pause()
        await app.workers.wait_for_complete()
        for width in (150, 80, 40):
            await pilot.resize_terminal(width, 40)
            await pilot.pause()
            reader = app.screen.query_one(Markdown)
            assert not reader.query("MarkdownTable")
            assert reader.virtual_size.width <= reader.size.width
            assert reader.size.height < 65
            assert "Network maintenance" in str(reader.query_one("MarkdownH1").render())
