"""The daily route reviews today's capsules and unresolved older System1 decisions."""

from datetime import timedelta
from pathlib import Path

import pytest
from storefile import default_store_path
from test_curator import T0, _ctx, _fresh, _ingest, _repo

from nmemory import system1
from nmemory.curator import CuratorClient, CuratorVerdict, CuratorWorker
from nmemory.database import Database
from nmemory.queries.curator import curator_daily_candidates


def test_daily_revisits_old_ambiguity_and_today_once(tmp_path: Path) -> None:
    _, store, server = _fresh()
    old = _ingest(server, "old ambiguous a returns one", "src/a.py:1")
    unrelated = _ingest(server, "old unambiguous a returns one", "src/a.py:1")
    store.reviews.record_system1_observation(
        old,
        question=system1.QUESTION_CURATOR,
        subject="changed_anchor at src/a.py:1",
        choice=system1.NENHUMA,
        p=0.5,
        confidence=0.2,
        model="system1-t",
        now=T0,
    )
    today = T0 + timedelta(days=2)
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": "today a returns one",
            "kind": "fact",
            "source": "test",
            "anchor": "src/a.py:1",
            "proof_hint": "read src/a.py",
            "project_id": "cur",
        },
        context=_ctx(),
        now=today,
    )
    assert not response["result"]["isError"]
    seen: list[str] = []

    def judge(prompt: str) -> CuratorVerdict:
        seen.append(prompt)
        return CuratorVerdict("holds", "the function returns one", None)

    worker = CuratorWorker(
        store, CuratorClient(custom_hook=judge), root=_repo(tmp_path), project="cur"
    )
    assert worker.daily_cycle(now=today) == 2
    assert len(seen) == 2
    assert any("old ambiguous" in p for p in seen)
    assert any("today a returns" in p for p in seen)
    assert not any("old unambiguous" in p for p in seen)
    assert worker.daily_cycle(now=today) == 0
    assert len(seen) == 2
    assert store.anchors.list_verifications(unrelated) == []


def test_daily_cli_is_gated_and_reaches_the_real_store(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    from datetime import datetime

    from nmemory import cli, curator
    from nmemory.commands import review as review_command

    _, store, server = _fresh(default_store_path())  # the store the CLI opens
    cid = _ingest(server, "the CLI sees a returning one", "src/a.py:1")
    root = _repo(tmp_path)
    monkeypatch.setenv("NOTT_NMEMORY_CURATOR_ROOT", str(root))
    monkeypatch.setattr(
        "sys.argv", ["nmemory", "review", "run", "--daily", "--project", "cur"]
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return T0

    monkeypatch.setattr(review_command, "datetime", Clock)
    monkeypatch.setenv("NOTT_NMEMORY_CURATOR_CRON", "0")
    cli.main()
    assert "disabled" in capsys.readouterr().out
    assert store.anchors.list_verifications(cid) == []

    def judge_under_project_lock(_: str) -> CuratorVerdict:
        with Database(store.db.path).daily_curator_owner("cur") as owner:
            assert not owner, "daily lock released before judgement"
        return CuratorVerdict("holds", "read the file", None)

    client = CuratorClient(custom_hook=judge_under_project_lock)
    monkeypatch.setattr(curator, "CuratorClient", lambda: client)
    monkeypatch.setenv("NOTT_NMEMORY_CURATOR_CRON", "1")
    cli.main()
    assert "reviewed 1" in capsys.readouterr().out
    assert store.anchors.list_verifications(cid)[-1].result == "holds"

    import pytest

    from nmemory.curator import CuratorError

    _ingest(server, "a second capsule whose provider fails", "src/a.py:1")

    def fail(_: str) -> CuratorVerdict:
        raise CuratorError("provider offline")

    client.custom_hook = fail
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert "daily review failed" in capsys.readouterr().err


def test_daily_catches_captures_after_yesterdays_pass(tmp_path: Path) -> None:
    import json

    _, store, server = _fresh()
    worker = CuratorWorker(
        store,
        CuratorClient(
            custom_hook=lambda _: CuratorVerdict("holds", "still true", None)
        ),
        root=_repo(tmp_path),
        project="cur",
    )
    assert worker.daily_cycle(now=T0) == 0
    late = T0 + timedelta(hours=1)
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": "captured after yesterday's daily pass",
            "kind": "fact",
            "source": "late",
            "anchor": "src/a.py:1",
            "proof_hint": "read a.py",
        },
        context=_ctx(),
        now=late,
    )
    cid = json.loads(response["result"]["content"][0]["text"])["outcomes"][0]["id"]
    assert worker.daily_cycle(now=T0 + timedelta(days=2)) == 1
    assert store.anchors.list_verifications(cid)[-1].result == "holds"


def test_daily_operational_failure_is_not_a_successful_count(tmp_path: Path) -> None:
    import pytest

    from nmemory.curator import CuratorError

    _, store, server = _fresh()
    cid = _ingest(server, "a() returns one", "src/a.py:1")

    def unavailable(_: str) -> CuratorVerdict:
        raise CuratorError("provider unavailable")

    worker = CuratorWorker(
        store,
        CuratorClient(custom_hook=unavailable),
        root=_repo(tmp_path),
        project="cur",
    )
    with pytest.raises(CuratorError, match="daily"):
        worker.daily_cycle(now=T0)
    worker.client.custom_hook = lambda _: CuratorVerdict("holds", "recovered", None)
    assert worker.daily_cycle(now=T0) == 1
    assert store.anchors.list_verifications(cid)[-1].result == "holds"


def test_daily_resolves_old_staged_ambiguity_without_a_file_anchor(
    monkeypatch, tmp_path: Path
) -> None:
    import json

    _, store, server = _fresh()
    cid = _ingest(server, "a concrete memory proposal", "session:origin", staged=True)
    store.reviews.record_system1_observation(
        cid,
        question=system1.QUESTION_STAGED,
        subject=system1.staged_subject(cid, "a concrete memory proposal"),
        choice=system1.NAO_DECIDE,
        p=0.5,
        confidence=None,
        model="system1-t",
        now=T0,
    )
    client = CuratorClient(
        custom_hook=lambda _: CuratorVerdict("holds", "unused", None)
    )
    seen: list[str] = []

    def provider(prompt: str, schema: str) -> str:
        seen.append(prompt)
        assert "ratified" in schema and "pending" in schema
        return json.dumps(
            {
                "structured_output": {
                    "verdict": "ratified",
                    "reason": "concrete and verifiable",
                }
            }
        )

    monkeypatch.setattr(client, "_invoke", provider)
    worker = CuratorWorker(store, client, root=_repo(tmp_path), project="cur")
    assert worker.daily_cycle(now=T0 + timedelta(days=2)) == 1
    assert store.reviews.review_verdict(cid) == "ratified"
    assert len(seen) == 1 and "a concrete memory proposal" in seen[0]
    assert worker.daily_cycle(now=T0 + timedelta(days=2)) == 0


def test_partial_daily_retry_next_day_does_not_repay_completed(tmp_path: Path) -> None:
    import pytest

    from nmemory.curator import CuratorError

    _, store, server = _fresh()
    done = _ingest(server, "successful capsule returns one", "src/a.py:1")
    failed = _ingest(server, "failing capsule returns one", "src/a.py:1")
    store.audit.append_audit(
        "curator", "curator_daily_pass", "cur", "completed", T0 - timedelta(days=1)
    )

    def judge(prompt: str) -> CuratorVerdict:
        if "failing capsule" in prompt:
            raise CuratorError("provider failed")
        return CuratorVerdict("holds", "read proof", None)

    worker = CuratorWorker(
        store, CuratorClient(custom_hook=judge), root=_repo(tmp_path), project="cur"
    )
    with pytest.raises(CuratorError):
        worker.daily_cycle(now=T0)
    assert store.anchors.list_verifications(done)[-1].result == "holds"
    assert curator_daily_candidates(store.db, "cur", T0 + timedelta(days=1)) == [failed]


def test_daily_includes_fractional_first_second_of_day() -> None:
    import json

    _, store, server = _fresh()
    midnight = T0.replace(hour=0, minute=0, second=0, microsecond=0)
    result = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": "captured just after midnight",
            "kind": "fact",
            "source": "test",
            "anchor": "src/a.py:1",
        },
        context=_ctx(),
        now=midnight + timedelta(microseconds=1),
    )
    cid = json.loads(result["result"]["content"][0]["text"])["outcomes"][0]["id"]
    assert cid in curator_daily_candidates(
        store.db, "cur", midnight + timedelta(seconds=2)
    )


def test_daily_completion_belongs_to_its_call_not_latest_global_event(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _, store, server = _fresh()
    cid = _ingest(server, "a valid fact followed by a mechanical scan", "src/a.py:1")
    worker = CuratorWorker(
        store,
        CuratorClient(
            custom_hook=lambda _: CuratorVerdict("holds", "read proof", None)
        ),
        root=_repo(tmp_path),
        project="cur",
    )
    original = worker._judge_one

    def judge_then_scan(*args, **kwargs):
        result = original(*args, **kwargs)
        store.anchors.record_verification(
            cid,
            kind="anchor",
            result="changed_anchor",
            detail="scan landed after daily judgement",
            actor="anchors",
            now=T0,
        )
        return result

    monkeypatch.setattr(worker, "_judge_one", judge_then_scan)
    assert worker.daily_cycle(now=T0) == 1
    assert curator_daily_candidates(store.db, "cur", T0) == []


def test_staged_completion_does_not_clear_an_anchor_question(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _, store, server = _fresh()
    cid = _ingest(
        server, "a staged fact with an unresolved anchor", "src/a.py:1", staged=True
    )
    for question in (system1.QUESTION_STAGED, system1.QUESTION_CURATOR):
        store.reviews.record_system1_observation(
            cid,
            question=question,
            subject="old ambiguity",
            choice=system1.NENHUMA,
            p=0.5,
            confidence=0.2,
            model="system1-t",
            now=T0,
        )
    client = CuratorClient(
        custom_hook=lambda _: CuratorVerdict("holds", "read source", None)
    )
    monkeypatch.setattr(
        client, "review_staged", lambda _: ("ratified", "concrete fact")
    )
    worker = CuratorWorker(store, client, root=_repo(tmp_path), project="cur")
    tomorrow = T0 + timedelta(days=1)
    assert worker.daily_cycle(now=tomorrow) == 1
    assert store.reviews.review_verdict(cid) == "ratified"
    next_day = tomorrow + timedelta(days=1)
    assert cid in curator_daily_candidates(store.db, "cur", next_day)
    assert worker.daily_cycle(now=next_day) == 1
    assert store.anchors.list_verifications(cid)[-1].result == "holds"
    assert curator_daily_candidates(store.db, "cur", next_day + timedelta(days=1)) == []


def test_daily_pending_can_resolve_when_evidence_or_judgement_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _, store, server = _fresh()
    missing = _ingest(server, "a fact awaiting a readable anchor", "session:origin")
    staged = _ingest(
        server, "a proposal awaiting a decisive review", "session:proposal", staged=True
    )
    client = CuratorClient(
        custom_hook=lambda _: CuratorVerdict("holds", "evidence is readable now", None)
    )
    monkeypatch.setattr(
        client, "review_staged", lambda _: ("pending", "not enough evidence yet")
    )
    worker = CuratorWorker(store, client, root=_repo(tmp_path), project="cur")
    assert worker.daily_cycle(now=T0) == 1
    assert store.reviews.review_verdict(staged) == "proposed"
    store.anchors.reanchor(
        missing, "src/a.py:1", "owner supplied current source", T0 + timedelta(hours=1)
    )
    monkeypatch.setattr(
        client, "review_staged", lambda _: ("ratified", "review is now decisive")
    )
    assert worker.daily_cycle(now=T0 + timedelta(days=1)) == 2
    assert store.anchors.list_verifications(missing)[-1].result == "holds"
    assert store.reviews.review_verdict(staged) == "ratified"


@pytest.mark.parametrize("expired", [False, True])
def test_resolved_staged_ambiguity_does_not_reenter_daily_queue(
    tmp_path: Path, expired: bool
) -> None:
    _, store, server = _fresh()
    cid = _ingest(server, "resolved staged fact", "src/a.py:1", staged=True)
    store.reviews.record_system1_observation(
        cid,
        question=system1.QUESTION_STAGED,
        subject="unresolved proposal",
        choice=system1.NENHUMA,
        p=0.5,
        confidence=0.2,
        model="system1-t",
        now=T0,
    )
    resolved_at = T0 + timedelta(days=15)
    if expired:
        assert cid in store.expire_staged_proposals(
            "cur", subtree=False, now=resolved_at, actor="curator"
        )
    else:
        response = server.call_tool(
            "nmemory_proposal_review",
            {
                "capsule_id": cid,
                "verdict": "ratified",
                "actor": "owner",
                "reason": "checked original evidence",
            },
            context=_ctx(),
            now=resolved_at,
        )
        assert "error" not in response, response
    seen: list[str] = []

    def judge(prompt: str) -> CuratorVerdict:
        seen.append(prompt)
        return CuratorVerdict("holds", "read source", None)

    root = _repo(tmp_path / str(expired))
    worker = CuratorWorker(
        store, CuratorClient(custom_hook=judge), root=root, project="cur"
    )
    for days in (16, 17, 18):
        worker.daily_cycle(now=T0 + timedelta(days=days))
    assert len(seen) <= 1
    assert cid not in curator_daily_candidates(store.db, "cur", T0 + timedelta(days=19))


def test_daily_keeps_repository_scan_proposals_for_explicit_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store, server = _fresh()
    root = _repo(tmp_path)
    server.anchor_root = str(root)
    scan = _ingest(
        server,
        "The source defines a function a.",
        "src/a.py:1",
        source="repository_scan",
        source_quote="def a():",
        derived_from=["def a():"],
        authority_class="agent-inferred",
        staged=True,
    )
    ordinary = _ingest(server, "Function a returns one.", "src/a.py:2", staged=True)
    assert store.epistemics.get_source_quote(scan)["verdict"] == "verified"
    client = CuratorClient(
        custom_hook=lambda _: CuratorVerdict("holds", "Source checked.", None)
    )
    prompts: list[str] = []

    def ratify(prompt: str) -> tuple[str, str]:
        prompts.append(prompt)
        return "ratified", "Source checked."

    monkeypatch.setattr(client, "review_staged", ratify)
    worker = CuratorWorker(store, client, root=root, project="cur")
    reviewed = worker.daily_cycle(now=T0)
    assert store.reviews.review_verdict(scan) == "proposed"
    assert store.reviews.review_verdict(ordinary) == "ratified"
    assert reviewed == 1
    assert len(prompts) == 1
    assert "Function a returns one." in prompts[0]
    assert worker.daily_cycle(now=T0 + timedelta(days=1)) == 0
    assert store.reviews.review_verdict(scan) == "proposed"
