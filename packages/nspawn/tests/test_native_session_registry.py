"""Native history and account isolation through a real registry file."""

from collections.abc import Iterator
from dataclasses import replace

import nott_database
import pytest
from registry_reset import reset_registry, sql, store

from nspawn.registry import Registry, RegistryError, UniqueTaken
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.schema import REGISTRY_SCHEMA_VERSION


@pytest.fixture
def registry() -> Iterator[Registry]:
    with Registry(store(), "native-owner") as registry:
        reset_registry(registry, now=1)
        yield registry


def row(run_id: str, **changes: object) -> RunRow:
    return replace(
        RunRow(
            run_id=run_id,
            worktree="/work/" + run_id,
            phase=RunPhase.PENDING,
            sandbox="workspace-write",
            model="luna",
            owner_instance="native-owner",
            created_at=1,
            updated_at=1,
            backend=Backend.CODEX,
            thread_id="vendor-thread",
            executed_provider="codex",
            executed_account="one",
        ),
        **changes,
    )


def test_completed_native_thread_can_have_another_run(registry: Registry) -> None:
    registry.insert_run(row("old", phase=RunPhase.COMPLETED))
    registry.insert_run(row("new"))
    assert registry.get_run("old").thread_id == registry.get_run("new").thread_id


def test_same_vendor_id_in_different_accounts_and_engines(registry: Registry) -> None:
    registry.insert_run(row("one"))
    registry.insert_run(row("two", executed_account="two"))
    registry.insert_run(row("agy", backend=Backend.AGY))
    assert len(registry.list_runs()) == 3


def test_native_admission_without_context_is_refused(registry: Registry) -> None:
    with pytest.raises(RegistryError, match="context"):
        registry.admit_run(row("missing"), True, 1, 100)
    assert registry.get_run("missing") is None
    assert registry.read_claim("/work/missing") is None


@pytest.fixture
def context():
    from nspawn.rows import NativeSessionContext

    return NativeSessionContext(
        "/accounts/one", "/state/codex", "openai", "NOTT_TEST_API_KEY"
    )


def test_context_and_claim_are_atomic_and_unknown_id_binds_later(registry, context):
    from nspawn.registry import WorktreeHeld

    registry.claim_worktree("/work/refused", "held", 1, 100)
    with pytest.raises(WorktreeHeld):
        registry.admit_run(row("refused"), True, 1, 100, native_context=context)
    assert registry.get_run("refused") is None
    assert (
        sql(registry, "SELECT count(*) AS n FROM native_contexts").fetchone()["n"] == 0
    )
    registry.admit_run(
        row("accepted", thread_id=None), True, 1, 100, native_context=context
    )
    assert registry.resolve_native_session("vendor-thread") is None
    current = registry.get_run("accepted")
    registry.update_run(replace(current, thread_id="vendor-thread"))
    resolved, stored = registry.resolve_native_session("vendor-thread")
    assert resolved.run_id == "accepted" and stored == context
    assert registry.read_claim("/work/accepted").run_id == "accepted"
    assert registry.open(now=2) == REGISTRY_SCHEMA_VERSION
    assert registry.resolve_native_session("vendor-thread")[1] == context


def test_failed_start_rolls_back_context_row_and_claim(registry, context):
    from nspawn.registry import UnknownRun

    with pytest.raises(UnknownRun):
        registry.admit_run(
            row("wrong-owner", owner_instance="foreign"),
            True,
            1,
            100,
            native_context=context,
        )
    assert registry.get_run("wrong-owner") is None
    assert registry.read_claim("/work/wrong-owner") is None
    assert (
        sql(registry, "SELECT count(*) AS n FROM native_contexts").fetchone()["n"] == 0
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"executed_provider": None},
        {"executed_account": None},
        {"backend": Backend.CLAUDE},
    ],
)
def test_context_requires_named_native_origin(registry, context, changes):
    with pytest.raises(RegistryError):
        registry.admit_run(
            row("invalid", **changes), True, 1, 100, native_context=context
        )
    assert registry.get_run("invalid") is None
    assert registry.read_claim("/work/invalid") is None


def test_resolve_latest_and_ambiguity_never_fall_back(registry, context):
    registry.admit_run(row("old"), False, 1, 100, native_context=context)
    registry.update_run(replace(registry.get_run("old"), phase=RunPhase.COMPLETED))
    registry.admit_run(row("new", created_at=2), False, 2, 100, native_context=context)
    assert registry.resolve_native_session("vendor-thread")[0].run_id == "new"
    registry.admit_run(
        row("other", executed_account="two"),
        False,
        3,
        100,
        native_context=replace(context, home="/accounts/two"),
    )
    with pytest.raises(RegistryError, match="ambiguous"):
        registry.resolve_native_session("vendor-thread")
    assert (
        registry.resolve_native_session(
            "vendor-thread", provider="codex", account="one"
        )[0].run_id
        == "new"
    )
    assert (
        registry.resolve_native_session("vendor-thread", account="two")[1].home
        == "/accounts/two"
    )
    for filters in (
        {"provider": "agy"},
        {"account": "absent"},
        {"provider": "other", "account": "one"},
    ):
        with pytest.raises(RegistryError, match="mismatch"):
            registry.resolve_native_session("vendor-thread", **filters)
    assert (
        registry.resolve_native_session("absent", provider="agy", account="other")
        is None
    )


def test_native_id_never_resolves_a_claude_row(registry, context):
    registry.insert_run(row("claude", backend=Backend.CLAUDE))
    assert registry.resolve_native_session("vendor-thread") is None
    registry.admit_run(row("native"), False, 1, 100, native_context=context)
    assert registry.resolve_native_session("vendor-thread")[0].run_id == "native"


def test_missing_historical_context_is_refused(registry):
    registry.insert_run(row("historical", phase=RunPhase.COMPLETED))
    with pytest.raises(RegistryError, match="lacks account context"):
        registry.resolve_native_session("vendor-thread")


@pytest.mark.parametrize("phase", list(RunPhase))
def test_only_finished_native_runs_release_the_thread(registry, phase):
    registry.insert_run(row("previous", phase=phase))
    if phase in (RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CLOSED):
        registry.insert_run(row("next"))
        assert registry.get_run("next") is not None
    else:
        with pytest.raises(nott_database.IntegrityError):
            registry.insert_run(row("next"))
        assert registry.get_run("next") is None


@pytest.mark.parametrize("missing", ["executed_provider", "executed_account"])
def test_a_native_thread_without_its_whole_origin_is_refused(registry, missing):
    # The active-thread index is unique only over non-null origins, so a null
    # origin is refused outright rather than left free to duplicate.
    with pytest.raises(nott_database.IntegrityError, match="CHECK"):
        registry.insert_run(row("unknown", **{missing: None}))
    assert registry.get_run("unknown") is None


def test_vendor_callback_cannot_bind_an_already_active_thread(registry, context):
    registry.admit_run(row("bound"), False, 1, 100, native_context=context)
    registry.admit_run(
        row("waiting", thread_id=None), False, 1, 100, native_context=context
    )
    with pytest.raises(nott_database.IntegrityError):
        registry.update_run(
            replace(registry.get_run("waiting"), thread_id="vendor-thread")
        )
    assert registry.get_run("waiting").thread_id is None


def test_two_connections_cannot_admit_the_same_active_origin(registry, context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    def admit(name):
        with Registry(registry.path, name) as rival:
            rival.open(now=1)
            barrier.wait(timeout=5)
            try:
                rival.admit_run(
                    row(name, owner_instance=name), True, 1, 100, native_context=context
                )
            except UniqueTaken:
                return False
            return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, ["first", "second"]))
    assert sorted(results) == [False, True]
    for table in ("runs", "claims", "native_contexts"):
        assert sql(registry, f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 1


def test_claude_threads_remain_globally_unique_after_completion(registry):
    registry.insert_run(row("claude", backend=Backend.CLAUDE, phase=RunPhase.COMPLETED))
    with pytest.raises(nott_database.IntegrityError):
        registry.insert_run(
            row("other", backend=Backend.CLAUDE, executed_account="two")
        )
    with pytest.raises(nott_database.IntegrityError):
        registry.insert_run(row("grok", backend=Backend.GROK))


@pytest.mark.parametrize(
    "field,value",
    [
        ("home", "relative"),
        ("home", "/bad\npath"),
        ("home", "/" + "a" * 4096),
        ("sqlite_home", "relative"),
        ("sqlite_home", "/bad\tpath"),
        ("model_provider", "secret/token"),
        ("model_provider", "a" * 64),
        ("auth_env", "sk-secret"),
        ("auth_env", "1BAD"),
        ("auth_env", "A" * 64),
    ],
)
def test_database_rejects_contexts_that_bypass_constructor(registry, field, value):
    registry.insert_run(row("ctx"))
    values = {
        "home": "/accounts/one",
        "sqlite_home": None,
        "model_provider": None,
        "auth_env": None,
    }
    values[field] = value
    with pytest.raises(nott_database.IntegrityError):
        sql(
            registry,
            "INSERT INTO native_contexts(run_id, home, sqlite_home, model_provider, auth_env) "
            "VALUES (?,?,?,?,?)",
            ("ctx", *values.values()),
        )
    assert (
        sql(registry, "SELECT count(*) AS n FROM native_contexts").fetchone()["n"] == 0
    )


@pytest.mark.parametrize("backend", [Backend.CODEX, Backend.AGY])
@pytest.mark.parametrize("coordinate", ["executed_provider", "executed_account"])
@pytest.mark.parametrize("spoof_backend", [False, True])
def test_native_update_cannot_change_its_origin(
    registry, context, backend, coordinate, spoof_backend
):
    registry.admit_run(
        row("native", backend=backend), True, 1, 100, native_context=context
    )
    current = registry.get_run("native")
    changed = replace(current, **{coordinate: "other"})
    if spoof_backend:
        changed = replace(changed, backend=Backend.CLAUDE)
    with pytest.raises(RegistryError, match="native.*origin.*immutable"):
        registry.update_run(changed, drop_claim=True)
    assert registry.get_run("native") == current
    assert registry.read_claim("/work/native").run_id == "native"
    assert registry.resolve_native_session("vendor-thread")[1] == context


def test_legacy_claude_update_keeps_account_failover(registry):
    registry.insert_run(row("legacy", backend=Backend.CLAUDE))
    current = registry.get_run("legacy")
    changed = replace(
        current,
        executed_provider="other",
        executed_account="two",
        phase=RunPhase.RUNNING,
    )
    registry.update_run(changed)
    assert registry.get_run("legacy") == changed


@pytest.mark.parametrize("ended", [RunPhase.FAILED, RunPhase.COMPLETED])
def test_finished_claude_run_releases_legacy_thread_key_on_new_active_update(
    registry, ended
):
    registry.insert_run(
        row(
            "prior-failed",
            backend=Backend.CLAUDE,
            phase=ended,
            thread_id="321d4263-1620-4166-a07d-efdc9e5a12ba",
        )
    )
    registry.insert_run(
        row(
            "resumed-active",
            backend=Backend.CLAUDE,
            phase=RunPhase.PENDING,
            thread_id=None,
        )
    )
    current = registry.get_run("resumed-active")
    registry.update_run(
        replace(current, thread_id="321d4263-1620-4166-a07d-efdc9e5a12ba")
    )
    assert registry.get_run("prior-failed").phase is RunPhase.CLOSED
    assert (
        registry.get_run("resumed-active").thread_id
        == "321d4263-1620-4166-a07d-efdc9e5a12ba"
    )


def test_finished_claude_run_with_remaining_claim_keeps_thread_fenced(registry):
    registry.insert_run(
        row(
            "claimed-failed",
            backend=Backend.CLAUDE,
            phase=RunPhase.FAILED,
            thread_id="321d4263-1620-4166-a07d-efdc9e5a12ba",
        )
    )
    registry.claim_worktree("/work/claimed-failed", "claimed-failed", 1, 100)
    registry.insert_run(
        row(
            "resumed-active",
            backend=Backend.CLAUDE,
            phase=RunPhase.PENDING,
            thread_id=None,
        )
    )
    with pytest.raises(nott_database.IntegrityError):
        registry.update_run(
            replace(
                registry.get_run("resumed-active"),
                thread_id="321d4263-1620-4166-a07d-efdc9e5a12ba",
            )
        )
    assert registry.get_run("claimed-failed").phase is RunPhase.FAILED


def test_finished_claude_run_with_legacy_process_group_keeps_thread_fenced(registry):
    registry.insert_run(
        row(
            "grouped-failed",
            backend=Backend.CLAUDE,
            phase=RunPhase.FAILED,
            process_group=12345,
            thread_id="321d4263-1620-4166-a07d-efdc9e5a12ba",
        )
    )
    registry.insert_run(
        row(
            "resumed-active",
            backend=Backend.CLAUDE,
            phase=RunPhase.PENDING,
            thread_id=None,
        )
    )
    with pytest.raises(nott_database.IntegrityError):
        registry.update_run(
            replace(
                registry.get_run("resumed-active"),
                thread_id="321d4263-1620-4166-a07d-efdc9e5a12ba",
            )
        )
    assert registry.get_run("grouped-failed").phase is RunPhase.FAILED
