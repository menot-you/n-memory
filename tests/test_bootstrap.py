"""Cold-start packs use real store state and resolve over the stdio door."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from storefile import default_store_path
from test_tools import StdioClient

from nmemory.queries.sidecars import digest_sidecars
from nmemory.server import MemoryServer
from nmemory.store import Store
from nmemory.tools import bootstrap as bootstrap_tool

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def ok(response: dict[str, Any]) -> dict[str, Any]:
    assert "result" in response and not response["result"]["isError"], response
    return json.loads(response["result"]["content"][0]["text"])


@pytest.fixture
def server() -> MemoryServer:
    url = default_store_path("bootstrap")
    return MemoryServer(store=Store.open(url))


def add(server: MemoryServer, kind: str, text: str, **extra: Any) -> str:
    result = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "kind": kind,
                        "content": text,
                        "source": "session:bootstrap-test",
                        "anchor": "test_bootstrap.py",
                        "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
                        "confidence": 0.9,
                        "project_id": "nott",
                        **extra,
                    }
                ]
            },
            now=NOW,
        )
    )
    return str(result["outcomes"][0]["id"])


def relate(
    server: MemoryServer, kind: str, src: str, dst: str, at: datetime = NOW
) -> None:
    ok(
        server.call_tool(
            "nmemory_relation_add", {"kind": kind, "from": src, "to": dst}, now=at
        )
    )


def pack(server: MemoryServer, **args: Any) -> dict[str, Any]:
    return ok(server.call_tool("nmemory_project_bootstrap", args, now=NOW))


def test_bootstrap_populates_five_sections_and_resolves_handles_over_stdio(
    server: MemoryServer,
) -> None:
    constraint = add(server, "constraint", "Never publish the protected dataset.")
    decision = add(server, "decision", "Use PostgreSQL for the catalog.")
    trap = add(server, "failure_pattern", "A closed pipe stalls writes.")
    task = add(server, "task", "Implement the catalog index.", project_id="nott/child")
    epic = add(server, "epic", "Catalog delivery epic.")
    relate(server, "part_of", task, epic)
    add(server, "constraint", "Outside constraint.", project_id="nottish")
    client = StdioClient({})  # inherits the test's NOTT_DATABASE_DIR
    try:
        data = ok(
            client.call_tool("nmemory_project_bootstrap", {"project_prefix": "nott"})
        )
        assert [r["id"] for r in data["constraints"]] == [constraint], (
            data,
            [(c.id, c.capsule.freshness) for c in server.store.capsules.list()],
            digest_sidecars(server.store.db),
        )
        assert [r["id"] for r in data["decisions"]] == [decision]
        assert [r["id"] for r in data["traps"]] == [trap]
        assert [r["id"] for r in data["ready"]["ready"]] == [task]
        assert data["ready"]["next_action"] == data["ready"]["ready"][0]["headline"]
        assert data["ready"]["ready_total"] == 1
        assert set(data["handles"]) == {constraint, decision, trap, task}
        for cid, handle in data["handles"].items():
            assert (
                ok(client.call_tool("nmemory_capsule_show", {"id": handle}))["id"]
                == cid
            )
        assert [
            key
            for key in data
            if key in {"constraints", "ready", "decisions", "traps", "handles"}
        ] == ["constraints", "ready", "decisions", "traps", "handles"]
    finally:
        client.close()


def test_scope_terms_lifecycle_and_usage(server: MemoryServer) -> None:
    constraint = add(server, "constraint", "needle protected constraint")
    decision = add(server, "decision", "needle valid decision")
    trap = add(
        server,
        "failure_pattern",
        "needle stale trap",
        valid_from="2020-01-01T00:00:00Z",
        valid_to="2020-02-01T00:00:00Z",
    )
    replacement = add(server, "failure_pattern", "replacement trap", supersedes=trap)
    server.store.tiers.set_tier(trap, "archived", NOW)
    archived = add(server, "constraint", "needle archived constraint")
    server.store.tiers.set_tier(archived, "archived", NOW)
    add(
        server,
        "decision",
        "needle expired decision",
        valid_from="2020-01-01T00:00:00Z",
        valid_to="2020-02-01T00:00:00Z",
    )
    add(server, "constraint", "needle staged constraint", staged=True)
    add(server, "decision", "needle child decision", project_id="nott/child")
    old = add(server, "constraint", "needle superseded constraint")
    add(server, "constraint", "replacement constraint", supersedes=old)
    removed = add(server, "constraint", "needle tombstoned constraint")
    server.store.capsules.forget_capsule(
        removed, "tombstone", "test removal", b"test-key", NOW
    )
    task = add(server, "task", "unmatched open work")
    staged = add(server, "task", "unmatched staged work", staged=True)
    before = digest_sidecars(server.store.db)
    data = pack(server, project_id="nott", terms=["needle"])
    assert [r["id"] for r in data["constraints"]] == [constraint]
    assert [r["id"] for r in data["decisions"]] == [decision]
    assert [r["id"] for r in data["traps"]] == [trap]
    assert data["traps"][0]["tier"] == "archived"
    assert data["traps"][0]["superseded"] is True
    assert data["traps"][0]["expired"] is True
    assert [r["id"] for r in data["ready"]["ready"]] == [task]
    assert replacement not in data["handles"] and staged not in data["handles"]
    assert digest_sidecars(server.store.db) == before
    assert pack(server, project_id="elsewhere", project_prefix="nott")["handles"] == {}


def test_global_blocker_and_cycle_fail_closed(server: MemoryServer) -> None:
    outside = add(server, "task", "External blocker", project_id="other")
    inside = add(server, "task", "Fenced work")
    relate(server, "blocks", outside, inside)
    assert pack(server, project_prefix="nott")["ready"]["ready_total"] == 0
    relate(server, "blocks", inside, outside)
    cyclic = pack(server, project_prefix="nott")
    assert cyclic["ready"]["status"] == "cycle"
    assert cyclic["ready"]["cycle"] == [outside, inside]
    assert "ready" not in cyclic["ready"]
    assert "next_action" not in cyclic["ready"]
    assert set(cyclic["handles"]) == {outside, inside}
    witness = add(server, "fact", "Observed blocker completion")
    relate(server, "witnesses", witness, outside)
    recovered = pack(server, project_prefix="nott")
    assert [r["id"] for r in recovered["ready"]["ready"]] == [inside]


def test_ready_cap_is_after_scope_and_totals_survive_budget_trim(
    server: MemoryServer,
) -> None:
    for i in range(15):
        add(server, "task", f"External work {i}", project_id="other")
    ids = [add(server, "task", f"In-scope work {i}") for i in range(12)]
    data = pack(server, project_prefix="nott", token_budget=10000)
    assert data["ready"]["ready_total"] == 12
    assert [r["id"] for r in data["ready"]["ready"]] == sorted(ids)[:10]
    assert data["budget"]["trimmed_by_budget"] == 0
    for budget in (120, 200, 350, 500, 1500):
        data = pack(server, project_prefix="nott", token_budget=budget)
        rows = data["ready"]["ready"]
        assert data["ready"]["ready_total"] == 12
        assert data["budget"]["trimmed_by_budget"] == 10 - len(rows)
        assert data["ready"]["next_action"] == (rows[0]["headline"] if rows else None)
        assert set(data["handles"]) == {r["id"] for r in rows}
        assert (
            data["budget"]["used_tokens"]
            == (len(json.dumps(data, ensure_ascii=False)) + 3) // 4
        )
        assert data["budget"]["used_tokens"] <= budget
        assert all(
            key in data
            for key in ("constraints", "decisions", "traps", "ready", "handles")
        )


@pytest.mark.parametrize("budget", [-1, 0, 1])
def test_unrepresentable_budget_is_explicit_refusal(
    server: MemoryServer, budget: int
) -> None:
    response = server.call_tool(
        "nmemory_project_bootstrap", {"token_budget": budget}, now=NOW
    )
    assert response["error"]["code"] == -32602
    assert "token_budget" in response["error"]["message"]


def test_bootstrap_does_not_invent_guidance_without_a_sidecar(
    server: MemoryServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = add(server, "constraint", "Keep the protected dataset private.")
    assert [r["id"] for r in pack(server)["constraints"]] == [cid]
    sidecars = digest_sidecars(server.store.db)
    sidecars.pop(cid)
    monkeypatch.setattr(bootstrap_tool, "digest_sidecars", lambda db: sidecars)
    result = pack(server)
    assert result["constraints"] == []
    assert cid not in result["handles"]


def test_falsified_and_quarantined_stay_out_of_the_pack(server: MemoryServer) -> None:
    """DEBT 2. The two fences `retrieve()` evaluates FIRST, asserted on the pack.

    These are not symmetrical with `superseded`/`expired`, which the trap section
    shows on purpose — a degraded trap is still a warning. `quarantined` means
    «do not trust this», `falsified` means «this was proven wrong», and a cold
    agent's orientation pack is the one surface that gets no chance to ask.
    `retrieve()` fences both before anything else (retrieve.py:661-664); the
    bootstrap handler modelled neither outside the constraint section, so this
    test was RED when written and the fence it names is the fix.

    Two observations per fence, because absence from the section alone would
    pass while the id still leaked through `handles` — the map the agent
    resolves to read the bytes.
    """
    for kind in ("decision", "failure_pattern", "task"):
        quarantined = add(server, kind, f"quarantined {kind}")
        falsified = add(server, kind, f"falsified {kind}")
        witness = add(server, "fact", f"observation that kills the {kind}")
        server.store.tiers.set_tier(quarantined, "quarantined", NOW)
        relate(server, "falsifies", witness, falsified)

        data = pack(server, project_prefix="nott")
        served = [
            r["id"] for rows in data.values() if isinstance(rows, list) for r in rows
        ]
        served += [r["id"] for r in data["ready"].get("ready", [])]
        assert quarantined not in served, (kind, "quarantined", data)
        assert falsified not in served, (kind, "falsified", data)
        assert quarantined not in data["handles"], (kind, "quarantined handle")
        assert falsified not in data["handles"], (kind, "falsified handle")


def test_every_section_total_survives_the_budget_trim(server: MemoryServer) -> None:
    """DEBT 3. The trim was only ever proved on `ready`.

    `test_ready_cap_is_after_scope_and_totals_survive_budget_trim` seeds tasks
    and nothing else, so `constraints_total`, `decisions_total` and
    `traps_total` read 0 from the first assertion to the last: three of the four
    totals were constant-zero, and a handler returning `len(selected[key])`
    instead of `len(sections[key])` would have passed it. The total is the whole
    point of the pair — the rows say what fitted, the total says what exists —
    and an agent that reads 2 of 9 constraints without being told there are 9 is
    worse off than one that reads none.
    """
    constraints = [
        add(server, "constraint", f"Constraint number {i}") for i in range(9)
    ]
    decisions = [add(server, "decision", f"Decision number {i}") for i in range(7)]
    traps = [add(server, "failure_pattern", f"Trap number {i}") for i in range(5)]
    tasks = [add(server, "task", f"Task number {i}") for i in range(6)]
    expected = {
        "constraints_total": len(constraints),
        "decisions_total": len(decisions),
        "traps_total": len(traps),
    }

    full = pack(server, project_prefix="nott", token_budget=100000)
    assert {k: full[k] for k in expected} == expected, full
    assert full["ready"]["ready_total"] == len(tasks)
    assert full["budget"]["trimmed_by_budget"] == 0
    untrimmed = sum(len(full[k]) for k in ("constraints", "decisions", "traps"))
    untrimmed += len(full["ready"]["ready"])

    trimmed_at_least_once = False
    for budget in (400, 600, 900, 1400, 2200):
        data = pack(server, project_prefix="nott", token_budget=budget)
        assert {k: data[k] for k in expected} == expected, (budget, data)
        assert data["ready"]["ready_total"] == len(tasks), (budget, data)
        rendered = sum(len(data[k]) for k in ("constraints", "decisions", "traps"))
        rendered += len(data["ready"]["ready"])
        assert data["budget"]["trimmed_by_budget"] == untrimmed - rendered, (
            budget,
            data,
        )
        assert data["budget"]["used_tokens"] <= budget, (budget, data)
        # Every rendered row still resolves, and nothing else does.
        assert set(data["handles"]) == {
            r["id"] for key in ("constraints", "decisions", "traps") for r in data[key]
        } | {r["id"] for r in data["ready"]["ready"]}
        if data["budget"]["trimmed_by_budget"] > 0:
            trimmed_at_least_once = True
    assert trimmed_at_least_once, "not vacuous: at least one budget must actually trim"


def test_a_falsification_dated_after_the_pack_does_not_fence_yet(
    server: MemoryServer,
) -> None:
    """The `at <= now` half of the falsified fence, which the test above leaves limp.

    Independent review measured it and said so: the fence test relates and packs
    at the same `NOW`, so `r.at <= now` is only ever exercised at EQUALITY — the
    mutants `>=` and «no date filter at all» both survive it, and only `<` dies.
    A falsification is an event in time: one recorded for tomorrow does not make
    a capsule untrustworthy today, and a fence that ignored the date would hide
    a live capsule on the strength of a future claim.

    It also pins an asymmetry worth knowing rather than fixing blind:
    `retrieve()` calls `store.is_falsified(cid, as_of=...)`, and with `as_of=None`
    that SQL applies NO date filter (store.py). The two surfaces therefore differ
    on a future-dated `falsifies`. Unreachable through `nmemory_relation_add`, which
    stamps `at = now` — which is exactly why it needs a test rather than a
    caller to keep it honest.
    """
    target = add(server, "decision", "A decision falsified only as of tomorrow.")
    witness = add(server, "fact", "The observation that will kill it, dated ahead.")
    tomorrow = NOW + timedelta(days=1)
    relate(server, "falsifies", witness, target, at=tomorrow)

    today = pack(server, project_prefix="nott")
    assert [r["id"] for r in today["decisions"]] == [target], (
        "a falsification dated after this pack has not happened yet",
        today,
    )
    assert target in today["handles"]

    # BELOW the boundary: falsified YESTERDAY, packed today. Without this the
    # mutant `r.at == now` survives the whole member — the two cases above sit
    # AT the boundary and ABOVE it, and `==` classifies both exactly as `<=`
    # does. Found by independent review, and it is the same lesson DEBT row 6
    # already paid for in this very PR: a boundary pin needs a row on each side
    # of the boundary, never one.
    stale = add(server, "decision", "A decision falsified yesterday.")
    yesterday_witness = add(
        server, "fact", "The observation that killed it, dated back."
    )
    relate(server, "falsifies", yesterday_witness, stale, at=NOW - timedelta(days=1))
    now_pack = pack(server, project_prefix="nott")
    assert stale not in [r["id"] for r in now_pack["decisions"]], (
        (
            "a falsification already in the past fences now, not only at the "
            "instant it was recorded"
        ),
        now_pack,
    )
    assert stale not in now_pack["handles"]

    # And the same store, read from a day later, fences it.
    later = ok(
        server.call_tool(
            "nmemory_project_bootstrap", {"project_prefix": "nott"}, now=tomorrow
        )
    )
    assert [r["id"] for r in later["decisions"]] == [], (
        "once the falsification's own date has passed, the fence bites",
        later,
    )
    assert target not in later["handles"]
