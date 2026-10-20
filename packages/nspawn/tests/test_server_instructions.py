"""The instructions that MCP serves say WHEN to use this, not just what it is.

Measured 2026-09-16 across 87,551 calls in real owner sessions: nMEMORY, whose
description opens with triggers, received 5,064 calls in 189 sessions; this server, which
described WHAT it is, received 2,697 in 53. The gradient is exact and is the reason for these tests.

The wording is not fixed here — the SHAPE that measurement shows works is fixed: triggers
early, and each naming the tool that resolves the moment. A trigger without a tool
name is advice, and advice did not move these numbers.

`test_mcp_l6` already guards the other side: no `nspawn_*` here that registry does not load.
"""

from __future__ import annotations

from nspawn.mcp import SERVER_INSTRUCTIONS

#: Each answers a different moment: dispatching one unit, dispatching multiple,
#: asking for the witness author cannot give, reading roster, reading budget, and reading
#: the response when notification arrives.
FERRAMENTAS_DOS_GATILHOS = (
    "nspawn_run_start",
    "nspawn_batch_start",
    "nspawn_review_start",
    "nspawn_provider_list",
    "nspawn_quota_show",
    "nspawn_result_show",
)


def test_triggers_come_early() -> None:
    """Early, because what comes late is not read."""
    b = SERVER_INSTRUCTIONS.encode("utf-8")
    assert b"Triggers" in b[:512], "the triggers do not appear in the first 512 bytes"


def gatilhos() -> str:
    """Only the triggers section, which ends where derived tool list begins.

    The first version of this test looked for the name ANYWHERE in the string — and always
    passed, because `Tools: ` is built from the registry and names every tool
    by definition. The mutant that dropped `nspawn_run_start` from trigger survived. A test
    that cannot fail is not a test, and this one almost entered like that."""
    return SERVER_INSTRUCTIONS.split("Tools: ", 1)[0]


def test_the_trigger_section_exists_and_is_not_the_tool_list() -> None:
    g = gatilhos()
    assert "Triggers" in g and len(g) < len(SERVER_INSTRUCTIONS), (
        "there is no trigger section separate from the tool list"
    )


def test_every_trigger_names_its_tool() -> None:
    g = gatilhos()
    for nome in FERRAMENTAS_DOS_GATILHOS:
        assert nome in g, (
            f"`{nome}` is named by no trigger (the tool list does not count)"
        )


def test_the_independent_witness_is_a_trigger_not_a_footnote() -> None:
    """Independent review is the promise the law most needs from here.

    `plan/SKILL.md`: «An execution-ready plan requires independent review; the author
    cannot supply that witness.» If the description does not say WHEN to call
    `nspawn_review_start`, the law asks for witness and nobody knows where to find it."""
    assert "nspawn_review_start" in SERVER_INSTRUCTIONS
    i = SERVER_INSTRUCTIONS.index("nspawn_review_start")
    assert "witness" in SERVER_INSTRUCTIONS[max(0, i - 220) : i], (
        "`nspawn_review_start` appears without the moment that calls for it"
    )


def test_instructions_fit_the_budget() -> None:
    """2048 B ceiling, same as nMEMORY: this enters EVERY session."""
    assert len(SERVER_INSTRUCTIONS.encode("utf-8")) <= 2048


def test_the_notice_promise_names_the_session_that_hears_it() -> None:
    """A run outlives the session that dispatched it, so the promise names both
    routes: a push while that session lives, and the project's next session
    start when it does not. "Pushed by the supervisor itself" promised a notice
    no process was left to push once the stdio stopped being the supervisor."""
    assert "per supervisor" not in SERVER_INSTRUCTIONS
    assert "pushed by the supervisor itself" not in SERVER_INSTRUCTIONS
    assert "next session" in SERVER_INSTRUCTIONS
