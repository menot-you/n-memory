"""Unit tests for metrics, score, recall, and artifact writers."""

import hashlib
import tempfile
from pathlib import Path

import pytest

from nmemory_evals.artifacts import (
    ConfigArtifact,
    file_sha256,
    write_bundle,
    write_report,
)
from nmemory_evals.recall import (
    ScoredQuestion,
    by_category,
    table,
)
from nmemory_evals.score import (
    AbstainRate,
    HitRate,
    QuestionCounts,
    RecallObservation,
    RecallOutcome,
    ScoreError,
    Scores,
    calculate,
    rate,
)


class MockQuestion:
    def __init__(
        self,
        qid: str,
        answerable: bool,
        golden_capsule_ids: list[str],
        golden_answer: str = "ans",
    ) -> None:
        self.qid = qid
        self.answerable = answerable
        self.golden_capsule_ids = golden_capsule_ids
        self.golden_answer = golden_answer


def test_scores_hit_true_abstain_and_false_abstain_separately() -> None:
    # Synthetic IDs distinguish the two answerable questions.
    questions = [
        MockQuestion("q-1", True, ["cap-1"]),
        MockQuestion("q-2", True, ["synthetic-capsule-b"]),
        MockQuestion("q-3", False, []),
    ]
    observations = [
        RecallObservation(
            qid="q-1",
            outcome=RecallOutcome.GROUNDED,
            result_ids=["cap-1"],
        ),
        RecallObservation(
            qid="q-2",
            outcome=RecallOutcome.ABSTAIN,
            result_ids=[],
        ),
        RecallObservation(
            qid="q-3",
            outcome=RecallOutcome.ABSTAIN,
            result_ids=[],
        ),
    ]
    scores = calculate(42, 3, questions, observations)
    assert scores.grounded_hit_at_k.hits == 1
    assert scores.true_abstain.abstains == 1
    assert scores.false_abstain.abstains == 1
    assert scores.headline == "true_abstain.rate"
    assert scores.grounded_hit_at_k.rate == pytest.approx(0.5)
    assert scores.true_abstain.rate == pytest.approx(1.0)
    assert scores.false_abstain.rate == pytest.approx(0.5)


def test_score_errors_on_duplicate_or_missing_observations() -> None:
    questions = [MockQuestion("q-1", True, ["cap-1"])]
    with pytest.raises(ScoreError, match="duplicate"):
        calculate(
            42,
            1,
            questions,
            [
                RecallObservation("q-1", RecallOutcome.GROUNDED, ["cap-1"]),
                RecallObservation("q-1", RecallOutcome.GROUNDED, ["cap-1"]),
            ],
        )

    with pytest.raises(ScoreError, match="missing"):
        calculate(42, 1, questions, [])


def test_rate_empty_denominator() -> None:
    assert rate(0, 0) == 0.0
    assert rate(5, 10) == 0.5


def test_any_and_all_diverge_on_a_multi_evidence_question() -> None:
    q = ScoredQuestion(
        question_id="q-multi-6",
        question_type="multi-session",
        evidence_sessions=["a", "b"],
        ranked_sessions=[["a"], ["x"], ["y"], ["z"], ["w"], ["b"]],
    )
    assert q.recall_any(5) is True
    assert q.recall_all(5) is False
    assert q.recall_all(6) is True
    assert q.recall_all(10) is True


def test_the_cut_at_k_is_the_rank_and_not_the_returned_list() -> None:
    q = ScoredQuestion(
        question_id="q-single-6",
        question_type="single-session-user",
        evidence_sessions=["a"],
        ranked_sessions=[["x"], ["y"], ["z"], ["w"], ["v"], ["a"]],
    )
    assert q.recall_any(5) is False
    assert q.recall_any(10) is True


def test_a_question_with_no_evidence_never_counts_as_recalled() -> None:
    q = ScoredQuestion(
        question_id="q-zero-1",
        question_type="single-session-user",
        evidence_sessions=[],
        ranked_sessions=[["a"]],
    )
    assert q.recall_any(5) is False
    assert q.recall_all(5) is False


def test_a_capsule_carrying_two_sessions_recalls_both() -> None:
    q = ScoredQuestion(
        question_id="q-dedup",
        question_type="single-session-user",
        evidence_sessions=["a"],
        ranked_sessions=[["a", "b"]],
    )
    assert q.recall_any(1) is True
    assert q.recall_all(1) is True


def test_the_table_reports_one_row_per_declared_k() -> None:
    questions = [
        ScoredQuestion("q-1", "single-session-user", ["a"], [["a"]]),
        ScoredQuestion(
            "q-2",
            "multi-session",
            ["b", "c"],
            [["b"], ["x"], ["y"], ["z"], ["w"], ["c"]],
        ),
    ]
    t = table(questions, [5, 10])
    assert len(t) == 2
    assert t[0].k == 5
    assert t[0].session_recall_any.hits == 2
    assert t[0].session_recall_all.hits == 1
    assert t[0].session_recall_any.total == 2
    assert t[0].session_recall_all.rate == pytest.approx(0.5)
    assert t[1].k == 10
    assert t[1].session_recall_all.hits == 2


def test_categories_keep_first_appearance_order_and_their_own_denominators() -> None:
    questions = [
        ScoredQuestion("q-1", "multi-session", ["b", "c"], [["b"]]),
        ScoredQuestion("q-2", "single-session-user", ["a"], [["a"]]),
        ScoredQuestion("q-3", "multi-session", ["d"], [["d"]]),
    ]
    categories = by_category(questions, [5])
    assert len(categories) == 2
    assert categories[0].question_type == "multi-session"
    assert categories[0].scored == 2
    assert categories[0].recall[0].session_recall_any.hits == 2
    assert categories[0].recall[0].session_recall_all.hits == 1
    assert categories[1].question_type == "single-session-user"
    assert categories[1].scored == 1


def test_file_sha256_streaming() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "large.bin"
        data = b"0123456789abcdef" * 10000
        test_file.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert file_sha256(test_file) == expected


def test_write_bundle_and_report() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "bundle"
        cfg = ConfigArtifact(
            bench="n1",
            seed=42,
            binary_path="/usr/bin/nmemory",
            binary_sha256="abc",
            services=10,
            engineers=5,
            events=20,
            questions=10,
            oow_questions=2,
            k=5,
            bench_version="0.1.0",
        )
        scores = Scores(
            bench="n1",
            seed=42,
            k=5,
            questions=QuestionCounts(8, 2),
            grounded_hit_at_k=HitRate(8, 8, 1.0),
            true_abstain=AbstainRate(2, 2, 1.0),
            false_abstain=AbstainRate(0, 8, 0.0),
            headline="true_abstain.rate",
        )
        write_bundle(
            out=out,
            config=cfg,
            world={"entities": {}, "rules": [], "events": []},
            questions=[{"qid": "q-001"}],
            transcript=[{"dir": "request", "elapsed_ms": 12, "frame": {}}],
            scores=scores,
        )

        assert (out / "config.json").read_text().endswith("\n")
        assert (out / "world.json").read_text().endswith("\n")
        assert (out / "qa.json").read_text().endswith("\n")
        assert (out / "transcript.jsonl").read_text().endswith("\n")
        assert (out / "scores.json").read_text().endswith("\n")

        rep_path = Path(tmpdir) / "report.json"
        write_report(rep_path, {"test": 123})
        assert rep_path.read_text().endswith("\n")
