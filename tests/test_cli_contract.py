"""CLI options and typed failures retain the actual memory engine contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import nott_database
import pytest
from storefile import default_store_path, put_embedding

from nmemory import cli
from nmemory.capsule import AuthorityClass, Capsule, Freshness, Provenance, Scope
from nmemory.commands import capsule as capsule_command
from nmemory.embedding import EmbeddingClient
from nmemory.space import DIMENSION, EmbeddingError
from nmemory.store import Store


def _append(store: Store, content: str, project: str, now: datetime) -> str:
    capsule_id, _ = store.append(
        Capsule(
            content=content,
            provenance=Provenance(source="test", anchor=content, source_hash=content),
            confidence=0.9,
            freshness=Freshness(valid_from=now),
            scope=Scope(project_id=project),
            authority_class=AuthorityClass.OBSERVED_FACT,
            instruction_taint=False,
        ),
        now,
    )
    return capsule_id


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "nmemory", *args],
        cwd=cwd,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(cwd),
            "NOTT_DATABASE_DIR": os.environ["NOTT_DATABASE_DIR"],
            "NOTT_NMEMORY_CONFIG": "/nonexistent/nmemory-tests/memory.toml",
            "NOTT_NMEMORY_EMBED_PROVIDER": "openai",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_context_search_defaults_to_git_root_slug_and_explicit_scope_wins(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Client Project"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    nested = root / "different-directory"
    nested.mkdir()
    store = Store.open(default_store_path())
    now = datetime.now(UTC)
    expected = _append(store, "searchmarker repository fact", "client-project", now)
    other = _append(store, "searchmarker other fact", "explicit", now)

    default = _run(["context", "search", "searchmarker", "--source", "memory"], nested)
    assert default.returncode == 0, default.stderr
    response = json.loads(default.stdout)
    assert response["scope"]["project_id"] == "client-project"
    assert {row["id"] for row in response["results"]} == {expected}

    explicit = _run(
        [
            "context",
            "search",
            "searchmarker",
            "--source",
            "memory",
            "--project",
            "explicit",
        ],
        nested,
    )
    assert explicit.returncode == 0, explicit.stderr
    response = json.loads(explicit.stdout)
    assert response["scope"]["project_id"] == "explicit"
    assert {row["id"] for row in response["results"]} == {other}


def test_context_search_outside_git_uses_cwd_slug(tmp_path: Path) -> None:
    cwd = tmp_path / "Local Project"
    cwd.mkdir()
    store = Store.open(default_store_path())
    expected = _append(
        store, "searchmarker local fact", "local-project", datetime.now(UTC)
    )
    result = _run(["context", "search", "searchmarker", "--source", "memory"], cwd)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["scope"]["project_id"] == "local-project"
    assert {row["id"] for row in response["results"]} == {expected}


def test_retrieve_budget_prefix_and_receipt_use_requested_scope(tmp_path: Path) -> None:
    store = Store.open(default_store_path())
    now = datetime.now(UTC)
    expected = {
        _append(store, "budgetmarker parent fact", "team", now),
        _append(store, "budgetmarker child fact", "team/component", now),
    }
    _append(store, "budgetmarker unrelated fact", "other", now)
    args = ["capsule", "retrieve", "budgetmarker", "--project-prefix", "team"]
    result = _run([*args, "--budget", "10000"], tmp_path)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert {row["id"] for row in response["evidence"]} == expected
    assert response["token_budget"] == 10000
    with store.db.connection() as conn:
        row = conn.execute(
            "SELECT project_id, project_prefix, returned_ids FROM recall_receipts "
            "WHERE id = ?",
            (response["receipt_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] is None and row[1] == "team"
    assert set(row[2].split(",")) == expected

    zero = _run([*args, "--budget", "0"], tmp_path)
    assert zero.returncode == 0, zero.stderr
    response = json.loads(zero.stdout)
    assert response["token_budget"] == 0
    assert response["evidence"] == []


def test_retrieve_auto_uses_embedding_client_with_ready_index(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = Store.open(default_store_path())
    queries: list[str] = []
    vector = [1.0] + [0.0] * (DIMENSION - 1)

    def embed(text: str) -> list[float]:
        queries.append(text)
        return vector

    client = EmbeddingClient(custom_hook=embed)
    capsule_id = _append(store, "A stored fact", "team", datetime.now(UTC))
    put_embedding(store.db, capsule_id, vector, client.space_fingerprint.as_str())
    monkeypatch.setattr(
        capsule_command, "EmbeddingClient", lambda: client, raising=False
    )

    with patch.object(
        capsule_command, "retrieve", wraps=capsule_command.retrieve
    ) as call:
        capsule_command.cmd_capsule_retrieve(["semanticmarker", "--project", "team"])

    assert call.call_args.kwargs.get("embeddings") is client
    response = json.loads(capsys.readouterr().out)
    assert response["lanes_run"] == ["term", "vector"]
    assert queries == ["semanticmarker"]
    assert {row["id"] for row in response["evidence"]} == {capsule_id}


def test_retrieve_known_at_observes_actual_supersession_time(tmp_path: Path) -> None:
    store = Store.open(default_store_path())
    now = datetime.now(UTC)
    earlier = now - timedelta(days=2)
    later = now - timedelta(days=1)
    old = _append(store, "revisionmarker previous fact", "team", earlier)
    new = _append(store, "revisionmarker current fact", "team", later)
    store.add_relation("supersedes", new, old, now=later)
    args = ["capsule", "retrieve", "revisionmarker", "--project", "team"]
    current = _run(args, tmp_path)
    assert current.returncode == 0, current.stderr
    assert {row["id"] for row in json.loads(current.stdout)["evidence"]} == {new}
    known_at = (earlier + timedelta(hours=1)).isoformat()
    historical = _run([*args, "--known-at", known_at], tmp_path)
    assert historical.returncode == 0, historical.stderr
    assert {row["id"] for row in json.loads(historical.stdout)["evidence"]} == {old}


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--limit", "-1"),
        ("--budget", "-1"),
        ("--known-at", "invalid-time"),
    ],
)
def test_retrieve_refuses_invalid_options_before_recording(
    tmp_path: Path,
    flag: str,
    value: str,
) -> None:
    store = Store.open(default_store_path())
    _append(store, "limitmarker fact", "team", datetime.now(UTC))
    result = _run(["capsule", "retrieve", "limitmarker", flag, value], tmp_path)
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    with store.db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recall_receipts").fetchone()[0] == 0


def test_capsule_show_tombstone_is_a_domain_refusal(tmp_path: Path) -> None:
    store = Store.open(default_store_path())
    now = datetime.now(UTC)
    capsule_id = _append(store, "Forgotten fact", "team", now)
    store.capsules.forget_capsule(
        capsule_id,
        mode="purged",
        reason="test refusal",
        now=now,
        hmac_key=b"test-only-hmac-key-32-bytes-long!!",
    )
    result = _run(["capsule", "show", "--id", capsule_id], tmp_path)
    assert result.returncode == 2, result.stderr
    assert len(result.stderr.splitlines()) == 1
    assert "tombstoned" in result.stderr and "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "args,cause",
    [
        (
            ["github", "sync", "--config", "/nonexistent/sync.toml"],
            "cannot read config",
        ),
    ],
)
def test_cli_operational_failure_preserves_cause_without_traceback(
    tmp_path: Path,
    args: list[str],
    cause: str,
) -> None:
    default_store_path()
    result = _run(args, tmp_path)
    assert result.returncode == 1, result.stderr
    assert len(result.stderr.splitlines()) == 1
    assert cause in result.stderr and "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "args", [["database", "create"], ["capsule", "retrieve", "test-refusal"]]
)
def test_cli_database_directory_refusal_uses_d18_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setenv("NOTT_DATABASE_DIR", "relative-test-store")
    result = _run(args, tmp_path)
    assert result.returncode == 2, result.stderr
    assert len(result.stderr.splitlines()) == 1
    assert "not an absolute path" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("verb", ["create", "snapshot"])
def test_cli_database_command_preserves_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    verb: str,
) -> None:
    from nmemory.commands import database as database_command

    def refuse(*args: object, **kwargs: object) -> None:
        raise nott_database.RefusedError("disposable database refusal")

    if verb == "create":
        monkeypatch.setattr(database_command, "create_store", refuse)
        arguments = ["database", "create"]
    else:
        monkeypatch.setattr(database_command.Store, "open", refuse)
        arguments = ["database", "snapshot", str(tmp_path / "snapshot")]
    assert cli.main(arguments) == 2
    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "disposable database refusal" in error
    assert "Traceback" not in error


@pytest.mark.parametrize(
    "command,query",
    [
        ("digest", "digest_sidecars"),
        ("digest", "delivery_census"),
        ("digest", "staged_census"),
        ("embedding", "embedding_status"),
    ],
)
def test_cli_post_open_database_refusal_uses_d18_exit_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    query: str,
) -> None:
    from nmemory.commands import digest, embedding

    Store.open(default_store_path())

    def refuse(*args: object, **kwargs: object) -> None:
        raise nott_database.RefusedError("disposable post-open database refusal")

    module = digest if command == "digest" else embedding
    monkeypatch.setattr(module, query, refuse)
    assert cli.main([command, "show"]) == 2
    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "disposable post-open database refusal" in error
    assert "Traceback" not in error


@pytest.mark.parametrize("verb", ["create", "snapshot"])
def test_cli_database_command_keeps_operational_exit_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    verb: str,
) -> None:
    from nmemory.commands import database as database_command

    def fail(*args: object, **kwargs: object) -> None:
        raise nott_database.DatabaseError("disposable operational database failure")

    if verb == "create":
        monkeypatch.setattr(database_command, "create_store", fail)
        arguments = ["database", "create"]
    else:
        monkeypatch.setattr(database_command.Store, "open", fail)
        arguments = ["database", "snapshot", str(tmp_path / "snapshot")]
    with pytest.raises(SystemExit) as stopped:
        cli.main(arguments)
    assert stopped.value.code == 1
    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "disposable operational database failure" in error
    assert "Traceback" not in error


def test_cli_does_not_swallow_programmer_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(args: list[str]) -> None:
        raise TypeError("programmer defect")

    monkeypatch.setitem(cli.COMMANDS, ("capsule", "show"), cli.Command("", "", broken))
    with pytest.raises(TypeError, match="programmer defect"):
        cli.main(["capsule", "show"])


def test_cli_embedding_failure_is_operational(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(args: list[str]) -> None:
        raise EmbeddingError("embedding provider refused")

    monkeypatch.setitem(
        cli.COMMANDS, ("embedding", "show"), cli.Command("", "", broken)
    )
    assert cli.main(["embedding", "show"]) == 1
    assert "embedding provider refused" in capsys.readouterr().err


def test_cli_subprocess_failure_never_prints_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def timed_out(args: list[str]) -> None:
        raise subprocess.TimeoutExpired(["test-program", "private-argument"], 3)

    monkeypatch.setitem(
        cli.COMMANDS, ("context", "search"), cli.Command("", "", timed_out)
    )
    assert cli.main(["context", "search"]) == 1
    error = capsys.readouterr().err
    assert "timed out" in error
    assert "private-argument" not in error
