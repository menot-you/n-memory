"""Test suite for CLI usage parity (sub-09) and staleness detection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from storefile import default_store_path, sample_capsule_id

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.cli import main
from nmemory.staleness import (
    StalenessClass,
    StalenessVerdict,
    evaluate_all,
    evaluate_condition,
    extract_branch,
    extract_capsule_id,
    extract_file_path,
    extract_git_sha,
    extract_pr_number,
)
from nmemory.store import Store

TEST_DB = ""
NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fresh_store() -> None:
    global TEST_DB
    TEST_DB = str(default_store_path())


def test_staleness_entity_extractors() -> None:
    """Staleness entity extractors correctly recognize PRs, capsules, file paths, branches, and SHAs."""
    assert extract_pr_number("stale when PR #123 merges") == 123
    assert extract_pr_number("stale when #42 closes") == 42
    assert extract_pr_number("stale when #1 is merged") is None  # <2 digits

    assert extract_capsule_id(
        "stale when " + sample_capsule_id(42) + " is superseded"
    ) == sample_capsule_id(42)
    assert extract_capsule_id(
        "stale when " + sample_capsule_id(12345) + " changes"
    ) == sample_capsule_id(12345)
    assert extract_capsule_id("invalid cap_42") is None

    assert extract_file_path("stale if src/lib.rs is deleted") == "src/lib.rs"
    assert (
        extract_file_path("stale if docs/architecture.md is removed")
        == "docs/architecture.md"
    )
    assert extract_file_path("no slash file.rs") is None

    assert extract_branch("stale if feat/wave0 is deleted") == "feat/wave0"
    assert extract_branch("stale if fix/bug-123 is gone") == "fix/bug-123"

    assert extract_git_sha("stale when main advances past 8ab29f1") == "8ab29f1"
    assert (
        extract_git_sha("stale after a1b2c3d4e5f60718293a4b5c6d7e8f9012345678")
        == "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    )


class MockRunner:
    def check_pr(
        self, pr: int, condition: str, capsule_id: str
    ) -> tuple[StalenessVerdict, str | None]:
        if pr == 100:
            return (StalenessVerdict.TRIGGERED, "MERGED")
        return (StalenessVerdict.NOT_TRIGGERED, "OPEN")

    def check_capsule(self, capsule_id: str) -> tuple[StalenessVerdict, str | None]:
        if capsule_id == "cap-1":
            return (StalenessVerdict.TRIGGERED, "superseded")
        return (StalenessVerdict.NOT_TRIGGERED, "active")

    def check_path(
        self, path: str, condition: str
    ) -> tuple[StalenessVerdict, str | None]:
        if "deleted" in condition:
            return (StalenessVerdict.TRIGGERED, "gone")
        return (StalenessVerdict.NOT_TRIGGERED, "still present")

    def check_branch(
        self, branch: str, condition: str
    ) -> tuple[StalenessVerdict, str | None]:
        if "deleted" in condition:
            return (StalenessVerdict.TRIGGERED, "gone")
        return (StalenessVerdict.NOT_TRIGGERED, "still at 1234567")

    def check_sha(
        self, sha: str, _condition: str
    ) -> tuple[StalenessVerdict, str | None]:
        if sha == "8ab29f1":
            return (StalenessVerdict.TRIGGERED, "main advanced to deadbeef")
        return (StalenessVerdict.NOT_TRIGGERED, "still the tip")


def test_staleness_evaluation_matrix() -> None:
    """Staleness evaluator assigns correct classes and verdicts across all supported entities."""
    runner = MockRunner()

    h_pr = evaluate_condition(sample_capsule_id(10), "stale when #100 merges", runner)
    assert h_pr.class_ == StalenessClass.PR
    assert h_pr.verdict == StalenessVerdict.TRIGGERED
    assert h_pr.observed_state == "MERGED"

    h_cap = evaluate_condition(
        sample_capsule_id(11), "stale when cap-1 is superseded", runner
    )
    assert h_cap.class_ == StalenessClass.CAPSULE
    assert h_cap.verdict == StalenessVerdict.TRIGGERED

    h_path = evaluate_condition(
        sample_capsule_id(12), "stale if plugin/decisions.md is deleted", runner
    )
    assert h_path.class_ == StalenessClass.PATH
    assert h_path.verdict == StalenessVerdict.TRIGGERED

    h_prose = evaluate_condition(
        sample_capsule_id(13), "stale when owner changes mind", runner
    )
    assert h_prose.class_ == StalenessClass.PROSE
    assert h_prose.verdict == StalenessVerdict.UNDECIDABLE

    report = evaluate_all(
        [
            (sample_capsule_id(10), "stale when #100 merges"),
            (sample_capsule_id(11), "stale when cap-1 is superseded"),
            (sample_capsule_id(13), "stale when owner changes mind"),
        ],
        runner,
    )
    assert report.total_evaluated == 3
    assert report.resolvable_count == 2
    assert report.prose_count == 1
    assert report.triggered_count == 2
    assert report.undecidable_count == 1


def test_cli_get_command(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI get command returns capsule by ID or handle."""
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(TEST_DB).parent))
    store = Store.open(TEST_DB)
    store.append(
        Capsule(
            content="Get command test capsule",
            provenance=Provenance(
                source="test",
                anchor="test.py:1",
                source_hash=sha256_hex(b"test"),
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=NOW),
            scope=Scope(project_id="nott"),
            authority_class=AuthorityClass.OBSERVED_FACT,
            instruction_taint=False,
        ),
        NOW,
    )

    monkeypatch.setattr("sys.argv", ["nmemory", "capsule", "show", "--id", "cap-1"])
    main()
    out = capsys.readouterr().out
    data: dict[str, Any] = json.loads(out)
    assert data["id"] == "cap-1"
    assert data["content"] == "Get command test capsule"
    assert data["authority_class"] == "observed-fact"


def test_cli_relate_command(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI relate command validates relation kinds and records relations."""
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(TEST_DB).parent))
    store = Store.open(TEST_DB)
    store.append(
        Capsule(
            content="One",
            provenance=Provenance(
                source="test",
                anchor="test.py:1",
                source_hash=sha256_hex(b"1"),
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=NOW),
            scope=Scope(project_id="nott"),
            authority_class=AuthorityClass.OBSERVED_FACT,
            instruction_taint=False,
        ),
        NOW,
    )
    store.append(
        Capsule(
            content="Two",
            provenance=Provenance(
                source="test",
                anchor="test.py:2",
                source_hash=sha256_hex(b"2"),
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=NOW),
            scope=Scope(project_id="nott"),
            authority_class=AuthorityClass.OBSERVED_FACT,
            instruction_taint=False,
        ),
        NOW,
    )

    # Valid relate
    monkeypatch.setattr(
        "sys.argv",
        [
            "nmemory",
            "capsule",
            "relate",
            "--kind",
            "supersedes",
            "--from",
            sample_capsule_id(2),
            "--to",
            "cap-1",
        ],
    )
    main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["outcome"] == "recorded"
    assert data["relation"] == "supersedes"
    assert data["from"] == sample_capsule_id(2)
    assert data["to"] == "cap-1"

    # Invalid kind exits 2
    monkeypatch.setattr(
        "sys.argv",
        [
            "nmemory",
            "capsule",
            "relate",
            "--kind",
            "invalid_kind",
            "--from",
            sample_capsule_id(2),
            "--to",
            "cap-1",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_cli_anchors_command(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI anchors command renders live capsule anchor index array."""
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(TEST_DB).parent))
    store = Store.open(TEST_DB)
    store.append(
        Capsule(
            content="Visible anchor headline\nBody here",
            provenance=Provenance(
                source="test",
                anchor="src/module.py:42",
                source_hash=sha256_hex(b"src"),
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=NOW),
            scope=Scope(project_id="nott"),
            authority_class=AuthorityClass.OBSERVED_FACT,
            instruction_taint=False,
        ),
        NOW,
    )
    store.append(
        Capsule(
            content="Tainted anchor headline\nBody here",
            provenance=Provenance(
                source="test",
                anchor="src/tainted.py:10",
                source_hash=sha256_hex(b"taint"),
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=NOW),
            scope=Scope(project_id="nott"),
            authority_class=AuthorityClass.OBSERVED_FACT,
            instruction_taint=True,
        ),
        NOW,
    )

    monkeypatch.setattr("sys.argv", ["nmemory", "anchor", "list"])
    main()
    out = capsys.readouterr().out
    rows: list[dict[str, Any]] = json.loads(out)
    assert len(rows) == 2
    assert rows[0]["id"] == "cap-1"
    assert rows[0]["anchorPath"] == "src/module.py"
    assert rows[0]["headline"] == "Visible anchor headline…"
    assert rows[0]["instructionTaint"] is False

    assert rows[1]["id"] == sample_capsule_id(2)
    assert rows[1]["anchorPath"] == "src/tainted.py"
    assert "headline" not in rows[1]
    assert rows[1]["instructionTaint"] is True


def test_cli_staleness_command(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI staleness command runs obsolescence checks and outputs plain text or JSON."""
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(TEST_DB).parent))
    store = Store.open(TEST_DB)
    store.append(
        Capsule(
            content="Clean capsule without stale_if",
            provenance=Provenance(
                source="test",
                anchor="clean.py:1",
                source_hash=sha256_hex(b"clean"),
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=NOW),
            scope=Scope(project_id="nott"),
            authority_class=AuthorityClass.OBSERVED_FACT,
            instruction_taint=False,
        ),
        NOW,
    )

    monkeypatch.setattr("sys.argv", ["nmemory", "capsule", "audit"])
    main()
    out = capsys.readouterr().out
    assert "0 condition(s) evaluated across store" in out

    monkeypatch.setattr("sys.argv", ["nmemory", "capsule", "audit", "--json"])
    main()
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert data["total_evaluated"] == 0
    assert data["triggered_count"] == 0


def test_cli_recall_and_digest_flags(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI recall and digest accept flags matching oracle usage."""
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(TEST_DB).parent))
    # Wall clock, not the fixed NOW: recall's read fence (ed48bfbba,
    # DEFAULT_UNREAD_DAYS=30) excludes a capsule nobody read for 30 days of REAL
    # time, so a capsule written at NOW turned `unread` on 2026-09-22.
    now = datetime.now(UTC)
    store = Store.open(TEST_DB)
    store.append(
        Capsule(
            content="Database architecture memory item",
            provenance=Provenance(
                source="test",
                anchor="db.py:1",
                source_hash=sha256_hex(b"db"),
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=now),
            scope=Scope(project_id="nott"),
            authority_class=AuthorityClass.OBSERVED_FACT,
            instruction_taint=False,
        ),
        now,
    )

    # Recall --terms and --limit
    monkeypatch.setattr(
        "sys.argv",
        [
            "nmemory",
            "capsule",
            "retrieve",
            "--terms",
            "architecture,database",
            "--limit",
            "5",
        ],
    )
    main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["outcome"] == "grounded"
    assert len(data["evidence"]) >= 1

    # Digest --headlines
    monkeypatch.setattr("sys.argv", ["nmemory", "digest", "show", "--headlines", "10"])
    main()
    out_d = capsys.readouterr().out
    assert "1 capsules stored in SQLite" in out_d
