"""The command backend upgrade preserves runs, events, and refusal guards."""

from dataclasses import replace
from pathlib import Path

import nott_database
import pytest

from nspawn.registry import Registry, RegistryError
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.schema import REGISTRY_SCHEMA_VERSION, SCHEMA_DDL


def test_version_six_upgrade_preserves_the_existing_run_and_event_chain(
    tmp_path: Path,
) -> None:
    legacy = (
        SCHEMA_DDL.replace(
            f"PRAGMA user_version = {REGISTRY_SCHEMA_VERSION}",
            "PRAGMA user_version = 6",
        )
        .replace("'command',", "")
        .replace(",'command'", "")
    )
    path = tmp_path / "legacy.sqlite3"
    nott_database.create(path, legacy)
    with Registry(path, "migration-owner") as registry:
        registry.register_instance(1)
        row = RunRow(
            run_id="retained-run",
            worktree=str(tmp_path),
            phase=RunPhase.PENDING,
            sandbox="workspace-write",
            model="retained-model",
            owner_instance="migration-owner",
            created_at=1,
            updated_at=1,
            backend=Backend.CLAUDE,
        )
        registry.insert_run(row)
        registry.append_run_event(
            row.run_id, at=1, kind="lifecycle", payload={"retained": True}
        )
        before = registry.read_run_events(row.run_id)
        guards = nott_database.query(
            registry.connection,
            "SELECT name, sql FROM sqlite_schema WHERE type = 'trigger' ORDER BY name",
        )
        assert registry.upgrade() == 7, "The registry must admit the command backend."
        assert registry.get_run(row.run_id) == row
        assert registry.read_run_events(row.run_id) == before
        assert registry.verify_run_event_chain(row.run_id)
        assert (
            nott_database.query(
                registry.connection,
                "SELECT name, sql FROM sqlite_schema WHERE type = 'trigger' ORDER BY name",
            )
            == guards
        )
        with (
            pytest.raises(nott_database.IntegrityError, match="append-only"),
            nott_database.write(registry.connection),
        ):
            registry.connection.execute("DELETE FROM run_events").close()
        assert registry.database_rollback() == 5
        assert registry.upgrade() == 7
        backend = Backend.parse("command")
        assert backend is not None
        registry.insert_run(replace(row, run_id="command-run", backend=backend))
        with pytest.raises(RegistryError, match="command"):
            registry.database_rollback()
        assert nott_database.user_version(registry.connection) == 7
        assert not nott_database.query(registry.connection, "PRAGMA foreign_key_check")
