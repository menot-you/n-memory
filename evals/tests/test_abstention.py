"""The `_abs` stop being skipped: admitted, ingested and scored as probes (N4b).

A probe is answered well when the store says `abstain` OR returns nothing
(true_abstain); any return is a false_abstain. A probe never enters the
recall or precision denominator — `scoring_scope` says so.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from conftest import FakeChild, instance, run_pilot

from nmemory_evals import artifacts
from nmemory_evals.score import Abstention, RecallOutcome, is_true_abstain


@pytest.mark.parametrize(
    ("outcome", "returned", "expected"),
    [
        (RecallOutcome.ABSTAIN, [], True),
        (RecallOutcome.MISSING_EVIDENCE, [], True),
        (RecallOutcome.GROUNDED, [["s1"]], False),
        (RecallOutcome.MISSING_EVIDENCE, [["s1"]], False),
    ],
)
def test_a_probe_is_a_true_abstain_on_abstain_or_on_nothing_returned(
    outcome: RecallOutcome, returned: list[list[str]], expected: bool
) -> None:
    assert is_true_abstain(outcome, returned) is expected


def test_probes_are_ingested_scored_and_kept_out_of_recall_and_precision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = FakeChild(
        answers={
            "q1": ("grounded", ["s1"]),
            "p1_abs": ("abstain", []),
            "p2_abs": ("grounded", ["s5"]),
        }
    )
    report = run_pilot(
        tmp_path,
        monkeypatch,
        [
            instance("q1", ["s1"], {"s1": "I moved to Lisbon", "s2": "rain"}),
            instance("p1_abs", ["s3"], {"s3": "I have a cat", "s4": "sun"}),
            instance("p2_abs", ["s5"], {"s5": "I have a dog", "s6": "wind"}),
        ],
        child,
        k_values=[5],
    )

    # the probes are ingested and queried like any other instance
    assert child.calls == {"ingested": 6, "retrieved": 3}
    assert report.abstention == Abstention(true_abstain=1, false_abstain=1)
    counts = report.instances
    assert (counts.sliced, counts.scored, counts.scored_abstention) == (3, 1, 2)
    assert counts.skipped_abstention == 0
    assert counts.largest_category_share == 1.0

    # and they never enter the recall or precision denominator
    (at5,) = report.retrieval_recall
    assert at5.session_recall_any.total == 1
    (p5,) = report.retrieval_precision
    assert (p5.scored, p5.nothing_returned) == (1, 0)
    (category,) = report.per_question_type
    assert category.scored == 1

    # but they stay persisted, in dataset order, with the declared outcome
    assert [(r.question_id, r.outcome) for r in report.scored] == [
        ("q1", RecallOutcome.GROUNDED),
        ("p1_abs", RecallOutcome.ABSTAIN),
        ("p2_abs", RecallOutcome.GROUNDED),
    ]

    results = tmp_path / "results.json"
    artifacts.write_report(results, report)
    data = json.loads(results.read_text(encoding="utf-8"))
    assert data["abstention"] == {"true_abstain": 1, "false_abstain": 1}
    assert data["instances"]["scored_abstention"] == 2
    assert data["instances"]["skipped_abstention"] == 0
    assert len(data["scored"]) == 3


def test_the_abstention_counts_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Abstention(true_abstain=1, false_abstain=0).true_abstain = 0  # type: ignore[misc]


def test_calculate_matches_rust_oracle_only_explicit_abstain_counts() -> None:
    """n1 has a parity contract with the owner's LIVE Rust oracle
    (monorepo mcps/nmemory/evals/src/score.rs:105, checked by the reviewer):
    true_abstain counts ONLY `outcome == abstain`. The empty return counts in
    ARENA A.3 (run_abstain_cell), which is a different definition — never here."""
    from nmemory_evals.score import RecallObservation, RecallOutcome, calculate

    class _Probe:
        def __init__(self, qid: str) -> None:
            self.qid = qid
            self.answerable = False
            self.golden_capsule_ids: list[str] = []
            self.golden_answer = ""

    perguntas = [_Probe("p1_abs"), _Probe("p2_abs")]
    observacoes = [
        RecallObservation(qid="p1_abs", outcome=RecallOutcome.ABSTAIN, result_ids=[]),
        RecallObservation(
            qid="p2_abs", outcome=RecallOutcome.MISSING_EVIDENCE, result_ids=[]
        ),
    ]
    scores = calculate(1, 5, perguntas, observacoes)
    assert scores.true_abstain.abstains == 1  # only the ABSTAIN; empty is not n1
    assert scores.true_abstain.rate == 0.5
