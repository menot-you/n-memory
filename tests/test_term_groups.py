"""The contract of `terms` is GROUPS, and the wire flattened it into loose words.

`nmemory_capsule_retrieve` serves this, verbatim: *"Term matching is FTS5 word-exact with
words inside one term AND-matched and terms OR-matched"*. `store.py:1111` fulfills
that law — `matched_groups = sum(1 for q in queries if q.issubset(words))`, one
group per term. Except the list never got there: `server.py` did
`query_str = " ".join(terms)` and `RetrieveParams.query` was `str`, so
`retrieve()` reconstructed groups via `query.split()` and EACH WORD became
its own group. A multi-word term thus grounded on any
subset of itself.

The second defect is on the EXPLAIN side. Upstream computes `matched_terms` using the
SAME matcher as the ranker (`retrieve.rs:3975 fn matched_groups` over `tokens()`,
which folds diacritics) and emits the entire TERM, or `alias:<term>`. The port
recomputed via substring over the raw query, without folding diacritics and without
groups, and emitted words. Hence `matched_terms: []` in a `grounded` result
(rcpt-1474, `terms=["memory_scan"]`) and the alias attribution promised by the
tool description itself — *"an alias hit grounds and is explained as
alias:<term> in matched_terms"* — never appearing.

Upstream implementation (`retrieve.rs:3975 fn matched_groups` and `:3995
fn term_matches`) — *"the store's within-term AND match, mirrored"*.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from storefile import default_store_path

from nmemory.cli import main
from nmemory.server import MemoryServer
from nmemory.store import Store


def fresh_server() -> MemoryServer:
    url = default_store_path("termgroups")
    return MemoryServer(store=Store.open(url))


def teach(server: MemoryServer, term: str, alias: str) -> None:
    """A row of the synonyms table. No product code writes it (retrieve only reads
    it to expand a term), so the row is seeded here."""
    with server.store.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO synonyms (term, alias, at) VALUES (?, ?, ?)",
            (term, alias, "2026-08-31T00:00:00Z"),
        )


def ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def ingest(server: MemoryServer, content: str) -> str:
    payload = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": content,
                        "kind": "fact",
                        "source": "test:termgroups",
                        "anchor": "PLAN.md:1",
                        "project_id": "nmemory",
                    }
                ]
            },
        )
    )
    row = payload["outcomes"][0]
    assert row["status"] == "captured", row
    return str(row["id"])


def retrieve(server: MemoryServer, *terms: str) -> dict[str, Any]:
    return ok(
        server.call_tool(
            "nmemory_capsule_retrieve", {"terms": list(terms), "project_id": "nmemory"}
        )
    )


def test_a_multi_word_term_needs_every_one_of_its_words() -> None:
    """The AND inside the term. A subset does not ground."""
    server = fresh_server()
    ingest(server, "The donor audit cannot close on a ceiling of rounds.")

    payload = retrieve(server, "audit verb residue")

    assert payload["outcome"] == "abstain", payload


def test_terms_still_or_match_between_themselves() -> None:
    """The other half of the law: closing the AND must not close the OR."""
    server = fresh_server()
    ingest(server, "The donor audit cannot close on a ceiling of rounds.")

    payload = retrieve(server, "residue verb", "audit")

    assert payload["outcome"] == "grounded", payload
    assert payload["results"][0]["matched_terms"] == ["audit"], payload


def test_matched_terms_names_the_whole_term_not_a_substring_scan() -> None:
    """`memory_scan` matches by tokens (`memory` + `scan`) and explain must state so."""
    server = fresh_server()
    ingest(server, "O taint scan do memory core, medido no store vivo.")

    payload = retrieve(server, "memory_scan")

    assert payload["outcome"] == "grounded", payload
    assert payload["results"][0]["matched_terms"] == ["memory_scan"], payload


def test_a_diacritic_in_the_content_still_explains_the_folded_term() -> None:
    """The ranker folds diacritics; explain must fold the same."""
    server = fresh_server()
    ingest(server, "The test résumé left in the live store is a missing fence.")

    payload = retrieve(server, "resume")

    assert payload["outcome"] == "grounded", payload
    assert payload["results"][0]["matched_terms"] == ["resume"], payload


def test_an_alias_hit_is_explained_as_alias_of_the_queried_term() -> None:
    """Tool description promises `alias:<term>`; the port served the word."""
    server = fresh_server()
    ingest(server, "O postgres do store, com statement_timeout explicito.")
    teach(server, "pg", "postgres")

    payload = retrieve(server, "pg")

    assert payload["outcome"] == "grounded", payload
    assert payload["results"][0]["matched_terms"] == ["alias:pg"], payload


def test_a_taught_word_alias_reaches_inside_a_multi_word_term() -> None:
    """Aliases were looked up for the whole term only, so a taught word never fired.

    Teaching keyboard -> shortcuts could not help, because the lookup key was
        "canvas keyboard". The swapped form keeps the AND: a capsule with
        "shortcuts" but no "canvas" stays out.
    """
    server = fresh_server()
    hit = ingest(
        server, "Owner decision on Mac app shortcuts: Command-plus zooms the canvas."
    )
    ingest(server, "The terminal keeps its own shortcuts.")
    teach(server, "keyboard", "shortcuts")

    payload = retrieve(server, "canvas keyboard")

    assert payload["outcome"] == "grounded", payload
    assert [r["id"] for r in payload["results"]] == [hit], payload
    assert payload["results"][0]["matched_terms"] == ["alias:canvas keyboard"], payload


def test_a_taught_word_alias_reaches_a_word_joined_by_punctuation() -> None:
    """Search splits "canvas-keyboard" into two words, so the alias lookup must too.

    The lookup split on whitespace only, so the term stayed one word and the
    taught keyboard -> shortcuts alias did not fire.
    """
    server = fresh_server()
    hit = ingest(
        server, "Owner decision on Mac app shortcuts: Command-plus zooms the canvas."
    )
    teach(server, "keyboard", "shortcuts")

    payload = retrieve(server, "canvas-keyboard")

    assert payload["outcome"] == "grounded", payload
    assert [r["id"] for r in payload["results"]] == [hit], payload
    assert payload["results"][0]["matched_terms"] == ["alias:canvas-keyboard"], payload


def test_a_taught_word_alias_is_one_way_inside_a_multi_word_term() -> None:
    """keyboard -> shortcuts does not make "canvas shortcuts" reach "canvas keyboard"."""
    server = fresh_server()
    ingest(server, "The canvas keyboard map stays fixed.")
    teach(server, "keyboard", "shortcuts")

    payload = retrieve(server, "canvas shortcuts")

    assert payload["outcome"] == "abstain", payload


def test_the_cli_comma_separates_groups_not_words(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--terms` is "Comma-separated terms": the comma already separated groups.

    Replacing comma with space (`.replace(",", " ")`) flattened them exactly
    as the MCP door flattened them, and CLI is the other caller of `retrieve()`.
    """
    server = fresh_server()
    ingest(server, "The donor audit cannot close on a ceiling of rounds.")
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(server.store.db.path).parent))

    monkeypatch.setattr(
        "sys.argv",
        ["nmemory", "capsule", "retrieve", "--terms", "audit verb residue"],
    )
    main()

    assert json.loads(capsys.readouterr().out)["outcome"] == "abstain"


def test_the_miss_ledger_records_one_row_per_term_not_per_word() -> None:
    """pin `store.rs:5036` — `record_recall_miss(terms: &[String])`.

    Upstream folds each entire TERM (`fold_term`: trim + lowercase + diacritic
    folding, internal spaces preserved) and writes ONE row per term. The
    port folded per word of joined query, and a multi-word term became N
    rows — the same flattening, now in the failure ledger.
    """
    server = fresh_server()
    ingest(server, "The donor audit cannot close on a ceiling of rounds.")

    assert retrieve(server, "rounds verb")["outcome"] == "abstain"

    terms = [row["term"] for row in server.store.traffic.list_recall_misses(limit=50)]
    assert terms == ["rounds verb"], terms


def test_a_term_that_matches_nothing_still_abstains() -> None:
    """The regression that matters: the honest path does not move."""
    server = fresh_server()
    ingest(server, "O taint scan do memory core, medido no store vivo.")

    payload = retrieve(server, "termoquenaoexisteemladonenhum")

    assert payload["outcome"] == "abstain", payload


def _teach_and_retrieve(term: str, alias: str, content: str, query: str) -> None:
    server = fresh_server()
    hit = ingest(server, content)
    teach(server, term, alias)

    payload = retrieve(server, query)

    assert payload["outcome"] == "grounded", payload
    assert [r["id"] for r in payload["results"]] == [hit], payload
    assert payload["results"][0]["matched_terms"] == [f"alias:{query}"], payload


@pytest.mark.parametrize(
    ("term", "alias", "content", "query"),
    [
        # Search folds "résumé" to "resume", so the lookup must fold it too.
        ("resume", "cv", "The canvas cv panel opens on start.", "canvas résumé"),
        # A word taught with its accents keeps its lookup.
        ("résumé", "cv", "The canvas cv panel opens on start.", "canvas résumé"),
        # Search drops the comma, so the lookup must drop it too.
        (
            "keyboard",
            "shortcuts",
            "The canvas shortcuts are fixed.",
            "canvas keyboard,",
        ),
        # Every occurrence of the word is swapped in one form.
        (
            "keyboard",
            "shortcuts",
            "The canvas shortcuts are fixed.",
            "canvas keyboard keyboard",
        ),
        # A one-word term that search folds or trims still finds its alias.
        ("resume", "cv", "The canvas cv panel opens on start.", "résumé"),
        ("keyboard", "shortcuts", "The canvas shortcuts are fixed.", "keyboard,"),
        # A taught term that search splits into several words fires inside a
        # longer term. The live alias table holds such terms.
        ("e-mail", "email", "Send the weekly email digest.", "weekly e-mail"),
        (
            "test-dbs",
            "test-databases",
            "Check test-databases now.",
            "check test-dbs now",
        ),
        (
            "pet store",
            "petshop",
            "Find the petshop link here.",
            "find the pet store link",
        ),
        (
            "keyboard-shortcuts",
            "zoom",
            "The canvas zoom is fixed.",
            "canvas keyboard-shortcuts",
        ),
        ("memory_scan", "review", "canvas review", "canvas memory_scan"),
    ],
)
def test_a_taught_word_alias_reaches_each_word_search_matches(
    term: str, alias: str, content: str, query: str
) -> None:
    _teach_and_retrieve(term, alias, content, query)


def test_a_repeated_word_makes_one_form() -> None:
    from nmemory.retrieve import group_alias_forms

    group = " ".join(["keyboard"] * 500)

    assert group_alias_forms(group, [("keyboard", "shortcuts")]) == [
        " ".join(["shortcuts"] * 500)
    ]
