"""The active history crosses stored links and compaction boundaries."""

from __future__ import annotations

from typing import Any

import pytest

from nspawn.journal import show


def test_chain_keeps_root_and_tools_but_omits_rewound_branch() -> None:
    rows: list[dict[str, Any]] = [{"type": "session", "vendor": "claude"}]
    for at, (uuid, parent, kind) in enumerate(
        [
            ("root", None, "message"),
            ("answer", "root", "tool_call"),
            ("tool", "answer", "tool_result"),
            ("old", "tool", "message"),
            ("compact", None, "compaction"),
            ("new", "compact", "message"),
        ]
    ):
        rows.append({"type": kind, "uuid": uuid, "parentUuid": parent, "src": at})
    rows[-2]["logicalParentUuid"] = "system"
    links = [
        {"uuid": "attachment", "parentUuid": "tool", "src": 3},
        {"uuid": "system", "parentUuid": "attachment", "src": 4},
    ]
    selected = show.active_chain(rows, links)
    assert [row.get("uuid") for row in selected] == [
        None,
        "root",
        "answer",
        "tool",
        "compact",
        "new",
    ]


def test_chain_keeps_legacy_rows_in_file_order() -> None:
    rows = [{"type": "message", "text": "legacy"}, {"type": "message", "text": "tail"}]
    assert show.active_chain(rows) == rows


def test_chain_requires_a_link_for_the_native_leaf() -> None:
    rows = [
        {"type": "session", "vendor": "claude", "metadata": {"leaf_uuid": "dropped"}},
        {"type": "message", "uuid": "root", "parentUuid": None, "src": 1},
    ]
    with pytest.raises(show.ChainIncomplete, match="dropped"):
        show.active_chain(rows)
    assert (
        show.active_chain(rows, [{"uuid": "dropped", "parentUuid": "root", "src": 2}])
        == rows
    )
