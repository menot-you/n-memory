"""Bulk attributions must not grant more than the original session profile."""

import json
from pathlib import Path

import nott_database
import pytest

from nspawn.registry import Registry, RegistryError, compute_chained_hash, create_store
from nspawn.rows import Backend, RunEventRow, RunPhase, RunRow, SessionProfile


@pytest.mark.parametrize(
    "shape",
    ["string", "array", "number", "null", "object-plus-string", "duplicate-member"],
)
def test_run_attributions_rejects_non_object_session_profile(
    tmp_path: Path, shape: str
) -> None:
    path = tmp_path / "registry.db"
    create_store(path)
    profile = SessionProfile("profile-run", True, False, ()).data()
    profiles: dict[str, list[object]] = {
        "string": [json.dumps(profile)],
        "array": [[profile]],
        "number": [42],
        "null": [None],
        "object-plus-string": [profile, json.dumps(profile)],
        "duplicate-member": [],
    }
    with Registry(path, "profile-owner") as registry:
        registry.insert_run(
            RunRow(
                run_id="profile-run",
                thread_id="profile-thread",
                worktree=str(tmp_path),
                phase=RunPhase.RUNNING,
                sandbox="workspace-write",
                model="fixture",
                owner_instance="profile-owner",
                created_at=1,
                updated_at=2,
                backend=Backend.CLAUDE,
            )
        )
        for record in profiles[shape]:
            registry.append_run_event(
                "profile-run",
                kind="lifecycle",
                payload={"session_profile": record},
                at=1000,
            )
        if shape == "duplicate-member":
            payload = (
                '{"session_profile": '
                + json.dumps(profile)
                + ', "session_profile": null}'
            )
            event = RunEventRow(
                "profile-run", 1, 1000, "lifecycle", json.loads(payload), "0" * 64
            )
            chained_hash = compute_chained_hash("", event.canonical_line())
            with registry._transaction():
                nott_database.query(
                    registry.connection,
                    "INSERT INTO run_events(run_id, seq, at, kind, payload, chained_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("profile-run", 1, 1000, "lifecycle", payload, chained_hash),
                )
        with pytest.raises(RegistryError, match="session profile"):
            registry.session_profile("profile-run")
        assert registry.run_attributions(("profile-run",)) == {
            "profile-run": {"run_id": "profile-run", "thread_id": "profile-thread"}
        }
