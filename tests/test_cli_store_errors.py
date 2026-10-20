"""Tests verifying store-opening CLI verbs fail cleanly with a one-line error.

The store-opening CLI verbs -- anchor list and embedding show -- must answer with a typed one-liner starting with
`nmemory: <verb>: `, never a raw Python traceback. A missing store is a typed
refusal (D18 2); an invalid database or unsupported schema is operational (D18 1).
`nmemory database create` is the cure for the first, and it never overwrites an
existing file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nmemory.database import STORE_FILE, Database, create_store

VERBS: list[tuple[list[str], str]] = [
    (["anchor", "list"], "anchor list"),
    (["embedding", "show"], "embedding show"),
]


def _run_cli(
    cmd: list[str], store_dir: Path, tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "NOTT_DATABASE_DIR": str(store_dir),
        "NOTT_NMEMORY_CONFIG": "/nonexistent/nmemory-tests/memory.toml",
    }

    return subprocess.run(
        [sys.executable, "-m", "nmemory", *cmd],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _assert_one_line_refusal(
    proc: subprocess.CompletedProcess[str], verb_name: str, *, exit_code: int = 1
) -> None:
    assert proc.returncode == exit_code, (
        f"expected exit {exit_code} for {verb_name}, got {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert lines[0].startswith(f"nmemory: {verb_name}: "), lines[0]
    assert "Traceback" not in proc.stderr, proc.stderr


def _empty_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "store"
    folder.mkdir(mode=0o700)
    return folder


@pytest.mark.parametrize("cmd,verb_name", VERBS)
def test_store_opening_verbs_refuse_a_missing_store(
    cmd: list[str], verb_name: str, tmp_path: Path
) -> None:
    store_dir = _empty_dir(tmp_path)
    proc = _run_cli(cmd, store_dir, tmp_path)
    _assert_one_line_refusal(proc, verb_name, exit_code=2)
    assert f"nmemory: {verb_name}: refused: " in proc.stderr
    assert "missing" in proc.stderr
    assert not (store_dir / STORE_FILE).exists(), "a verb created the store"


@pytest.mark.parametrize("cmd,verb_name", VERBS)
def test_store_opening_verbs_refuse_a_file_that_is_not_a_store(
    cmd: list[str], verb_name: str, tmp_path: Path
) -> None:
    store_dir = _empty_dir(tmp_path)
    target = store_dir / STORE_FILE
    target.write_bytes(b"not a database" * 64)
    target.chmod(0o600)
    _assert_one_line_refusal(_run_cli(cmd, store_dir, tmp_path), verb_name)


def test_embeddings_status_refuses_a_store_of_another_schema(tmp_path: Path) -> None:
    store_dir = _empty_dir(tmp_path)
    target = store_dir / STORE_FILE
    create_store(target)
    with Database(target).writing() as conn:
        conn.execute("PRAGMA query_only = OFF")
        conn.execute("PRAGMA user_version = 99")
    proc = _run_cli(["embedding", "show"], store_dir, tmp_path)
    _assert_one_line_refusal(proc, "embedding show")
    assert "v99" in proc.stderr


def test_database_create_is_the_cure_and_never_overwrites(tmp_path: Path) -> None:
    store_dir = _empty_dir(tmp_path)
    created = _run_cli(["database", "create"], store_dir, tmp_path)
    assert created.returncode == 0, created.stderr
    status = _run_cli(["embedding", "show"], store_dir, tmp_path)
    assert status.returncode == 0, status.stderr

    again = _run_cli(["database", "create"], store_dir, tmp_path)
    assert again.returncode == 2, again.stdout
    assert "nmemory: database create: refused: " in again.stderr
    assert "already exists" in again.stderr
