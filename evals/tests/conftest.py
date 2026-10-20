"""A fake nMEMORY child, to run `Pilot.run()` in process.

The pilot talks to the binary through `Driver.spawn(...).request(...)` and
loads each haystack with `Pilot.seed_sessions`; swapping both for this fake
covers `run`, `measure_per_instance` and `measure_single` without a store or
the binary, and lets the test dictate what each question returns.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from nmemory.renamed import RENAMED as MEMORY_RENAMED
from nott_system1 import SCOPES, route_variables
from nott_system1.renamed import RENAMED as SYSTEM1_RENAMED
from nott_system1.renamed import RETIRED as SYSTEM1_RETIRED
from workspace_root import WORKSPACE_ROOT

from nmemory_evals import artifacts, pilot
from nmemory_evals.longmemeval import Instance
from nmemory_evals.pilot import DatasetPin, PilotConfig
from nmemory_evals.renamed import RENAMED

STAMP = "2023/05/20 (Sat) 02:21"

#: The host's production identity: the live store's keys, its scope, the session a live
#: client runs under, and the paid System1 key. A box that serves
#: nMEMORY exports them in its login shell, and a test run from that shell inherited
#: them — it, and every child it spawns. A case that needs one sets it itself.
HOST_IDENTITY = (
    "NOTT_NMEMORY_HMAC_KEY",
    "NOTT_NMEMORY_HMAC_KEY_FILE",
    "NOTT_NMEMORY_PROJECT",
    "NOTT_NMEMORY_HARNESS",
    "NOTT_NMEMORY_MODEL",
    "NOTT_NMEMORY_SESSION_ID",
    *(name for scope in (None, *sorted(SCOPES)) for name in route_variables(scope)),
)


@pytest.fixture(scope="session", autouse=True)
def _ensure_nmemory_bin_on_path() -> None:
    cand = WORKSPACE_ROOT / ".venv" / "bin" / "nmemory"
    if cand.is_file():
        if "NOTT_NMEMORY_BIN" not in os.environ:
            os.environ["NOTT_NMEMORY_BIN"] = str(cand)
        if str(cand.parent) not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = f"{cand.parent}:{os.environ.get('PATH', '')}"


@dataclass
class FakeChild:
    """Seeds, and answers scope/relate/retrieve, as `answers` dictates per question.

    `answers[qid] = (outcome, session_ids_devolvidos)`. A seeded session's
    capsule id is `cap:session/<id>`, so that retrieve returns ids the pilot
    knows; retrieve keys on the `topic_id` (`cap:scope/<qid>`), and therefore
    serves both per_instance and phased, where all scopes are born before any
    retrieve.
    """

    answers: dict[str, tuple[str, list[str]]]
    calls: dict[str, int] = field(
        default_factory=lambda: {"ingested": 0, "retrieved": 0}
    )

    def spawn(self, *_: Any) -> FakeChild:
        return self

    def handshake(self) -> None:
        pass

    def finish(self) -> None:
        pass

    def seed_sessions(
        self, instance: Instance, _project_id: str, observed: pilot.Observed
    ) -> dict[str, list[str]]:
        by_capsule: dict[str, list[str]] = {}
        for session in instance.sessions():
            self.calls["ingested"] += 1
            observed.sessions_ingested += 1
            observed.capsules_captured += 1
            capsule_id = f"cap:session/{session.session_id}"
            by_capsule.setdefault(capsule_id, []).append(session.session_id)
        return by_capsule

    def request(
        self, id_num: int, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        args = params["arguments"]
        payload: dict[str, Any]
        match params["name"]:
            case "nmemory_capsule_ingest":
                capsule_id = f"cap:{args['anchor']}"
                payload = {"outcomes": [{"status": "captured", "id": capsule_id}]}
            case "nmemory_relation_add":
                payload = {}
            case "nmemory_capsule_retrieve":
                outcome, sessions = self.answers[
                    args["topic_id"].removeprefix("cap:scope/")
                ]
                self.calls["retrieved"] += 1
                payload = {
                    "outcome": outcome,
                    "lane_used": "term",
                    "results": [{"id": f"cap:session/{s}"} for s in sessions],
                }
            case other:
                raise AssertionError(f"unexpected tool {other}")
        return {"result": {"content": [{"text": json.dumps(payload)}]}}


def instance(
    qid: str,
    evidence: list[str],
    sessions: dict[str, str],
    question: str = "what did I say",
    question_type: str = "single-session-user",
) -> dict[str, Any]:
    ids = list(sessions)
    return {
        "question_id": qid,
        "question_type": question_type,
        "question": question,
        "answer_session_ids": evidence,
        "haystack_dates": [STAMP] * len(ids),
        "haystack_session_ids": ids,
        "haystack_sessions": [
            [{"role": "user", "content": text}] for text in sessions.values()
        ],
    }


def pilot_dataset(
    tmp_path: Path,
    instances: list[dict[str, Any]],
    k_values: list[int],
    execution: str = "per_instance",
) -> tuple[Path, PilotConfig]:
    """The instances as a dataset file, and a term-lane config that pins it."""
    dataset = tmp_path / "lme.json"
    dataset.write_text(json.dumps(instances), encoding="utf-8")
    config = PilotConfig(
        bench="longmemeval-s-retrieval",
        dataset=DatasetPin(
            repository="test",
            file="lme.json",
            sha256=artifacts.file_sha256(dataset),
            bytes=dataset.stat().st_size,
            license="MIT",
        ),
        slice_first_instances=len(instances),
        granularity="session",
        lane="term",
        k_values=k_values,
        token_budget=1000,
        project_id="lme-test",
        term_extraction="alnum runs",
        scoring_scope="test",
        execution=execution,
    )
    return dataset, config


def run_pilot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    instances: list[dict[str, Any]],
    child: FakeChild,
    k_values: list[int],
    execution: str = "per_instance",
) -> pilot.Report:
    dataset, config = pilot_dataset(tmp_path, instances, k_values, execution)
    binary = tmp_path / "nmemory"
    binary.write_bytes(b"not a binary")
    monkeypatch.delenv("NOTT_NMEMORY_EVALS_REUSE", raising=False)
    monkeypatch.delenv("NOTT_NMEMORY_EVALS_RUN_ID", raising=False)
    monkeypatch.setattr(pilot.driver, "prepare_database", lambda *_: None)
    monkeypatch.setattr(pilot, "Driver", child)
    monkeypatch.setattr(pilot.Pilot, "seed_sessions", child.seed_sessions)
    return pilot.Pilot(
        config,
        dataset,
        binary,
        tmp_path / "run",
        Path(os.environ["NOTT_DATABASE_DIR"]),
    ).run()


def _outside_var_tmp(name: str) -> Path:
    """A path that is NEVER a descendant of `/var/tmp`, whatever the `TMPDIR`.

    The `--out` refusals (`prepare_output`, `run_e1`, `run_extract_eval`,
    `compare`) only fire outside `/var/tmp`, and four tests were building that
    "outside" from `tmp_path`. pytest derives `tmp_path` from `TMPDIR`, and
    the python gate exports `TMPDIR=/var/tmp/p.XXXX` — inside `/var/tmp`, so the
    path was VALID and the refusal correctly did not fire. The four tests
    went red under the gate and green by hand, over sound bytes (measured
    2026-09-16).

    `/tmp` is never a descendant of `/var/tmp`, and `test_outside_var_tmp_is_really_outside`
    pins that against the real predicate.
    """
    return Path("/tmp/nmemory-evals-outside") / name


@pytest.fixture
def default_store() -> Path:
    """The store `open_live_store()` reads, created empty under the test's own directory."""
    from nmemory.database import create_store, store_path

    path = store_path()
    create_store(path)
    return path


@pytest.fixture
def bench_store_dir() -> Iterator[Path]:
    """A fresh benchmark store directory under /var/tmp, whatever the `TMPDIR`.

    The driver only accepts a /var/tmp subdirectory, so this is made there
    rather than from `tmp_path`, and removed afterwards.
    """
    path = Path(tempfile.mkdtemp(prefix="nmemory-bench-store-", dir="/var/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def outside_var_tmp() -> Callable[[str], Path]:
    """The helper as a FIXTURE, not as an import.

    `evals/nmemory` has no `tests/__init__.py`, so `tests` is only a
    NAMESPACE package; another workspace member had a REGULAR package
    with the same name. Under the gate's `uv sync --all-packages` both enter
    `sys.path`, and by PEP 420 the regular one wins: `from tests.conftest import …`
    would load the other member's conftest and collection of these four files
    would die (revision 31876474). A fixture does not care.
    """
    return _outside_var_tmp


@pytest.fixture(autouse=True)
def _no_old_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    # A host that still exports a renamed variable's old spelling makes every read
    # refuse by name; the suite measures the evals, not the host's migration. Neither
    # spelling of the host's identity reaches a test or the `nmemory` child it spawns.
    # A case that needs one sets it itself.
    for old in (
        *RENAMED.values(),
        *MEMORY_RENAMED.values(),
        *SYSTEM1_RENAMED.values(),
        *SYSTEM1_RETIRED,
        *HOST_IDENTITY,
    ):
        monkeypatch.delenv(old, raising=False)
