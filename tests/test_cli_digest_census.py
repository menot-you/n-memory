"""The CLI census reads a real subtree without opening a write-capable store."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import nott_database
import pytest
from child_env import child_env
from nott_database import DatabaseError
from storefile import default_store_path, sample_capsule_id

from nmemory.commands import digest as digest_command
from nmemory.commands.common import open_store_from_args
from nmemory.database import Database
from nmemory.queries.sidecars import CapsuleSidecar, digest_sidecars
from nmemory.server import MemoryServer
from nmemory.store import Store


def test_digest_counts_the_subtree_and_read_census_cannot_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = default_store_path("clidigest")
    store = Store.open(url)
    server = MemoryServer(store=store)
    for project in ("a", "a/child", "ab"):
        response = server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": f"Census project {project}",
                "project_id": project,
                "source": "test_cli_digest_census",
                "anchor": project,
                "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
                "kind": "fact",
            },
        )
        assert not response["result"]["isError"], response
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "nmemory.cli",
            "digest",
            "show",
            "--project-prefix",
            "a",
        ],
        env=child_env({**os.environ, "NOTT_DATABASE_DIR": str(Path(url).parent)}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "2 capsules stored in SQLite" in result.stdout

    readonly = open_store_from_args(
        argparse.Namespace(command="digest"), read_census=True, path=url
    )
    capsule = store.capsules.get("cap-1")
    assert capsule is not None
    with pytest.raises(DatabaseError, match="read-only"):
        readonly.append(capsule.capsule, datetime.now(UTC))
    assert digest_sidecars(readonly.db).keys() == digest_sidecars(store.db).keys()


def test_digest_prints_recent_kind_counts_in_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nmemory.cli import main

    url = default_store_path("clidigestrecent")
    server = MemoryServer(store=Store.open(url))
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": "Recent orientation",
            "source": "test",
            "anchor": "recent",
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
            "kind": "fact",
            "project_id": "recent",
        },
    )
    assert not response["result"]["isError"], response
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(url).parent))
    monkeypatch.setattr(
        sys, "argv", ["nmemory", "digest", "show", "--project-prefix", "recent"]
    )
    main()
    output = capsys.readouterr()
    assert output.err == ""
    assert "1 capsules written · 1 never recalled (100%)" in output.out
    assert "written/never recalled: fact 1/1" in output.out


@pytest.mark.parametrize("error_type", [DatabaseError, nott_database.Error, OSError])
def test_digest_reports_census_failure_without_provider_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_type: type[Exception],
) -> None:
    from nmemory.cli import main

    url = default_store_path("clidigestfailure")
    Store.open(url)
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(url).parent))
    monkeypatch.setattr(sys, "argv", ["nmemory", "digest", "show"])

    def fail_query(
        db: Database, *, project_prefix: str | None = None
    ) -> dict[str, CapsuleSidecar]:
        raise error_type("provider detail must not reach stderr")

    monkeypatch.setattr(digest_command, "digest_sidecars", fail_query)
    with pytest.raises(SystemExit) as failure:
        main()
    assert failure.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"nmemory: digest: {error_type.__name__}\n"


def test_o_digest_serve_a_fila_staged_com_o_seu_horizonte(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The staged queue census without a consumer was machinery: a measured number
    that nobody reads caps nothing. This is the real consumer — the output that
    every session reads on startup, next to the delivery yardstick — and that is what makes
    "the queue never grows without a cap" observable instead of aspirational.

    The line separates `awaiting` from `expired` and `curator evidence`, because
    treating the three as a single number was the tradeoff that erased evidence to
    lower the count.
    """
    import json
    from datetime import timedelta

    from nmemory.cli import main
    from nmemory.curator import CURATOR_ACTOR
    from nmemory.queries.staged import (
        REVIEW_REASON_EVIDENCE,
        REVIEW_REASON_PROPOSAL,
        STAGED_HORIZON_DAYS,
    )

    url = default_store_path("clistaged")
    store = Store.open(url)
    server = MemoryServer(store=store)
    now = datetime.now(UTC)
    ids: list[str] = []
    for marca in ("velha", "nova", "prova"):
        response = server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": f"proposta {marca} da fila staged",
                "source": "test",
                "anchor": f"fila/{marca}",
                "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
                "kind": "fact",
                "project_id": "fila",
            },
        )
        assert not response["result"]["isError"], response
        payload = json.loads(response["result"]["content"][0]["text"])
        ids.append(str(payload["outcomes"][0]["id"]))
    velha, nova, prova = ids
    store.reviews.append_review_event(
        velha,
        "proposed",
        REVIEW_REASON_PROPOSAL,
        "judge",
        now - timedelta(days=STAGED_HORIZON_DAYS + 3),
    )
    store.reviews.append_review_event(
        nova, "proposed", REVIEW_REASON_PROPOSAL, "judge", now - timedelta(days=1)
    )
    store.reviews.append_review_event(
        prova, "proposed", REVIEW_REASON_EVIDENCE, CURATOR_ACTOR, now
    )
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(url).parent))
    monkeypatch.setattr(
        sys, "argv", ["nmemory", "digest", "show", "--project-prefix", "fila"]
    )
    main()
    output = capsys.readouterr()
    assert output.err == ""
    assert (
        f"staged queue: 2 awaiting judgement · 1 expired (>{STAGED_HORIZON_DAYS} d)"
        in output.out
    ), output.out
    assert "1 curator evidence (fenced by design)" in output.out, output.out


def test_digest_keeps_rewrites_out_of_the_headline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Measured 2026-09-26: a translation sweep of 1821 capsules, each superseding
    an older one, read as 60% unread knowledge. A rewrite is counted on its own
    line, not in the headline."""
    from nmemory.cli import main
    from nmemory.database import Database

    url = default_store_path("clidigestrewrite")
    store = Store.open(url)
    server = MemoryServer(store=store)

    def ingest(content: str, **extra: object) -> str:
        response = server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": content,
                "source": "test",
                "anchor": content,
                "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
                "kind": "fact",
                "project_id": "sweep",
                **extra,
            },
        )
        assert not response["result"]["isError"], response
        return str(response["result"]["content"][0]["text"])

    for index in range(2):
        ingest(f"Old wording number {index}")
    with Database(url).writing() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE capsules SET created_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-30 days')"
        )
    for index in range(3):
        ingest(f"Organic observation number {index}")
    for index, old in enumerate(("cap-1", sample_capsule_id(2))):
        ingest(f"English wording number {index}", supersedes=old)
    store.traffic.record_recall(sample_capsule_id(3), datetime.now(UTC))
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(url).parent))
    monkeypatch.setattr(
        sys, "argv", ["nmemory", "digest", "show", "--project-prefix", "sweep"]
    )
    main()
    out = capsys.readouterr().out
    assert "3 capsules written · 2 never recalled (67%)" in out, out
    assert "2 rewrites (each supersedes an older capsule) · 2 never served" in out, out
