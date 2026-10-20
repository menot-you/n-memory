"""`headlines` in nmemory_digest_show is the GLOBAL cap — and the ceiling on response size.

Measured by the owner on 2026-08-28: `nmemory_digest_show(headlines=20)` returned 560,744
characters with 1,294 rows in `most_recalled`. The argument was validated and discarded,
and `most_recalled`/`pinned` lacked both `sort` and `slice`.

Upstream implementation (`src/server/digest.rs` + `src/server/projection.rs`):

- `n = headlines ?? DIGEST_HEADLINES_DEFAULT` (10);
- `newest`: newest-first, `take(n)`;
- `most_recalled`: `recall_count` desc, then `last_recalled_at` desc, then
  `seq` asc — and ONLY THEN `take(n)`; `most_recalled_total` counts BEFORE the cutoff;
- `open_session_ids`: `take(n)`, with exact `open_sessions`;
- `pinned`: newest-first, capped at `DIGEST_PINNED_CAP` (20) — its OWN constant,
  never `n` — with exact `pinned_total`.

The size ceiling does not come from upstream: it is the wire protection missing there.
A correct cap limits the NUMBER of rows, never their WIDTH, so a high `headlines`
on a large store still exceeds half a megabyte.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from storefile import fresh_store_path, sample_capsule_id

from nmemory.server import MemoryServer
from nmemory.store import Store
from nmemory.tools.digest import DIGEST_HEADLINES_DEFAULT, DIGEST_MAX_WIRE_BYTES

# Store population: 21 capsules, of which 17 recalled and all 21 pinned.
# 21 > DIGEST_PINNED_CAP (20) and 17 > DIGEST_HEADLINES_DEFAULT (10), so every
# cap in this file actually cuts.
N_CAPSULES = 21
N_RECALLED = 17
N_SESSIONS = 12
HEADLINES_K = 7


def _fresh_server() -> MemoryServer:
    return MemoryServer(store=Store.open(fresh_store_path()))


def _payload(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res, f"nmemory_digest_show respondeu erro de protocolo: {res}"
    assert res["result"]["isError"] is False, res["result"]
    parsed: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return parsed


def _ingest(server: MemoryServer, items: list[dict[str, Any]]) -> None:
    res = server.call_tool("nmemory_capsule_ingest", {"items": items})
    assert "result" in res and res["result"]["isError"] is False, res


def _seed_ordered_store(server: MemoryServer) -> None:
    "21 capsules with a recall pattern whose CORRECT order is reverse seq."
    _ingest(
        server,
        [
            {
                "content": f"Digest cap test capsule number {i}.",
                "source": "session:test-digest-cap",
                "anchor": f"PLAN.md:{i}",
                "confidence": 0.9,
                "project_id": "nmemory/digestcap",
                "kind": "fact",
            }
            for i in range(1, N_CAPSULES + 1)
        ],
    )

    base = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
    counts = {i: i for i in range(1, 15)}
    counts[15] = 15
    counts[16] = 15
    counts[17] = 15
    last_at = {i: base + timedelta(seconds=i) for i in range(1, 15)}
    last_at[15] = base + timedelta(seconds=300)
    last_at[16] = base + timedelta(seconds=100)
    last_at[17] = base + timedelta(seconds=100)

    for i in range(1, N_RECALLED + 1):
        for _ in range(counts[i]):
            server.store.traffic.record_recall(f"cap-{i}", last_at[i])

    for i in range(1, N_CAPSULES + 1):
        server.store.pins.set_pin(f"cap-{i}", True, "teste de cap", "judge", base)

    for _ in range(N_SESSIONS):
        res = server.call_tool("nmemory_session_start", {})
        assert "result" in res, res


# The order produced by upstream: count desc, last_recalled_at desc, seq asc.
EXPECTED_MOST_RECALLED = [
    sample_capsule_id(15),
    sample_capsule_id(16),
    sample_capsule_id(17),
    sample_capsule_id(14),
    sample_capsule_id(13),
    sample_capsule_id(12),
    sample_capsule_id(11),
    sample_capsule_id(10),
    sample_capsule_id(9),
    sample_capsule_id(8),
    sample_capsule_id(7),
    sample_capsule_id(6),
    sample_capsule_id(5),
    sample_capsule_id(4),
    sample_capsule_id(3),
    sample_capsule_id(2),
    "cap-1",
]


def test_digest_headlines_caps_every_named_list() -> None:
    server = _fresh_server()
    _seed_ordered_store(server)

    d = _payload(server.call_tool("nmemory_digest_show", {"headlines": HEADLINES_K}))

    # EXACT length, never `<=`: `newest` already returned 5, and a `<= 7` would
    # have glossed over the defect without seeing anything.
    assert len(d["newest"]) == HEADLINES_K, (
        f"newest should carry {HEADLINES_K} rows, carried {len(d['newest'])}"
    )
    assert len(d["most_recalled"]) == HEADLINES_K, (
        f"most_recalled should carry {HEADLINES_K} rows, "
        f"carried {len(d['most_recalled'])}"
    )
    assert len(d["open_session_ids"]) == HEADLINES_K, (
        f"open_session_ids should carry {HEADLINES_K} ids, "
        f"carried {len(d['open_session_ids'])}"
    )
    for lane in ("ready", "blocked", "done"):
        assert len(d["dag"][lane]) <= HEADLINES_K, (
            f"dag.{lane} exceeded the cap: {len(d['dag'][lane])}"
        )

    # Totals remain EXACT and outside the cap — which is what separates "capped"
    # from "complete" for the reader.
    assert d["newest_total"] == N_CAPSULES
    assert d["most_recalled_total"] == N_RECALLED
    assert d["open_sessions"] == N_SESSIONS
    assert d["pinned_total"] == N_CAPSULES

    # newest is newest-first.
    assert [e["id"] for e in d["newest"]] == [
        f"cap-{i}" for i in range(N_CAPSULES, N_CAPSULES - HEADLINES_K, -1)
    ]

    # The truncated sessions are the OLDEST-opened, never an arbitrary slice.
    assert d["open_session_ids"] == [f"sess-{i}" for i in range(1, HEADLINES_K + 1)]


def test_digest_most_recalled_is_ranked_before_it_is_cut() -> None:
    server = _fresh_server()
    _seed_ordered_store(server)

    d = _payload(server.call_tool("nmemory_digest_show", {"headlines": HEADLINES_K}))
    rows = d["most_recalled"]

    # A cap without ordering returns the WRONG capsules: slicing the list in append
    # order yields the 7 least recalled under the name "most recalled".
    assert [r["id"] for r in rows] == EXPECTED_MOST_RECALLED[:HEADLINES_K], (
        "most_recalled must be ordered by recall_count desc, then "
        "last_recalled_at desc, then seq asc — BEFORE the cut"
    )
    assert [r["recall_count"] for r in rows] == [15, 15, 15, 14, 13, 12, 11]

    assert rows[1]["last_recalled_at"] == rows[2]["last_recalled_at"]

    # The complete list remains sorted when the cap cuts nothing.
    wide = _payload(server.call_tool("nmemory_digest_show", {"headlines": 100}))
    assert [r["id"] for r in wide["most_recalled"]] == EXPECTED_MOST_RECALLED
    assert wide["most_recalled_total"] == N_RECALLED


def test_digest_omitted_headlines_defaults_to_ten() -> None:
    server = _fresh_server()
    _seed_ordered_store(server)

    d = _payload(server.call_tool("nmemory_digest_show", {}))

    assert DIGEST_HEADLINES_DEFAULT == 10
    assert len(d["newest"]) == DIGEST_HEADLINES_DEFAULT
    assert len(d["most_recalled"]) == DIGEST_HEADLINES_DEFAULT
    assert len(d["open_session_ids"]) == DIGEST_HEADLINES_DEFAULT
    assert [r["id"] for r in d["most_recalled"]] == (
        EXPECTED_MOST_RECALLED[:DIGEST_HEADLINES_DEFAULT]
    )


def test_digest_pinned_has_its_own_cap_and_is_newest_first() -> None:
    """`pinned` does not follow `n`: upstream caps at DIGEST_PINNED_CAP (20)."""
    server = _fresh_server()
    _seed_ordered_store(server)

    d = _payload(server.call_tool("nmemory_digest_show", {"headlines": HEADLINES_K}))

    assert len(d["pinned"]) == 20, (
        f"pinned is capped at DIGEST_PINNED_CAP=20, came with {len(d['pinned'])}"
    )
    assert d["pinned_total"] == N_CAPSULES
    assert [e["id"] for e in d["pinned"]] == [
        f"cap-{i}" for i in range(N_CAPSULES, 1, -1)
    ]


def test_digest_rejects_negative_headlines() -> None:
    """`headlines` is `usize` upstream. In Python `lista[:-1]` would slice silently."""
    server = _fresh_server()
    _seed_ordered_store(server)

    res = server.call_tool("nmemory_digest_show", {"headlines": -1})
    assert "error" in res, f"negative headlines must be refused, got: {res}"


def test_digest_response_stays_under_the_declared_wire_ceiling() -> None:
    """The wire: a correct cap limits the NUMBER of rows, never the WIDTH of rows."""
    server = _fresh_server()

    wide_line = "Wide capsule line written to prove the size ceilings. " * 6
    _ingest(
        server,
        [
            {
                "content": f"{i:04d} {wide_line}",
                "source": "session:test-digest-ceiling",
                "anchor": f"PLAN.md:{i}",
                "confidence": 0.9,
                "project_id": "nmemory/digestcap",
                "kind": "fact",
            }
            for i in range(1, 401)
        ],
    )

    res = server.call_tool("nmemory_digest_show", {"headlines": 400})
    d = _payload(res)
    wire = len(res["result"]["content"][0]["text"].encode("utf-8"))

    assert wire <= DIGEST_MAX_WIRE_BYTES, (
        f"digest devolveu {wire} bytes, acima do teto declarado {DIGEST_MAX_WIRE_BYTES}"
    )
    # The size cutoff is NAMED: without it the response lies, because the schema
    # teaches that "list length < total" is resolved by increasing `headlines` — and here
    # increasing `headlines` only makes it worse.
    assert "truncated" in d and "newest" in d["truncated"], (
        f"the size cut must declare itself, got: {d.get('truncated')}"
    )
    # Totals do NOT lie about the population even after the cutoff.
    assert d["newest_total"] == 400
    assert len(d["newest"]) < 400


def test_digest_ceiling_is_dormant_on_a_small_store() -> None:
    """Below the ceiling the response is standard — wire protection must not touch anything."""
    server = _fresh_server()
    _seed_ordered_store(server)

    res = server.call_tool("nmemory_digest_show", {"headlines": HEADLINES_K})
    d = _payload(res)

    assert len(res["result"]["content"][0]["text"].encode("utf-8")) < (
        DIGEST_MAX_WIRE_BYTES
    )
    assert "truncated" not in d, (
        f"a rede disparou num store pequeno: {d.get('truncated')}"
    )
