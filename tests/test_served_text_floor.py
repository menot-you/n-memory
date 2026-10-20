"""The owner's obligation text, guarded on the PORT side — because the judge cannot.

`reconcile_pin_floor` in `the test suite` makes the contract the FLOOR of the served text:
the port may PREPEND, and the bytes must survive verbatim as a suffix. That
predicate guards the canonical text. It cannot guard the OWNER's, and the reason is
structural, not an oversight: deleting every prepend leaves `port == pin`, which
satisfies `endswith` and passes green. A differential judge measures against the specification,
and the contract does not contain these sentences — asking it to hold them is asking it to
be authority over bytes it has never seen.

So the floor lives here, on the port's own side, and it needs neither oracle nor
database: it reads the catalogue the server actually serves and asserts the sentences
are still in it.

These are not decoration. Each one is the trigger that makes a COLD agent do the
right thing before it has any reason to: read memory before the first tool, persist
an owner's order in the turn it is given, record an outcome with its evidence, close
the bracket before a compaction nobody warned about. They were authored deliberately
in 8f4241a and 8e105db. Measured 2026-08-30: 846 bytes across five tools, and zero
bytes of the own text altered anywhere in the 24.

2026-09-26 (prompt audit, pending the owner's acceptance): the session_finish sentence
is reworded to the handoff behaviour the server has (N4); the other four lose their
capitals and booster clauses, and dod_set's names how the DoD is created (N14).
TOTAL_OWNER_BYTES moves with them.
"""

from __future__ import annotations

import json
import os

# tool -> the exact obligation the port PREPENDS to the text.
# Verbatim, including the trailing space that separates it from the sentence.
OWNER_PREPENDS: dict[str, str] = {
    "nmemory_context_show": (
        "Call this before your first other tool in every session: it recovers where "
        "the owner stopped. "
    ),
    "nmemory_dod_set": (
        "The accepted DoD exists before the unit is dispatched: create it with "
        "nmemory_capsule_ingest kind=dod, bind it with nmemory_relation_add dod_of, "
        "and accept it here. "
    ),
    "nmemory_capsule_ingest": (
        "Persist an owner correction, law, preference or decision in the turn it is "
        "stated, before the work it changes: a later turn may never come. "
    ),
    "nmemory_outcome_record": (
        "Call this when a result becomes true, with its evidence, so the result has a "
        "durable witness beyond the screen. "
    ),
    "nmemory_session_finish": (
        "Call this before compaction, session end or handoff; the owner can end a "
        "session without warning, and anything not persisted by then is lost. "
    ),
}

TOTAL_OWNER_BYTES = 653


def _served_catalogue() -> dict[str, str]:
    """The descriptions the server serves, read the same way `get_tool_definitions` does."""
    here = os.path.dirname(os.path.abspath(__file__))
    schema = os.path.join(here, "..", "nmemory", "tools_schema.json")
    with open(schema, "r", encoding="utf-8") as fh:
        tools = json.load(fh)
    assert tools, f"tool catalogue {schema} carries zero tools"
    return {t["name"]: t.get("description", "") for t in tools}


def test_the_owner_obligations_are_still_served() -> None:
    """Each prepend is present, at the FRONT, in the description the server serves."""
    catalogue = _served_catalogue()
    missing: list[str] = []
    for name, prepend in OWNER_PREPENDS.items():
        served = catalogue.get(name)
        if served is None:
            missing.append(f"  {name}: the tool is gone from the catalogue entirely")
        elif not served.startswith(prepend):
            missing.append(
                f"  {name}: the obligation is not at the front of the served text.\n"
                f"    expected prefix: {prepend!r}\n"
                f"    served instead:  {served[: len(prepend) + 40]!r}"
            )
    assert not missing, (
        "owner-authored obligation text was dropped from the served catalogue.\n"
        "The differential judge CANNOT catch this — deleting a prepend leaves the "
        "port equal to the specification, which passes the contract floor. This test is the only "
        "thing holding it:\n" + "\n".join(missing)
    )


def test_the_obligation_budget_did_not_silently_shrink() -> None:
    """A count, deliberately: a sentence trimmed to nothing still `startswith` a prefix
    of itself, and only the total catches a quiet erosion across several tools."""
    total = sum(len(p) for p in OWNER_PREPENDS.values())
    assert total == TOTAL_OWNER_BYTES, (
        f"the obligation text measures {total} bytes, not {TOTAL_OWNER_BYTES}. "
        "If this was deliberate, move the constant in the SAME commit that moves the "
        "text, so the next reader sees a decision instead of drift."
    )


def test_no_other_tool_grew_a_prepend_without_being_declared() -> None:
    """A sixth tool acquiring owner text is a decision that belongs in this table.

    CEILING, and it is real: this matches four literal openers — the three the five
    use, plus `An owner `, capsule_ingest's until 2026-09-26. A sixth prepend that
    opens some other way passes green here, and the pin floor absorbs it too —
    measured, both. It catches the likely case (obligation
    text written in the voice of the five) and nothing more. The exact check would
    need the own text to subtract, which is precisely what this file exists to
    do WITHOUT; a heuristic that says so is honest, one that reads as exhaustive is
    not.
    """
    catalogue = _served_catalogue()
    undeclared = sorted(
        name
        for name, served in catalogue.items()
        if name not in OWNER_PREPENDS
        and served.startswith(("Call this ", "The accepted ", "An owner ", "Persist "))
    )
    assert not undeclared, (
        "these tools now open with obligation-shaped text and are NOT in "
        f"OWNER_PREPENDS: {undeclared}. Declare them here, so the contract floor and this "
        "floor keep naming the same five surfaces."
    )
