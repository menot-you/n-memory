"""nmemory_capsule_list: list capsules by fence and sidecar filters.

The module name carries a trailing underscore so it never shadows `list`.

`limit` keeps the NEWEST tail of the whole fenced range, so a consumer that must
read a WHOLE session or project could reach no further than one response held:
the older rows were addressable by no limit at all. `page_size` opts in to the
FORWARD walk of the same range (oldest first, by store order), and a request
that does not name it is answered exactly as before — same rows, same tail
semantics, same keys.

The continuation is this module's wire form of a position, and the position
itself belongs to the repository (`ListPage.last_scanned_seq`). It is opaque by
contract: the caller hands it back unchanged and reads nothing out of it.
`nmemory_work_list` keeps its own cursor codec on purpose — that one binds the
canonical generation and REFUSES once state moves, which is right for a work
plan that must be collected whole, and wrong here: a capsule written between two
pages must simply appear on a later page.

`with_record` is available only with `page_size`; each row then carries the full
`nmemory_capsule_show` result under `record`. The complete stdio response stays
within `LIST_RECORD_PAGE_MAX_BYTES`: a byte-cut page ends before the next row and
continues after the last row included, while a record that cannot fit alone is
refused by capsule id and budget. Omitting it leaves the compact row unchanged.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .. import NMemoryError
from ..capsule import (
    format_rfc3339,
    headline_of,
)
from ..capture import CaptureContext
from ..project import (
    canonical_project,
)
from ..queries.standing import standing_of
from ..repositories.capsules import (
    LIST_PAGE_MAX,
    LIST_RECORD_PAGE_MAX_BYTES,
    ListFilter,
)
from ..wire import decode_cursor, encode_cursor, wire_bytes
from .common import (
    ADVISORY_LABEL,
    DATA_FRAMING,
    HandlerState,
    record_read_traffic,
    verb_result,
)
from .errors import invalid_params
from .get import _render_capsule_get

#: The continuation's version tag. It names the TOOL as well as the version: a
#: cursor from another paged surface decodes and must still be refused.
_CURSOR_TAG = "cap-list-v1"

#: The longest continuation the decoder will even look at. A cursor this server
#: issued is ~120 chars; the bound keeps a hostile string from reaching base64
#: and JSON at all.
_CURSOR_MAX_CHARS = 4096

_MALFORMED = "invalid capsule-list cursor"
_REBOUND = (
    "capsule-list cursor was issued for a different fence or filter — a cursor "
    "continues the request that issued it; restart without cursor"
)


class ListCursorError(NMemoryError):
    """A continuation this request cannot read: the refusal names which fault."""


#: The request id the JSON-RPC reply echoes is the client's, so the measure takes
#: the widest one a client plausibly sends rather than a one-digit stand-in.
_WIRE_ID = "0" * 64
#: What `standing` adds to a row after the page is cut (it is read for the served
#: rows at once). The running count reserves it; the exact measure below decides.
_STANDING_RESERVE = 256


def _wire_response_bytes(answer: dict[str, Any]) -> int:
    """The MCP JSON-RPC line the stdio writer emits, including its newline."""
    return wire_bytes({"jsonrpc": "2.0", "id": _WIRE_ID, **verb_result(answer)}) + 1


def _row_wire_bytes(row: dict[str, Any]) -> int:
    """What one row adds to that line: its JSON text, escaped once more because the
    answer travels as the text of the reply's content. JSON string escaping is per
    character, so the row's share of the line is the escaped length of its own text,
    and a page's line is its envelope plus the sum of its rows and separators."""
    return wire_bytes(json.dumps(row, ensure_ascii=False)) - 2


def _page_wire_size(rows: list[dict[str, Any]], next_cursor: str | None) -> int:
    answer: dict[str, Any] = {
        "label": ADVISORY_LABEL,
        "framing": DATA_FRAMING,
        "returned": len(rows),
        "entries": rows,
        "next_cursor": next_cursor,
    }
    return _wire_response_bytes(answer)


def _oversize_record(capsule_id: str) -> dict[str, Any]:
    return invalid_params(
        f"record for capsule {capsule_id} exceeds the "
        f"{LIST_RECORD_PAGE_MAX_BYTES}-byte capsule-list response budget"
    )


def _binding(bound: list[Any]) -> str:
    """The digest of the fences and filters a continuation is bound to.

    Every axis that selects rows enters: a cursor replayed under another fence or
    another sidecar filter would otherwise continue a POSITION into a different
    range and silently skip rows the new request should have seen.
    """
    payload = json.dumps(bound, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{_CURSOR_TAG}\x00{payload}".encode()).hexdigest()


def _encode_cursor(binding: str, after_seq: int) -> str:
    return encode_cursor([_CURSOR_TAG, binding, after_seq])


def _decode_cursor(cursor: str, binding: str) -> int:
    """The position a continuation carries; it raises instead of returning a bad one.

    Fail-closed on every axis: the shape is checked before the binding, so a
    forged string is told apart from a cursor replayed under another filter, and
    neither reaches the store. There is no server-side cursor state to consult —
    the string IS the position, which is why nothing in it is trusted.
    """
    decoded = decode_cursor(cursor, _CURSOR_MAX_CHARS)
    if (
        not isinstance(decoded, list)
        or len(decoded) != 3
        or decoded[0] != _CURSOR_TAG
        or not isinstance(decoded[1], str)
        or len(decoded[1]) != 64
        or type(decoded[2]) is not int
        or not 1 <= decoded[2] <= 2**63 - 1
    ):
        raise ListCursorError(_MALFORMED)
    if decoded[1] != binding:
        raise ListCursorError(_REBOUND)
    return int(decoded[2])


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    proj_id = args.get("project_id")
    if proj_id is not None:
        proj_id = canonical_project(proj_id)
    proj_prefix = args.get("project_prefix")
    if proj_prefix is not None:
        proj_prefix = canonical_project(proj_prefix)
    session_id = args.get("session_id")
    want_kind = args.get("kind")
    want_tier = args.get("tier")
    want_expired = args.get("expired")
    want_review = args.get("review_state")
    limit = args.get("limit")
    page_size = args.get("page_size")
    cursor = args.get("cursor")
    with_record = args.get("with_record")

    if with_record is not None and type(with_record) is not bool:
        return invalid_params("with_record must be a boolean")

    sidecar_filtered = (
        want_kind is not None
        or want_tier is not None
        or want_expired is not None
        or want_review is not None
    )

    # The opt-in, and the two ways of asking that cannot both be meant.
    after_seq: int | None = None
    if page_size is None:
        if with_record is not None:
            return invalid_params(
                "with_record needs page_size: full records are available only on a "
                "paged read"
            )
        if cursor is not None:
            return invalid_params(
                "cursor needs page_size: the forward walk is opt-in — pass page_size "
                "to start it, then hand next_cursor back unchanged with the same "
                "page_size, fences and filters"
            )
    else:
        if limit is not None:
            return invalid_params(
                "limit and page_size select different reads: limit keeps the NEWEST "
                "rows of the whole fenced range, page_size walks that range forward "
                "from the oldest — pass one, not both"
            )
        if type(page_size) is not int or not 1 <= page_size <= LIST_PAGE_MAX:
            return invalid_params(
                f"page_size must be an integer from 1 to {LIST_PAGE_MAX}"
            )
        binding = _binding(
            [
                proj_id,
                proj_prefix,
                session_id,
                want_kind,
                want_tier,
                want_expired,
                want_review,
                with_record if with_record is True else None,
            ]
        )
        if cursor is not None:
            try:
                after_seq = _decode_cursor(cursor, binding)
            except ListCursorError as exc:
                return invalid_params(str(exc))

    filt = ListFilter(
        project_id=proj_id,
        project_prefix=proj_prefix,
        session_id=session_id,
        limit=None if (sidecar_filtered or page_size is not None) else limit,
        # The whole-range read selects the kind in SQL; a page keeps it a sidecar
        # filter applied after the cut, which its continuation is bound to.
        kind=want_kind if page_size is None else None,
    )
    page = None
    if page_size is None:
        listed = state.store.capsules.list(filt)
    else:
        page = state.store.capsules.list_page(
            filt, after_seq=after_seq, page_size=page_size
        )
        listed = page.rows

    rows: list[dict[str, Any]] = []
    row_sequences: list[int] = []
    byte_cut = False
    # The running size of a record page's reply: its envelope with the widest cursor
    # this range can carry, then each row as it is taken.
    used = (
        _page_wire_size([], _encode_cursor(binding, 2**63 - 1))
        if with_record is True
        else 0
    )
    for stored in listed:
        tier = state.store.tiers.get_tier(stored.id)
        tier_str = tier.value if hasattr(tier, "value") else str(tier)
        if want_tier is not None and tier_str != want_tier:
            continue

        if want_review is not None:
            verdict = state.store.reviews.review_verdict(stored.id)
            if verdict != want_review:
                continue

        is_exp = (
            stored.capsule.freshness.valid_to is not None
            and stored.capsule.freshness.valid_to < now
        )
        if want_expired is not None and is_exp != want_expired:
            continue

        cls = state.store.classifications.get_classification(stored.id)
        kind_val = cls.kind.value if cls else None
        if want_kind is not None and kind_val != want_kind:
            continue

        superseded = state.store.relations.is_superseded(stored.id)
        pinned = state.store.pins.get_pin(stored.id)

        row: dict[str, Any] = {
            "id": stored.id,
            "handle": state.store.capsules.get_handle(stored.id),
            "project_id": stored.capsule.scope.project_id,
            "instruction_taint": stored.capsule.instruction_taint,
            "created_at": format_rfc3339(stored.created_at),
            "headline": headline_of(stored.capsule.content),
        }
        if with_record is True:
            row["record"] = _render_capsule_get(state, stored, context, actor, now)
        if tier_str in ("archived", "quarantined"):
            row["tier"] = tier_str
        if is_exp:
            row["expired"] = True
        if kind_val is not None:
            row["kind"] = kind_val
        if superseded:
            row["superseded"] = True
        if pinned:
            row["pinned"] = True
        if session_id is not None:
            row["session_id"] = stored.session_id
        if with_record is True:
            cost = _row_wire_bytes(row) + _STANDING_RESERVE + (2 if rows else 0)
            if rows and used + cost > LIST_RECORD_PAGE_MAX_BYTES:
                # The page ends BEFORE this row and continues after the last row it
                # served, so this row opens the next page whole: nothing is cut and
                # nothing is skipped.
                byte_cut = True
                break
            used += cost
        rows.append(row)
        if page is not None:
            row_sequences.append(stored.seq)
        if with_record is True and used > LIST_RECORD_PAGE_MAX_BYTES:
            # A row alone over the running count: the exact measure below serves it
            # on a page of its own or refuses it by name.
            byte_cut = True
            break

    if sidecar_filtered and limit is not None and len(rows) > limit:
        rows = rows[-limit:]
    # U3: nothing is served without standing (dod/task stay out by design).
    list_standing = standing_of(state.store.db, [row["id"] for row in rows], now=now)
    for row in rows:
        if row["id"] in list_standing:
            row["standing"] = list_standing[row["id"]]

    answer: dict[str, Any] = {
        "label": ADVISORY_LABEL,
        "framing": DATA_FRAMING,
        "returned": len(rows),
        "entries": rows,
    }
    if page is not None:
        # The end marker is the END OF THE SCAN, never an empty page: every row of
        # a page can fail a sidecar filter above while rows the consumer wants
        # still lie further on, and a consumer that stopped there would read a
        # TRUNCATED session as a complete one. A scan that saw nothing at all has
        # no position to carry and has also, by definition, reached the end.
        answer["next_cursor"] = (
            _encode_cursor(binding, row_sequences[-1])
            if byte_cut and row_sequences
            else None
            if page.exhausted or page.last_scanned_seq is None
            else _encode_cursor(binding, page.last_scanned_seq)
        )
    if with_record is True:
        while _wire_response_bytes(answer) > LIST_RECORD_PAGE_MAX_BYTES:
            if len(rows) == 1:
                return _oversize_record(str(rows[0]["id"]))
            rows.pop()
            row_sequences.pop()
            byte_cut = True
            answer["returned"] = len(rows)
            answer["next_cursor"] = (
                _encode_cursor(binding, row_sequences[-1]) if row_sequences else None
            )

    # U1 (nmemory-curator): `list` serves — the row names the filter and the ids
    # that arrived. Arrival, never recall (RANKING_SURFACES).
    record_read_traffic(
        state,
        "list",
        [row["id"] for row in rows],
        " ".join(str(v) for v in (want_kind, want_tier, session_id) if v is not None)
        or "list",
        proj_id,
        proj_prefix,
        None,
        context,
        now,
    )
    return verb_result(answer)
