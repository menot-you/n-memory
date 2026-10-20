"""U3: the hygiene report delivers itself, once per day, through the real door.

Three promises, measured through the CLI that the systemd unit will run:
one capsule per day with measured numbers, no duplication on a second
run on the same day, and a typed refusal (exit 2, named missing store) instead
of a report of zeros when there is no store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from storefile import default_store_path

from nmemory import hygiene
from nmemory.database import Database
from nmemory.server import MemoryServer
from nmemory.store import Store


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        # The test's own store directory: the child never resolves the live one.
        "NOTT_DATABASE_DIR": os.environ["NOTT_DATABASE_DIR"],
        "NOTT_NMEMORY_CONFIG": "/nonexistent/nmemory-hygiene.toml",
    }
    return subprocess.run(
        [sys.executable, "-m", "nmemory", *args, "--project", "pynott"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _hygiene_capsules(url: str, day: str) -> list[str]:
    with Database(url).writing() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT canonical_json ->> '$.content' FROM capsules"
            " WHERE canonical_json IS NOT NULL"
            "   AND canonical_json ->> '$.provenance.source' = ?",
            (f"hygiene:{day}",),
        )
        return [str(row[0]) for row in cur.fetchall()]


@pytest.fixture
def seeded() -> tuple[str, Store]:
    url = default_store_path("hygiene")
    store = Store.open(url)
    server = MemoryServer(store=store, project_id="pynott")
    for index in range(2):
        response = server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": f"Hygiene: seeded observation {index}",
                "kind": "fact",
                "source": "test_hygiene_report",
                "anchor": f"tests/test_hygiene_report.py:{index + 1}",
                "proof_hint": "fixture: the capture gate requires a re-obtainable proof",
            },
        )
        assert not response["result"]["isError"], response
    now = datetime.now(UTC)
    # THE ALARM: queried and nothing came back. Two `abstain`, one
    # `missing_evidence`, all from TODAY.
    for term, outcome in (
        ("hygiene one", "abstain"),
        ("hygiene two", "abstain"),
        ("hygiene three", "missing_evidence"),
    ):
        store.traffic.record_recall_miss(term, outcome, now)
    # `context` served and `context` empty, within the 7-day window.
    for served in (["cap-1"], [], []):
        store.db.record_read_traffic(
            surface="context",
            session_id="hygiene-test",
            terms="hygiene",
            served_ids=served,
            project_id="pynott",
            project_prefix=None,
            outcome=None,
            now=now - timedelta(days=1),
        )
    return url, store


def test_hygiene_writes_one_measured_capsule_per_day_and_does_not_duplicate(
    seeded: tuple[str, Store], tmp_path: Path
) -> None:
    url, _store = seeded
    day = datetime.now(UTC).date().isoformat()

    first = _run_cli(["quality", "measure"])
    assert first.returncode == 0, f"stdout: {first.stdout}\nstderr: {first.stderr}"

    capsules = _hygiene_capsules(url, day)
    assert len(capsules) == 1, capsules
    content = capsules[0]
    # The MEASURED numbers, not generic text.
    assert "abstain 2" in content, content
    assert "missing_evidence 1" in content, content
    assert "2 of 3" in content, content  # `context` empty in 2 of the 3 calls

    second = _run_cli(["quality", "measure"])
    assert second.returncode == 0, f"stdout: {second.stdout}\nstderr: {second.stderr}"
    assert _hygiene_capsules(url, day) == capsules


def test_hygiene_report_is_reobtainable_and_classified_fact(
    seeded: tuple[str, Store], tmp_path: Path
) -> None:
    url, store = seeded
    day = datetime.now(UTC).date().isoformat()

    assert _run_cli(["quality", "measure", "--json"]).returncode == 0

    with Database(url).writing() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM capsules"
            " WHERE canonical_json ->> '$.provenance.source' = ?",
            (f"hygiene:{day}",),
        )
        row = cur.fetchone()
    assert row is not None
    cid = str(row[0])
    classification = store.classifications.get_classification(cid)
    assert classification is not None and classification.kind.value == "fact"
    epistemic = store.epistemics.get_epistemic(cid)
    assert epistemic is not None and epistemic["proof_hint"]
    assert "nmemory" in epistemic["proof_hint"] and "hygiene" in epistemic["proof_hint"]


def test_hygiene_json_carries_the_same_numbers(
    seeded: tuple[str, Store], tmp_path: Path
) -> None:
    url, _store = seeded
    proc = _run_cli(["quality", "measure", "--json", "--dry-run"])
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["abstain"] == 2
    assert payload["missing_evidence"] == 1
    assert payload["context_calls"] == 3
    assert payload["context_empty"] == 2
    # `--dry-run` MEASURES and does not write: the store remains as it was.
    assert _hygiene_capsules(url, datetime.now(UTC).date().isoformat()) == []


def test_hygiene_without_a_store_refuses_naming_the_configuration(
    tmp_path: Path,
) -> None:
    """A missing store is a typed refusal (D18 `2`), never a report of zeros."""
    proc = _run_cli(["quality", "measure"])
    assert proc.returncode == 2, (
        f"expected exit 2, got {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert proc.stderr.startswith("nmemory: quality measure: refused: "), proc.stderr
    assert "missing" in proc.stderr, proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    assert proc.stdout == "", proc.stdout


def test_a_corrida_concorrente_do_mesmo_dia_NAO_escreve_duas_capsulas(
    seeded: tuple[str, Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check INSIDE the transaction is the one that decides, not the outside one.

    Independent review (2026-09-20) caught that the existence SELECT ran
    outside the transaction and in autocommit: two concurrent runs on the same day
    both passed through it empty, and since measured numbers may differ
    between the two measurements, similarity dedup did not join them. The asserted
    idempotence was sequential, not concurrent.

    Here the race is SIMULATED deterministically: the first call to
    `_existente` — the outside one — lies and says there is nothing, exactly as
    would happen if the capsule were written by another run immediately after it.
    Only the second call, already after the store's writer lock, can prevent
    duplication.
    """
    _url, store = seeded
    now = datetime.now(UTC)
    h = hygiene.measure(store, now)

    first_id, written = hygiene.publish(store, h, "pynott", now)
    assert written

    real = hygiene._existente
    chamadas = {"n": 0}

    def cega_a_primeira(s: Store, source: str) -> str | None:
        chamadas["n"] += 1
        return None if chamadas["n"] == 1 else real(s, source)

    monkeypatch.setattr(hygiene, "_existente", cega_a_primeira)
    generation = store.db.get_generation()
    second_id, written_again = hygiene.publish(store, h, "pynott", now)

    assert chamadas["n"] == 2, "the check inside the transaction did not run"
    # Declaring the write and finding the day's capsule changed nothing.
    assert store.db.get_generation() == generation
    assert not written_again
    assert second_id == first_id
