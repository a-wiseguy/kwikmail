from dataclasses import replace
from datetime import UTC, datetime
from random import Random

import pytest

from kwikmeel.models import MailHeader
from kwikmeel.threads import Conversation, flatten_threads, group_threads


def header(uid, message_id="", references=(), in_reply_to="", seen=False):
    return MailHeader(
        uid,
        "same subject",
        "sender@example.com",
        None,
        seen,
        message_id=message_id,
        references=references,
        in_reply_to=in_reply_to,
    )


def uids(groups):
    return tuple(tuple(member.uid for member in group.members) for group in groups)


def test_empty_mailbox_and_filter():
    assert group_threads([]) == ()
    assert flatten_threads((), set()) == ()
    assert flatten_threads(group_threads([header(1)]), set(), set()) == ()


def test_chain_branches_and_group_order_use_uids_not_dates():
    headers = [
        header(1, "<root@example.com>"),
        replace(
            header(4, "<reply@example.com>", ("<root@example.com>",)),
            date=datetime(2000, 1, 1, tzinfo=UTC),
        ),
        header(3, "<branch@example.com>", in_reply_to="<root@example.com>"),
        header(6, "<separate@example.com>"),
        header(5, "<tail@example.com>", in_reply_to="<reply@example.com>"),
    ]
    groups = group_threads(headers)
    assert uids(groups) == ((6,), (5, 4, 3, 1))
    assert groups[1].representative.uid == 5
    assert groups[1].unread == 4


def test_subjects_never_merge_unrelated_messages():
    assert uids(group_threads([header(1), header(2), header(3, "<id@example.com>")])) == (
        (3,),
        (2,),
        (1,),
    )


def test_duplicate_message_ids_preserve_distinct_uids():
    groups = group_threads(
        [
            header(1, "<duplicate@example.com>"),
            header(2, "<duplicate@example.com>", seen=True),
            header(3, "<reply@example.com>", in_reply_to="<duplicate@example.com>"),
        ]
    )
    assert uids(groups) == ((3, 2, 1),)
    assert groups[0].unread == 2


def test_headers_without_own_ids_join_shared_unloaded_ancestry():
    assert uids(
        group_threads(
            [
                header(1, references=("<unloaded@example.com>",)),
                header(2, in_reply_to="<unloaded@example.com>"),
                header(3, "<child@example.com>", ("<unloaded@example.com>",)),
                header(4),
            ]
        )
    ) == ((4,), (3, 2, 1))


def test_cycles_and_self_references_terminate_without_message_loss():
    assert uids(
        group_threads(
            [
                header(1, "<a@example.com>", in_reply_to="<c@example.com>"),
                header(2, "<b@example.com>", ("<a@example.com>",)),
                header(3, "<c@example.com>", ("<b@example.com>", "<c@example.com>")),
            ]
        )
    ) == ((3, 2, 1),)


@pytest.mark.parametrize(
    "invalid",
    [
        "missing brackets",
        "<no-at>",
        "<bad id@example.com>",
        "<naïve@example.com>",
        "<" + "a" * 255 + "@x>",
    ],
)
def test_invalid_ids_do_not_merge_messages(invalid):
    assert uids(
        group_threads(
            [
                header(1, invalid),
                header(2, invalid, (invalid,), invalid),
            ]
        )
    ) == ((2,), (1,))


def test_reference_parser_bounds_keep_root_and_latest_ancestor():
    references = tuple(f"<id-{index}@example.com>" for index in range(100))
    groups = group_threads(
        [
            header(1, references[0]),
            header(2, references[1]),
            header(3, references[-1]),
            header(4, references=references),
        ]
    )
    assert uids(groups) == ((4, 3, 1), (2,))


def test_keys_and_results_are_independent_of_input_order():
    headers = [
        header(1, "<z@example.com>", ("<missing@example.com>",)),
        header(2, "<b@example.com>", in_reply_to="<z@example.com>"),
        header(4),
        header(3, "<a@example.com>"),
    ]
    expected = group_threads(headers)
    for _ in range(20):
        Random(_).shuffle(headers)
        assert group_threads(headers) == expected
    assert len({group.key for group in expected}) == 3


def test_pagination_can_merge_previously_separate_groups():
    first = [header(9, "<a@example.com>"), header(8, "<b@example.com>")]
    assert uids(group_threads(first)) == ((9,), (8,))
    bridge = header(1, "<parent@example.com>", ("<a@example.com>", "<b@example.com>"))
    assert uids(group_threads(first + [bridge])) == ((9, 8, 1),)


def test_repeated_uid_uses_last_snapshot_once():
    original = header(2, "<a@example.com>")
    groups = group_threads([original, header(1), replace(original, seen=True)])
    assert uids(groups) == ((2,), (1,))
    assert groups[0].unread == 0


def test_collapsed_and_expanded_rows_keep_loaded_counts_without_duplicates():
    groups = group_threads(
        [
            header(1, "<root@example.com>"),
            header(2, "<reply@example.com>", ("<root@example.com>",), seen=True),
            header(3, "<last@example.com>", ("<root@example.com>",)),
            header(4),
        ]
    )
    collapsed = flatten_threads(groups, set())
    assert [
        (row.header.uid, row.depth, row.count, row.unread, row.expanded) for row in collapsed
    ] == [(4, 0, 1, 1, False), (3, 0, 3, 2, False)]
    expanded = flatten_threads(groups, {groups[1].key})
    assert [
        (row.header.uid, row.depth, row.count, row.unread, row.expanded) for row in expanded
    ] == [
        (4, 0, 1, 1, False),
        (3, 0, 3, 2, True),
        (2, 1, 3, 2, True),
        (1, 1, 3, 2, True),
    ]
    assert [row.thread_key for row in expanded[1:]] == [groups[1].key] * 3


def test_filter_uses_newest_matching_member_with_full_group_counts():
    groups = group_threads(
        [
            header(1, "<root@example.com>"),
            header(2, "<reply@example.com>", ("<root@example.com>",), seen=True),
            header(3, "<last@example.com>", ("<root@example.com>",)),
            header(4),
        ]
    )
    rows = flatten_threads(groups, set(), {1, 2})
    assert [(row.header.uid, row.count, row.unread) for row in rows] == [(2, 3, 2)]
    rows = flatten_threads(groups, {groups[1].key}, {1, 2})
    assert [(row.header.uid, row.depth, row.count, row.unread) for row in rows] == [
        (2, 0, 3, 2),
        (1, 1, 3, 2),
    ]


def test_grouping_and_flattening_preserve_ten_thousand_loaded_messages():
    headers = [
        header(
            uid,
            f"<id-{uid}@example.com>",
            in_reply_to=f"<id-{uid - 1}@example.com>" if uid % 10 else "",
        )
        for uid in range(1, 10_001)
    ]
    Random(42).shuffle(headers)
    groups = group_threads(headers)
    assert len(groups) == 1_001
    assert sum(len(group.members) for group in groups) == 10_000
    rows = flatten_threads(groups, {group.key for group in groups})
    assert len(rows) == 10_000
    assert {row.header.uid for row in rows} == set(range(1, 10_001))
    assert groups[0].representative.uid == 10_000


def test_long_chain_has_no_recursion_limit():
    groups = group_threads(
        [
            header(uid, f"<id-{uid}@example.com>", in_reply_to=f"<id-{uid - 1}@example.com>")
            for uid in range(1, 10_001)
        ]
    )
    assert uids(groups) == (tuple(range(10_000, 0, -1)),)


def test_conversation_unread_is_derived_from_current_members():
    original = header(1)
    assert Conversation("uid:1", (original,)).unread == 1
    assert Conversation("uid:1", (replace(original, seen=True),)).unread == 0


def test_conversation_representative_is_newest_loaded_uid():
    assert Conversation("group", (header(1), header(3), header(2))).representative.uid == 3
