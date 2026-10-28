"""Conversation grouping within one account, folder, and UIDVALIDITY."""

from collections.abc import Sequence
from dataclasses import dataclass

from .mail import message_ids
from .models import MailHeader


@dataclass(frozen=True)
class Conversation:
    key: str
    members: tuple[MailHeader, ...]

    @property
    def representative(self) -> MailHeader:
        return max(self.members, key=lambda header: header.uid)

    @property
    def unread(self) -> int:
        return sum(not header.seen for header in self.members)


@dataclass(frozen=True)
class ThreadRow:
    header: MailHeader
    thread_key: str
    depth: int
    count: int
    unread: int
    expanded: bool


def group_threads(headers: Sequence[MailHeader]) -> tuple[Conversation, ...]:
    """Group loaded UIDs through valid ancestry, including unloaded ancestors.

    Members and conversations follow descending UID order. Repeated UIDs use the
    last supplied snapshot. Keys use the smallest connected message ID, or UID
    when no valid ID exists. Loading headers can merge groups and change keys.
    """
    parents: dict[str, str] = {}
    sizes: dict[str, int] = {}

    def root(node: str) -> str:
        if node not in parents:
            parents[node] = node
            sizes[node] = 1
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node

    def join(left: str, right: str) -> None:
        left, right = root(left), root(right)
        if left == right:
            return
        if sizes[left] < sizes[right]:
            left, right = right, left
        parents[right] = left
        sizes[left] += sizes[right]

    loaded = {header.uid: header for header in headers}
    for header in loaded.values():
        node = f"uid:{header.uid}"
        root(node)
        own = message_ids(header.message_id)[:1]
        ancestry = message_ids(" ".join(header.references))
        reply = message_ids(header.in_reply_to)
        for message_id in (*own, *ancestry, *reply):
            join(node, message_id)

    keys: dict[str, str] = {}
    for node in parents:
        if node.startswith("<"):
            component = root(node)
            keys[component] = min(keys.get(component, node), node)

    members: dict[str, list[MailHeader]] = {}
    for header in sorted(loaded.values(), key=lambda header: header.uid, reverse=True):
        node = f"uid:{header.uid}"
        component = root(node)
        members.setdefault(component, []).append(header)

    return tuple(
        Conversation(keys.get(component, f"uid:{group[0].uid}"), tuple(group))
        for component, group in members.items()
    )


def flatten_threads(
    groups: Sequence[Conversation],
    expanded: set[str],
    matching_uids: set[int] | None = None,
) -> tuple[ThreadRow, ...]:
    """Emit matching messages once, keeping counts for all loaded members."""
    rows = []
    for group in groups:
        matching = tuple(
            header
            for header in group.members
            if matching_uids is None or header.uid in matching_uids
        )
        if not matching:
            continue
        is_expanded = group.key in expanded
        visible = matching if is_expanded else matching[:1]
        unread = group.unread
        rows.extend(
            ThreadRow(
                header,
                group.key,
                int(index > 0),
                len(group.members),
                unread,
                is_expanded,
            )
            for index, header in enumerate(visible)
        )
    return tuple(rows)
