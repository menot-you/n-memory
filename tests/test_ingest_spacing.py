"""Prose that has lost its spaces is stored, and the reply warns about it.

Codex sessions wrote captures such as `GLUED` below. Their rollouts
hold the same bytes in the model's own tool call, so nMEMORY stored what it was
sent; such capsules were recalled at about half the store's rate. A capture gate
must never lose a valid memory, so the check is advisory: the capsule is stored
and its row (or the finished session, for a handoff) carries a warning that asks
for normal spacing in future writes. Identifiers, paths, JSON, hashes and real
long words never draw it.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from nmemory.language import glued_words
from nmemory.server import MemoryServer
from nmemory.store import Store

ANCHOR = "mcps/nmemory/nmemory/tools/ingest.py:301"

GLUED = (
    "Thefixtureworkercheckedthegeneratedsourceandrecordedthetestresult. "
    "Thefixtureworkerdidnotpublishthesourceorstartanexternalprocess. "
    "Thefixtureworkerkeptthereportinthetemporarytestdirectory. "
) * 4

SPACED = (
    "The fixture worker checked the generated source and recorded the test result. "
    "The fixture worker did not publish the source or start an external process. "
    "The fixture worker kept the report in the temporary test directory. "
) * 4

# Never glued. Each vector but the reviewer's two carries the runs that would
# draw the warning if the rule it names were gone.
NOT_GLUED = {
    # PR #1378 review: real prose and real identifiers, normally spaced.
    "review-long-words": (
        "The internationalization and characterization tests reproduced the "
        "misconfiguration."
    ),
    "review-identifiers": (
        "The handlers onreadystatechange, onanimationiteration and "
        "onpointerrawupdate are registered."
    ),
    # camelCase names are set aside.
    "camel-case": (
        "Renamed ExternalReferenceResolverRegistry to ReferenceResolver, "
        "ConfigurationRepositoryFactory to SettingsStore and "
        "AuthenticationMiddlewarePipeline to AuthGuard in every module."
    ),
    # Paths are set aside.
    "paths": (
        "Read ~/.local/share/nott/runtime/nmemoryserverentrypoint, "
        "/var/tmp/importancereplaypristinecopy/ and "
        "./tests/fixtures/languagevectorsfixturefile before the change."
    ),
    # A JSON line and quoted strings are set aside.
    "json": (
        "NOTT workflow record\n"
        '{"bound_review": {"bundle_digest": "48f7cb08f0c59f7c91ce725f7fe6e14e", '
        '"covered": ["changelog", "livingdocumentationcheck", '
        '"mutationinventorycheck", "nonewfailuresintroducedcheck"]}}'
    ),
    "inline-json": (
        'The probe answered {"status":"captured","nearduplicatesimilarityhint":'
        '{"similarcapsuleidentifier":"cap-1","score":0.75},'
        '"unanchoredcaptureflag":true} as expected for the batch.'
    ),
    # Capitals and digits are not lowercase letters.
    "capitals-and-timestamps": (
        "Set PYTHONDONTWRITEBYTECODE and PYTHONUNBUFFERED for spill runs "
        "20260906T024004Z95029, 20260906T021218Z89601 and 20260906T064927Z93136."
    ),
    # Hex hashes are set aside.
    "hashes": (
        "Receipts hash 8c0a8f1cc43ec1e61fc5813b5abdeb4c59e9c2f22e8eb85dee18a02394a90103, "
        "775c6d8e2b3838337f4fdbbe50067aa7e52f93e3ccb1a70a308535d457aa7bd9 and "
        "470585528a4ddd30093f13e680d2f7a1d2cbd9071fb73ded07e88e9e414fc16f."
    ),
    # Backticked code is set aside.
    "backticked": (
        "The fixture names `acceptedplannedfeatures`, `receiveexternalapprovalroute` "
        "and `fullmeasurementevidence` as the glued words it expects."
    ),
    # Twenty lowercase letters is still a word: the rule starts at 21.
    "twenty-letter-words": (
        "Spelled right: internationalization, counterrevolutionary and "
        "uncharacteristically."
    ),
    # Two long runs are not enough to judge.
    "two-long-words": (
        "The longest words in the list are antidisestablishmentarianism and "
        "pneumonoultramicroscopic."
    ),
    # One long run repeated is one word, not three.
    "repeated-long-word": (
        "The word antidisestablishmentarianism appears three times here: "
        "antidisestablishmentarianism, and once more antidisestablishmentarianism."
    ),
    # Three long runs in a long text are fewer than one word in twenty.
    "long-words-in-a-long-text": (
        "The longest words in the list are antidisestablishmentarianism, "
        "pneumonoultramicroscopic and floccinaucinihilipilification, and none of "
        "them was used in the change. The proof is the one run that failed before "
        "the fix and passes after it, read from the log of that run and not from "
        "the summary line that the runner prints at the end of it, with the same "
        "store, the same client and the same server on both sides of the change."
    ),
}


def _server() -> MemoryServer:
    return MemoryServer(store=Store.open_disposable_test_store(), project_id="nmemory")


def _body(resp: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in resp, f"the call should not fail as a whole: {resp}"
    body: dict[str, Any] = json.loads(resp["result"]["content"][0]["text"])
    return body


def _item(text: str) -> dict[str, Any]:
    return {
        "content": text,
        "source": "session:2026-10-07",
        "anchor": ANCHOR,
        "kind": "fact",
        "project_id": "nmemory",
    }


def _ingest(text: str) -> dict[str, Any]:
    body = _body(_server().call_tool("nmemory_capsule_ingest", _item(text)))
    assert body["captured"] == 1, f"a valid capture was not stored: {body}"
    row: dict[str, Any] = body["outcomes"][0]
    return row


def test_glued_prose_is_stored_with_a_warning() -> None:
    warning = _ingest(GLUED).get("warning", "")
    assert GLUED.split()[0][:40] in warning, f"it must name a word: {warning}"
    assert "future captures" in warning and "PR #1160" in warning, warning


def test_the_same_capture_spaced_has_no_warning() -> None:
    row = _ingest(SPACED)
    assert "warning" not in row, row


@pytest.mark.parametrize("name", sorted(NOT_GLUED))
def test_code_and_long_words_draw_no_warning(name: str) -> None:
    row = _ingest(NOT_GLUED[name])
    assert "warning" not in row, f"{name}: warned as glued: {row}"


def test_batch_warns_only_the_glued_row() -> None:
    items = [_item(SPACED), _item(GLUED), _item(NOT_GLUED["camel-case"])]
    body = _body(_server().call_tool("nmemory_capsule_ingest", {"items": items}))
    assert body["captured"] == 3, body
    assert ["warning" in row for row in body["outcomes"]] == [False, True, False], body


def test_a_broken_detector_never_costs_the_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_: str) -> list[str] | None:
        raise MemoryError

    monkeypatch.setattr("nmemory.tools.ingest.glued_words", broken)
    row = _ingest(GLUED)
    assert "not checked" in row.get("warning", ""), row


def test_a_long_unspaced_run_is_judged_in_linear_time() -> None:
    # The path pattern once retried a slashless run at every offset: this took
    # about a minute, and every capture runs it twice.
    started = time.perf_counter()
    assert glued_words("x" * 100_000) is None, "one glued run is not three"
    assert time.perf_counter() - started < 2


def _finish(server: MemoryServer, handoff: str) -> dict[str, Any]:
    started = _body(server.call_tool("nmemory_session_start", {}))
    return server.call_tool(
        "nmemory_session_finish",
        {"session_id": started["session_id"], "handoff": handoff},
    )


def test_glued_handoff_is_stored_finished_and_warned() -> None:
    server = _server()
    finished = _finish(server, GLUED)
    body = _body(finished)
    assert not finished["result"].get("isError"), finished
    assert "finished_at" in body, f"the session must finish: {body}"
    warning = body.get("warning", "")
    assert "future handoffs" in warning and "PR #1160" in warning, body
    got = _body(
        server.call_tool("nmemory_capsule_show", {"id": body["handoff_capsule"]})
    )
    assert got["capsule"]["content"] == GLUED, "the handoff must be stored as written"


def test_spaced_handoff_has_no_warning() -> None:
    body = _body(_finish(_server(), SPACED))
    assert "warning" not in body, body


def test_a_broken_detector_never_costs_the_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_: str) -> list[str] | None:
        raise MemoryError

    monkeypatch.setattr("nmemory.tools.session_finish.glued_words", broken)
    body = _body(_finish(_server(), GLUED))
    assert "finished_at" in body, body
    assert "not checked" in body.get("warning", ""), body
