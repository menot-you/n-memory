"""Determinism conformance tests for the n1 generation path."""

import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from nmemory_evals import narrate, qa, world
from nmemory_evals.qa import QuestionTemplate
from nmemory_evals.world import EventKind, WorldConfig


def fixture() -> WorldConfig:
    return WorldConfig(
        seed=42,
        services=8,
        engineers=5,
        events=120,
    )


def deterministic_capsules(event_count: int) -> dict[int, str]:
    return {seq: f"cap-{seq}" for seq in range(1, event_count + 1)}


def test_same_seed_reproduces_world_and_programmatic_golden_bytes() -> None:
    first = world.generate(fixture())
    second = world.generate(fixture())

    assert [rule.id for rule in first.rules] == ["R1", "R2", "R3", "R4", "R5"]
    assert first == second

    first_qa = qa.generate(first, 40)
    second_qa = qa.generate(second, 40)
    qa.resolve_capsule_ids(first_qa, deterministic_capsules(len(first.events)))
    qa.resolve_capsule_ids(second_qa, deterministic_capsules(len(second.events)))

    assert len(first_qa) == 40
    assert first_qa == second_qa


def test_injectable_narration_changes_cases_without_changing_goldens() -> None:
    w = world.generate(fixture())
    baseline = qa.build_cases(w, 40, narrate.render, fixture())
    rephrased = qa.build_cases(
        w,
        40,
        lambda event: f"Rephrased through production seam: {narrate.render(event)}",
        fixture(),
    )

    assert len(baseline.events) == len(w.events)
    assert all(
        case.seq == event.seq and case.valid_from == event.at
        for case, event in zip(baseline.events, w.events, strict=True)
    )
    assert all(
        before.narrative != after.narrative
        for before, after in zip(baseline.events, rephrased.events, strict=True)
    )

    before_answers = [q.golden_answer for q in baseline.questions]
    after_answers = [q.golden_answer for q in rephrased.questions]
    assert before_answers == after_answers
    assert baseline.questions == rephrased.questions


def test_generated_events_obey_all_five_world_rules() -> None:
    w = world.generate(fixture())
    deploy_by_id = {}
    newest_owner_by_service = {}
    last_version_by_service = {}

    for event in w.events:
        service_number = int(event.service.removeprefix("svc-"))
        assert service_number < 900, f"R5: {event.id}"
        assert event.service in w.entities.services
        assert event.engineer in w.entities.engineers
        assert event.at.startswith("2026-")

        match event.kind:
            case EventKind.DEPLOY:
                version_num = int(event.version.removeprefix("v").removesuffix(".0.0"))
                previous = last_version_by_service.get(event.service, 0)
                assert version_num > previous, "R1 increasing semver"
                last_version_by_service[event.service] = version_num
                deploy_by_id[event.id] = event
            case EventKind.INCIDENT_OPEN:
                causing_id = event.caused_by
                assert causing_id is not None, "R2 causing deploy"
                causing = deploy_by_id.get(causing_id)
                assert causing is not None, "R2 prior deploy"
                assert causing.service == event.service
                assert causing.version == event.version
            case EventKind.OWNERSHIP_TRANSFER:
                newest_owner_by_service[event.service] = event
            case EventKind.RESOLVE:
                assert event.incident is not None, "R4 incident name"
                assert bool(event.engineer), "R4 resolving engineer"

    questions = qa.generate(w, 40)
    assert any(
        q.template == QuestionTemplate.WHO_DEPLOYED
        and q.terms[2:] == ["deploy", "deployed"]
        for q in questions
    )
    assert any(
        q.template == QuestionTemplate.WHICH_DEPLOY_CAUSED
        and q.terms[2:] == ["incident", "caused"]
        for q in questions
    )
    assert any(
        q.template == QuestionTemplate.WHO_RESOLVED
        and q.terms[2:] == ["resolved", "resolution"]
        for q in questions
    )

    for q in questions:
        if q.template == QuestionTemplate.CURRENT_OWNER:
            service = q.terms[0]
            newest = newest_owner_by_service.get(service)
            assert newest is not None, "R3 newest transfer"
            assert q.golden_event_seqs == [newest.seq]
            assert q.golden_answer == newest.engineer

    out_of_world = [q for q in questions if q.template == QuestionTemplate.OUT_OF_WORLD]
    assert len(out_of_world) == 8
    assert all(
        not q.answerable
        and q.golden_answer == "abstain"
        and q.golden_event_seqs == []
        and q.terms[0].startswith("svc-9")
        for q in out_of_world
    )


def tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens, split on non-alphanumeric chars."""
    return {tok for tok in re.split(r"[^a-zA-Z0-9]+", text.lower()) if tok}


def test_the_paraphrase_case_shares_no_token_with_its_gold_capsule() -> None:
    w = world.generate(fixture())
    questions = qa.generate(w, 40)
    paraphrased = [
        q for q in questions if q.template == QuestionTemplate.PARAPHRASED_OWNER
    ]
    assert len(paraphrased) == 1
    q = paraphrased[0]
    assert q.answerable

    newest_transfer = next(
        event
        for event in reversed(w.events)
        if event.kind == EventKind.OWNERSHIP_TRANSFER
    )
    assert q.golden_event_seqs == [newest_transfer.seq]
    assert q.golden_answer == newest_transfer.engineer

    gold = tokens(narrate.render(newest_transfer))
    for text in [q.question] + q.terms:
        shared = tokens(text).intersection(gold)
        assert not shared, (
            f"the term lane reaches the golden capsule through {shared} in {text}"
        )


def test_generation_paths_contain_no_wall_clock_reads() -> None:
    pkg_dir = Path(__file__).parent.parent / "nmemory_evals"
    sources = [
        (pkg_dir / "world.py").read_text(encoding="utf-8"),
        (pkg_dir / "narrate.py").read_text(encoding="utf-8"),
        (pkg_dir / "qa.py").read_text(encoding="utf-8"),
    ]
    combined = "\n".join(sources)
    banned = [
        "time.time",
        "datetime.now",
        "datetime.utcnow",
        "time.monotonic",
        "time.localtime",
    ]
    for pattern in banned:
        assert pattern not in combined, (
            f"Banned clock read {pattern} found in generation path"
        )


def test_default_binary_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nmemory_evals.main import BenchError, default_binary

    # 1. NOTT_NMEMORY_BIN override
    custom = tmp_path / "custom_bin"
    monkeypatch.setenv("NOTT_NMEMORY_BIN", str(custom))
    assert default_binary() == custom

    # 2. PATH resolution
    monkeypatch.delenv("NOTT_NMEMORY_BIN", raising=False)
    monkeypatch.setattr(
        "shutil.which", lambda cmd: "/bin/nmemory" if cmd == "nmemory" else None
    )
    assert default_binary() == Path("/bin/nmemory")

    # 3. Missing binary raises BenchError
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    with pytest.raises(BenchError, match="is not on PATH"):
        default_binary()


def test_n1_cli_validation_errors() -> None:
    from nmemory_evals.cli import main

    assert main(["scenario", "run", "-k", "0"]) == 1
    assert main(["scenario", "run", "--questions", "2"]) == 1


def test_main_e1_dispatch_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from nmemory_evals.cli import main

    monkeypatch.setattr("nmemory_evals.main.run_e1", lambda args: Path("/dev/null"))
    assert main(["capsule", "score", "-k", "5"]) == 0


def test_run_e1_branches(
    monkeypatch: pytest.MonkeyPatch, outside_var_tmp: Callable[[str], Path]
) -> None:
    monkeypatch.setenv("NOTT_DATABASE_DIR", "/var/tmp/nmemory-bench-mock-store")
    import argparse
    import shutil

    from nmemory_evals.main import BenchError, run_e1

    monkeypatch.setattr(
        "nmemory_evals.e1.run_e1_driver",
        lambda **kwargs: type("Obj", (), {"to_dict": lambda s: {"total": 1}})(),
    )

    # 1. out is None branch
    res1 = run_e1(argparse.Namespace(binary=None, lane="term", k=1, out=None))
    assert res1 == Path("/dev/stdout")

    # 2. out is valid /var/tmp path branch
    valid_out = Path(f"/var/tmp/nmemory-bench/test_e1_branch_out-{os.getpid()}")
    if valid_out.exists():
        shutil.rmtree(valid_out)
    res2 = run_e1(argparse.Namespace(binary=None, lane="term", k=1, out=valid_out))
    assert res2.is_file()

    # 3. out is invalid (not in /var/tmp)
    with pytest.raises(BenchError, match="must be a new descendant"):
        run_e1(
            argparse.Namespace(
                binary=None, lane="term", k=1, out=outside_var_tmp("not_var")
            )
        )


def test_main_pilot_dispatch_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from nmemory_evals.cli import main

    monkeypatch.setattr(
        "nmemory_evals.main.run_longmemeval", lambda **kwargs: Path("/dev/null")
    )
    assert main(["suite", "run", "--config", "c.json", "--data", "d.json"]) == 0
