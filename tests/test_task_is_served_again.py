"""`task` leaves the recall fence; `dod` stays in it. Decided by measurement.

THE PLAN fenced both `dod` and `task` out of `nmemory_capsule_retrieve`, on a SUPPLY-side
statistic: "task is 78.7% never read". That is not the same question as the
DEMAND-side one — when an agent asks, what does it get? Replaying the 1549
historical receipts answered it:

  dod   —    41 result slots of 8585 (0.5%); 2 capsules ever served, both on a
             single day; 8% re-read, the worst of any kind. Fencing costs
             nothing, and the owner's order keeps it fenced.
  task  —  1679 slots (19.6%): the MOST-served kind in the store. 341 capsules
             served, 148 of them (43%) across two or more days and 85 across
             three. Fencing removes a fifth of everything the store serves,
             touches 42.8% of queries and EMPTIES up to 110 of them, turning
             `grounded` into `missing_evidence` for real questions.

The measurement that settled it came later and is decisive: `nmemory.usage` —
the table behind "% ever retrieved", the number the fence existed to
improve — could only ever see `nmemory_capsule_retrieve`. With 920 sessions each
contract-bound to call `nmemory_context_show` first, it was blind to AT LEAST 37.2% of
reads, so the baseline undercounts by >= 1.59x. Fencing `task` would have
improved that number by shrinking the denominator of a measure that was not
measuring what it claimed.

WHAT WOULD REVERSE THIS. The cross-day figure is a proxy: it was the best
available while `recall_receipts.session_id` was NULL in 1545 of 1549 rows.
`nmemory.read_traffic` now records the asking session on every surface. If
per-session data shows tasks are read only inside the campaign session that
wrote them — scaffolding, never shared — the fence becomes right again, and it
is one line away.
"""

from __future__ import annotations

from storefile import fresh_store_path

from nmemory.database import Database
from nmemory.retrieve import RECALL_EXCLUDED_KINDS
from nmemory.server import MemoryServer
from nmemory.store import Store


def _served(server: MemoryServer, term: str) -> tuple[list[str], dict[str, int]]:
    response = server.call_tool("nmemory_capsule_retrieve", {"terms": [term]})
    assert not response["result"]["isError"], response
    import json

    body = json.loads(response["result"]["content"][0]["text"])
    # The WIRE shape, not retrieve()'s internal one: the server renders
    # `evidence`/`exclusions` as `results`/`excluded`. Asserting on the internal
    # names passes vacuously — `.get()` returns empty and every membership test
    # is trivially false.
    ids = [e["id"] for e in body.get("results", [])]
    return ids, body.get("excluded", {})


def test_a_task_is_served_and_a_dod_is_not() -> None:
    url = fresh_store_path("task_unfence")
    pg = Database(url)
    server = MemoryServer(store=Store.open(url))

    for kind in ("task", "dod", "decision"):
        response = server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": f"Zebrafish marker observation for the {kind} plane",
                "kind": kind,
                "source": "test_task_unfence",
                "anchor": kind,
            },
        )
        assert not response["result"]["isError"], response

    ids, exclusions = _served(server, "zebrafish")
    kinds = {}
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("SELECT capsule_id, kind FROM classifications")
        kinds = dict(cur.fetchall())

    served_kinds = {kinds.get(i) for i in ids}
    assert "task" in served_kinds, (
        "a task matching the query is served again: it is the most-served kind "
        "in the live store and 43% of its read capsules are read across days"
    )
    assert "dod" not in served_kinds, "dod stays fenced (owner's order)"
    assert exclusions.get("kind_excluded") == 1, (
        "exactly one capsule is fenced, and it is the dod"
    )


def test_the_fence_names_only_dod() -> None:
    """The constant IS the decision; the rollback the PLAN promised is this line."""
    assert RECALL_EXCLUDED_KINDS == ("dod",)
