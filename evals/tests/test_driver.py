"""Unit and protocol tests for the nmemory stdio MCP driver."""

import os
import shlex
import subprocess
import sys
from pathlib import Path

import nott_database
import pytest
from nmemory.tools.ingest import gate_refusal, item_sidecars
from test_shadow_confinement import show_as_foreign

from nmemory_evals import cli, narrate, qa, world
from nmemory_evals.driver import (
    RESPONSE_TIMEOUT,
    SHUTDOWN_GRACE,
    Driver,
    DriverError,
    ingest_request,
    nmemory_command,
    recall_outcome,
    store_command,
    store_directory,
    tool_payload,
    validate_disposable_store_dir,
)
from nmemory_evals.qa import NarratedEvent
from nmemory_evals.score import RecallOutcome
from nmemory_evals.world import WorldConfig

BENCH_STORE = Path("/var/tmp/nmemory-bench/store")


def test_benchmark_children_open_only_the_benchmark_store() -> None:
    cmd, env = nmemory_command(Path("/tmp/nmemory"), BENCH_STORE, "bench", False)
    assert cmd == ["/tmp/nmemory", "mcp", "serve", "--project", "bench"]
    assert (
        env.items()
        >= {
            "NOTT_DATABASE_DIR": str(BENCH_STORE),
            "NOTT_NMEMORY_CONFIG": "/nonexistent/nmemory-evals/memory.toml",
        }.items()
    )


@pytest.mark.parametrize(
    "name",
    ["HOME", "TMPDIR", "XDG_CACHE_HOME", "UV_CACHE_DIR", *nott_database.TEST_MARKERS],
)
def test_store_children_keep_disposable_paths_and_test_markers(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "/var/tmp/private-evaluation")
    _, env = store_command("/unused", BENCH_STORE)
    assert env.get(name) == "/var/tmp/private-evaluation"


@pytest.mark.parametrize(
    "store_dir",
    [
        # Synthetic account path. Validation rejects it before filesystem access.
        "/synthetic/home/test-account/.nott/state/database",
        "/tmp/bench",
        "/var/tmp",
        "/var/tmp/../etc",
        "relative/dir",
    ],
)
def test_a_store_directory_outside_var_tmp_is_refused(store_dir: str) -> None:
    with pytest.raises(DriverError, match="/var/tmp"):
        validate_disposable_store_dir(Path(store_dir))


def test_the_live_store_directory_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    live = Path("/var/tmp/nmemory-bench/pretend-live")
    monkeypatch.setattr(nott_database, "live_directory", lambda: live)
    with pytest.raises(DriverError, match="live store"):
        validate_disposable_store_dir(live)
    assert validate_disposable_store_dir(BENCH_STORE) == BENCH_STORE


def test_a_store_directory_under_an_ancestor_another_account_can_change_is_refused(
    bench_store_dir: Path,
) -> None:
    # Another account that can rename `job` puts a link to the live store parent in
    # its place, and the store cleanup then deletes live files (PR 1928 review, F1).
    shared = bench_store_dir / "shared"
    shared.mkdir(mode=0o700)
    shared.chmod(0o777)
    store = shared / "job" / "database"
    with pytest.raises(DriverError, match=f"{shared}.*writable by group or other"):
        validate_disposable_store_dir(store)
    with pytest.raises(DriverError, match=f"{shared}.*writable by group or other"):
        store_directory(store)
    assert not (shared / "job").exists()
    shared.chmod(0o1777)
    assert validate_disposable_store_dir(store) == store


def test_store_directory_refuses_a_foreign_ancestor_made_after_the_preflight(
    bench_store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The preflight finds `new-job` missing. Another account makes it before
    # `directory_open` makes the rest. The admission after creation refuses it
    # (PR 1928, FM1).
    job = bench_store_dir / "new-job"
    show_as_foreign(monkeypatch, job)
    with pytest.raises(DriverError, match="owned by uid"):
        store_directory(job / "database")


def test_ingest_request_binds_capsule_freshness_to_synthetic_event_time() -> None:
    event = NarratedEvent(
        seq=7,
        valid_from="2026-01-01T01:02:00Z",
        narrative="synthetic event",
        proof_hint="nmemory-evals scenario show --seed 42 --seq 7",
    )
    req = ingest_request(event)
    assert req["arguments"]["valid_from"] == "2026-01-01T01:02:00Z"
    assert req["arguments"]["content"] == "synthetic event"
    assert req["arguments"]["anchor"] == "event:7"
    assert req["arguments"]["proof_hint"] == event.proof_hint


def n1_cases() -> qa.BenchmarkCases:
    config = WorldConfig(seed=42, services=8, engineers=5, events=120)
    return qa.build_cases(world.generate(config), 40, narrate.render, config)


def test_every_n1_ingest_passes_the_nmemory_capture_gate() -> None:
    """The n1 capsules assert fact, so nMEMORY's proof gate (#686) judges them: the
    bench must bring re-obtainable proof, never an invented anchor. This is the
    port's own gate, fed exactly as `handle` feeds it."""
    for event in n1_cases().events:
        item = ingest_request(event)["arguments"]
        kinds, epistemics, quotes, _ = item_sidecars([item])
        assert gate_refusal(item, None, kinds[0], quotes[0], epistemics[0]) is None, (
            item
        )


def test_the_proof_hint_re_derives_the_ingested_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The hint is a command, and running it prints the capsule's content back."""
    for event in n1_cases().events[:3] + n1_cases().events[-1:]:
        argv = shlex.split(event.proof_hint)
        assert argv[:3] == ["nmemory-evals", "scenario", "show"], argv
        assert cli.main(argv[1:]) == 0
        assert capsys.readouterr().out == f"{event.valid_from} {event.narrative}\n"


def test_an_event_the_world_does_not_hold_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["scenario", "show", "--seed", "42", "--seq", "121"]) == 2
    assert "no event 121" in capsys.readouterr().err


def test_a_closing_child_gets_at_least_as_long_as_an_answering_one() -> None:
    assert SHUTDOWN_GRACE == 30.0
    assert SHUTDOWN_GRACE >= RESPONSE_TIMEOUT


def test_bounded_timeout_kills_an_unresponsive_child() -> None:
    cmd = ["/bin/sh", "-c", "while IFS= read -r _line; do :; done"]
    driver = Driver.spawn_command(cmd, response_timeout=0.05)
    with pytest.raises(DriverError, match="timed out"):
        driver.request(9, "fixture/never-responds", {})

    assert driver.child.poll() is not None


def test_tool_payload_contracts() -> None:
    payload = {"grounded": True, "k": 3}
    wrapped = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": '{"grounded": true, "k": 3}'}]},
    }
    assert tool_payload(wrapped) == payload

    with pytest.raises(DriverError, match="boom"):
        tool_payload({"error": {"code": -32000, "message": "boom"}})

    with pytest.raises(DriverError, match="failed"):
        tool_payload(
            {
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "error text"}],
                }
            }
        )

    with pytest.raises(DriverError, match="omitted result"):
        tool_payload({"jsonrpc": "2.0", "id": 1})

    with pytest.raises(DriverError, match="omitted text"):
        tool_payload({"result": {"content": []}})

    str_wrapped = {
        "result": {"content": [{"type": "text", "text": '"not json at all"'}]}
    }
    assert tool_payload(str_wrapped) == "not json at all"

    with pytest.raises(DriverError, match="unparseable"):
        tool_payload({"result": {"content": [{"type": "text", "text": "{ broken"}]}})


def test_recall_outcome_parsing() -> None:
    assert recall_outcome({"outcome": "grounded"}, "q1") == RecallOutcome.GROUNDED
    assert (
        recall_outcome({"outcome": "missing_evidence"}, "q1")
        == RecallOutcome.MISSING_EVIDENCE
    )
    assert recall_outcome({"outcome": "abstain"}, "q1") == RecallOutcome.ABSTAIN

    with pytest.raises(DriverError, match="no declared outcome"):
        recall_outcome({}, "q1")

    with pytest.raises(DriverError, match="unknown outcome"):
        recall_outcome({"outcome": "invalid_val"}, "q1")


def test_eval_children_never_inherit_a_memory_toml_store() -> None:
    """The evals' env HAS to neutralize `~/.nott/memory.toml`.

    The retired PostgreSQL store read the file FIRST: measured 2026-08-29, a
    child with only the env mounted served 2684 capsules from the canonical
    store while the requested one had 0. The store is no longer configured
    there, and the benchmark still keeps the owner's file out of its children.
    """
    _, env = nmemory_command(Path("/tmp/nmemory"), BENCH_STORE, "bench", False)
    override = env.get("NOTT_NMEMORY_CONFIG")
    assert override, (
        "the child needs NOTT_NMEMORY_CONFIG so it does not read the host's memory.toml"
    )
    assert not Path(override).exists(), (
        f"the fence points at a path that exists: {override}"
    )


def test_a_spawned_child_never_reaches_the_ambient_memory_toml(
    tmp_path: Path,
) -> None:
    """The test above checks the ENV; this one drives a real CHILD.

    The two failure modes are different: that one catches the fence being
    absent or pointing at a path that exists; this one catches PRECEDENCE
    shifting — a child that resolved its store from its HOME rather than
    from the directory handed to it. The falsifier plants a sentinel
    `memory.toml` in the child's HOME and demands the benchmark's store back.
    """
    home = tmp_path / "home"
    (home / ".nott").mkdir(parents=True)
    (home / ".nott" / "memory.toml").write_text(
        '[store]\nurl = "sentinel-store-that-must-not-be-opened"\n',
        encoding="utf-8",
    )
    _cmd, env = store_command(Path("/tmp/nmemory"), BENCH_STORE)

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            ("from nmemory.database import store_path;print(store_path())"),
        ],
        env={
            **env,
            "HOME": str(home),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.pathsep.join(sys.path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == str(BENCH_STORE / "nmemory.sqlite3"), child.stdout
