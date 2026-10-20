"""The effective execution class is OBSERVED by the System1, never self-declared.

Every leg drives `resolve_execution_class` with a double of `system1.ask` — the
module wrapper, exactly as `test_mcp_contract.py::_system1_duplo` does. A test that
reached the live service would bill a call per run and read a vendor 422 as a
product verdict.
"""

from __future__ import annotations

from typing import Any

import pytest

from nspawn import classify, system1
from nspawn.owner_roster import UNCLASSIFIED, ClassPolicy, Roster, System1

#: The host table of the campaign: the three cheap classes carry a strength cap,
#: the expensive ones carry none.
_CLASSES = {
    "deterministic": ClassPolicy(effort="low", ceiling="low", max_strength=7),
    "mechanical_fanout": ClassPolicy(effort="low", ceiling="medium", max_strength=7),
    "unclassified": ClassPolicy(effort="low", ceiling="medium", max_strength=7),
    "verification": ClassPolicy(effort="medium", ceiling="high"),
    "review": ClassPolicy(effort="medium", ceiling="high"),
    "design": ClassPolicy(effort="medium", ceiling="high"),
    "planning": ClassPolicy(effort="high", ceiling="ultra"),
    "judgment": ClassPolicy(effort="high", ceiling="ultra"),
}


def _roster(classes: dict[str, ClassPolicy] | None = None) -> Roster:
    return Roster(classes=dict(_CLASSES if classes is None else classes))


class _System1:
    """A double of `system1.ask` that records every call it receives."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self.timeouts: list[float | None] = []

    def __call__(
        self,
        state: Any,
        questions: dict[str, Any],
        model: Any = None,
        timeout: float | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((state, questions))
        self.timeouts.append(timeout)
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer is None:
            return {}
        ident = next(iter(questions))
        if isinstance(self.answer, system1.Answer):
            return {ident: self.answer}
        value, confidence = self.answer
        return {
            ident: system1.Answer(
                id=ident,
                type="choice",
                value=value,
                probabilities={str(value): 1.0},
                confidence=confidence,
                legend=None,
            )
        }


def _install(monkeypatch: pytest.MonkeyPatch, answer: Any) -> _System1:
    double = _System1(answer)
    monkeypatch.setattr(system1, "ask", double)
    return double


def test_a_cheaper_observation_descends_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, ("mechanical_fanout", 0.9))
    effective, warning, _ = classify.resolve_execution_class(
        "judgment", "rename the symbol in twelve files", _roster()
    )
    assert effective == "mechanical_fanout"
    assert warning is not None
    assert "judgment" in warning
    assert "mechanical_fanout" in warning
    assert "0.9" in warning


#: The options and words Laya is trained and evaluated on, in the order it is
#: asked them: copied from the gold set's `question.criteria`
#: (`~/.nott/laya/feedback/gold/nspawn_v3.jsonl`, one table for all 416 items).
_GOLD_CRITERIA = {
    "judgment": (
        "Weighing an open question with no settled answer: trade-offs, "
        "architecture, taste, or deciding what should be done at all."
    ),
    "planning": (
        "Turning an agreed result into an ordered plan of work: phases, "
        "write-sets, sequencing, acceptance."
    ),
    "review": (
        "Assessing someone else's candidate — code, plan or argument — "
        "against acceptance, and reporting findings."
    ),
    "mechanical_fanout": (
        "Bulk work whose rule is already known and repeats over many places: "
        "renames, ports of one pattern, sweeps across files."
    ),
    "design": (
        "Shaping how something looks or reads: interface, layout, wording, "
        "visual or interaction work."
    ),
    "verification": (
        "Proving a claim by running or measuring it: tests, gates, "
        "reproductions, evidence that something holds."
    ),
    "deterministic": (
        "Work with one correct output and no latitude: running a formatter, "
        "reading a value, applying a stated edit."
    ),
    "implementation": (
        "Making a bounded, already-agreed code or configuration change: "
        "implementing a feature, fix or port whose result is settled."
    ),
    "nenhuma": "The text states no task to classify.",
}


def test_the_question_is_the_gold_sets_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same options, same order, same words: the question Laya was trained on."""
    double = _install(monkeypatch, ("judgment", 1.0))
    classify.resolve_execution_class("judgment", "add the flag", _roster())
    _state, questions = double.calls[0]
    criteria = questions["execution_class"]["criteria"]
    assert list(criteria.items()) == list(_GOLD_CRITERIA.items())


def test_an_implementation_answer_is_an_answer_not_an_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Laya says `implementation`: it is observed as such, never `unclassified`."""
    classes = {**_CLASSES, "implementation": _CLASSES["review"]}
    _install(monkeypatch, ("implementation", 0.9))
    effective, warning, observation = classify.resolve_execution_class(
        "implementation", "implement the agreed fix", _roster(classes)
    )
    assert (effective, warning) == ("implementation", None)
    assert observation is not None and observation.observed == "implementation"

    # Declared dearer, observed `implementation`: the observation descends to it.
    effective, warning, _ = classify.resolve_execution_class(
        "judgment", "implement the agreed fix", _roster(classes)
    )
    assert effective == "implementation"
    assert warning is not None and "implementation" in warning


def test_a_dearer_observation_never_climbs(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, ("judgment", 1.0))
    effective, warning, _ = classify.resolve_execution_class(
        "mechanical_fanout", "rename the symbol", _roster()
    )
    assert effective == "mechanical_fanout"
    # The class does not move, which is the whole point of this leg — but the
    # disagreement is now SAID, so the owner's declared-versus-observed audit
    # can see a dispatch where a third party read the work differently and the
    # price stayed put.
    assert warning is not None and "observed" in warning


def test_an_equal_cost_observation_keeps_the_declared_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, ("design", 1.0))
    effective, warning, _ = classify.resolve_execution_class(
        "review", "judge this candidate", _roster()
    )
    assert effective == "review"
    # The class does not move, which is the whole point of this leg — but the
    # disagreement is now SAID, so the owner's declared-versus-observed audit
    # can see a dispatch where a third party read the work differently and the
    # price stayed put.
    assert warning is not None and "observed" in warning


def test_a_roster_without_classes_never_asks_the_system1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    double = _install(monkeypatch, ("mechanical_fanout", 1.0))
    effective, warning, _ = classify.resolve_execution_class(
        "judgment", "anything at all", _roster({})
    )
    assert (effective, warning) == ("judgment", None)
    assert double.calls == []


def test_the_state_is_the_head_of_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    double = _install(monkeypatch, ("judgment", 1.0))
    classify.resolve_execution_class("judgment", "x" * 9000, _roster())
    state, _questions = double.calls[0]
    assert state == "x" * 4000


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(system1.System1Unavailable("no key"), id="unavailable"),
        pytest.param(None, id="question-unanswered"),
        pytest.param((0.5, 1.0), id="value-off-the-vocabulary"),
        pytest.param(("nenhuma", 1.0), id="none-of-them"),
        pytest.param(("mechanical_fanout", None), id="confidence-absent"),
        pytest.param(("mechanical_fanout", 0.49), id="confidence-below-the-floor"),
    ],
)
def test_every_failure_falls_to_unclassified_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, answer: Any
) -> None:
    _install(monkeypatch, answer)
    effective, warning, _ = classify.resolve_execution_class(
        "judgment", "orchestrate the campaign", _roster()
    )
    assert effective == "unclassified"
    assert warning is not None
    assert "judgment" in warning


def test_a_failure_never_raises_the_floor_of_a_cheaper_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`deterministic` is cheaper than `unclassified`: falling closed is a `min`."""
    _install(monkeypatch, system1.System1Unavailable("down"))
    effective, warning, _ = classify.resolve_execution_class(
        "deterministic", "run the formatter", _roster()
    )
    assert effective == "deterministic"
    assert warning is not None


def test_any_other_library_exception_rises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, RuntimeError("bug, not an outage"))
    with pytest.raises(RuntimeError, match="bug, not an outage"):
        classify.resolve_execution_class("judgment", "prompt", _roster())


def test_a_zero_cap_is_a_cap_and_not_an_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`max_strength = 0` is the cheapest class there is, never "no cap"."""
    # Same ceiling on both sides, so only the cap can separate them: read as an
    # absence, zero would tie and the declared class would stand.
    classes = dict(_CLASSES)
    classes["deterministic"] = ClassPolicy(effort="low", ceiling="low")
    classes["mechanical_fanout"] = ClassPolicy(
        effort="low", ceiling="low", max_strength=0
    )
    _install(monkeypatch, ("mechanical_fanout", 1.0))
    effective, warning, _ = classify.resolve_execution_class(
        "deterministic", "run the formatter", _roster(classes)
    )
    assert effective == "mechanical_fanout"
    assert warning is not None


def test_the_classifier_asks_with_its_own_short_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch waits behind this call: the library default is minutes."""
    double = _install(monkeypatch, ("judgment", 1.0))
    classify.resolve_execution_class("judgment", "decide the architecture", _roster())
    (deadline,) = double.timeouts
    assert deadline is not None
    assert deadline != system1.TIMEOUT_SECONDS
    assert 0 < deadline <= classify.CLASSIFY_TIMEOUT_SECONDS


def test_a_shorter_roster_deadline_wins_and_a_longer_one_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`system1.timeout_seconds` may shorten the classifier, never lengthen it."""
    double = _install(monkeypatch, ("judgment", 1.0))
    short = Roster(classes=dict(_CLASSES), system1=System1(timeout_seconds=1.0))
    classify.resolve_execution_class("judgment", "p", short)
    long = Roster(classes=dict(_CLASSES), system1=System1(timeout_seconds=600.0))
    classify.resolve_execution_class("judgment", "p", long)
    assert double.timeouts == [1.0, classify.CLASSIFY_TIMEOUT_SECONDS]


def test_an_exhausted_deadline_falls_closed_with_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline is proven by the double, never by a wall clock."""

    def timing_out(
        state: Any,
        questions: dict[str, Any],
        model: Any = None,
        timeout: float | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        raise system1.System1Unavailable(f"timed out after {timeout}s")

    monkeypatch.setattr(system1, "ask", timing_out)
    effective, warning, _ = classify.resolve_execution_class(
        "judgment", "decide the architecture", _roster()
    )
    assert effective == "unclassified"
    assert warning is not None and "timed out after" in warning


#: The two axes pull APART here, which is the whole point: `design` caps
#: strength lower than `judgment` but tops out HIGHER on the effort ladder.
#: Every class in `_CLASSES` moves both axes together, so no test built on that
#: table can see this — measured by an independent review, 2026-09-21.
_CROSSED_AXES = {
    "unclassified": ClassPolicy(effort="low", ceiling="medium", max_strength=7),
    "judgment": ClassPolicy(effort="high", ceiling="high"),
    "design": ClassPolicy(effort="medium", ceiling="ultra", max_strength=2),
}


def test_a_cheaper_cap_never_buys_a_higher_effort_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The System1 LOWERS. A class that is cheaper on one axis and dearer on the
    other is not cheaper, and taking it would hand the dispatch a ceiling the
    declared class could not reach.

    Declared `judgment` (ceiling `high`), observed `design` (cap 2 but ceiling
    `ultra`). A lexicographic compare reads `(2, ultra) < (256, high)` and
    switches, so an effort of `ultra` — which `_class_effort` would have
    refused by name against `judgment` — is suddenly inside the ceiling. That
    is an escalation bought by an observation, and `classify` exists to forbid
    exactly that.
    """
    _install(monkeypatch, ("design", 0.9))
    roster = _roster(_CROSSED_AXES)
    effective, _, _ = classify.resolve_execution_class(
        "judgment", "rename x to y", roster
    )
    assert effective == "judgment"


def test_the_failed_system1_never_raises_the_effort_ceiling_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same trap on the fail-closed path: `min(declared, unclassified)`.

    With a roster whose `unclassified` tops out ABOVE the declared class, a
    FAILURE of the System1 would hand the dispatch a higher ceiling than it asked
    for — falling closed that raises a ceiling is not falling closed.
    """
    _install(monkeypatch, system1.System1Unavailable("down"))
    roster = _roster(
        {
            "unclassified": ClassPolicy(effort="low", ceiling="ultra", max_strength=1),
            "deterministic": ClassPolicy(effort="low", ceiling="low"),
        }
    )
    effective, warning, _ = classify.resolve_execution_class(
        "deterministic", "rename x to y", roster
    )
    assert effective == "deterministic"
    assert warning is not None


#: The THIRD axis, which the two tables above cannot see because both give
#: every class the same `effort`. A class buys three things and this is the one
#: actually spent: with no pinned effort the picker resolves the turn to
#: `policy.effort`. Found by an independent review, 2026-09-21, with these
#: exact tables.
_CROSSED_START = {
    "unclassified": ClassPolicy(effort="high", ceiling="high", max_strength=1),
    "deterministic": ClassPolicy(effort="low", ceiling="ultra", max_strength=7),
}


def test_an_observation_never_raises_the_effort_actually_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cheaper cap and cheaper ceiling, DEARER start — that is not cheaper.

    `verification` caps lower and tops out lower than `deterministic`, so a
    two-axis compare calls it cheaper and switches. But it STARTS at `high`
    where the declared class started at `low`, so the observation raised the
    effort the dispatch actually runs at — an escalation bought by an
    observation, which is what this module exists to forbid.
    """
    _install(monkeypatch, ("verification", 0.9))
    roster = _roster(
        {
            "unclassified": ClassPolicy(effort="low", ceiling="low", max_strength=1),
            "deterministic": ClassPolicy(effort="low", ceiling="ultra", max_strength=9),
            "verification": ClassPolicy(effort="high", ceiling="high", max_strength=7),
        }
    )
    effective, _, _ = classify.resolve_execution_class(
        "deterministic", "rename x to y", roster
    )
    assert effective == "deterministic"


def test_a_failed_system1_never_raises_the_effort_actually_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same third axis on the fail-closed path.

    `unclassified` caps lower and tops out lower, but STARTS at `high`. A
    failure of the System1 must not hand the dispatch a dearer start than the class
    the caller declared — a fall that raises what is billed is not a fall.
    """
    _install(monkeypatch, system1.System1Unavailable("down"))
    effective, warning, _ = classify.resolve_execution_class(
        "deterministic", "rename x to y", _roster(_CROSSED_START)
    )
    assert effective == "deterministic"
    assert warning is not None


#: The FOURTH axis, and it is not a number at all: the NAME of the class picks
#: which column `rank_providers` sorts by (`ranks.py`, `_AXES`) — `strength`
#: descending for judgment/planning/review/verification/implementation, `thrift` for
#: `mechanical_fanout`, `design` for `design`. Two classes can be identical on
#: all three numeric axes and still route to opposite ends of the roster.
#: Found by an independent review, 2026-09-21, with this exact table.
_CROSSED_RANKING = {
    "unclassified": ClassPolicy(effort="medium", ceiling="medium", max_strength=9),
    "mechanical_fanout": ClassPolicy(effort="high", ceiling="high", max_strength=9),
    "judgment": ClassPolicy(effort="low", ceiling="low", max_strength=9),
}


def test_an_observation_never_switches_the_ranking_axis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cheaper on all three numbers, and still dearer: it ranks by `strength`.

    Declared `mechanical_fanout` ranks the affordable pool by descending
    `thrift` — the cheap route wins. Observed `judgment` caps the same and
    starts and tops out LOWER, so every numeric axis says cheaper; but it ranks
    by descending `strength`, so the dispatch leaves on the strongest route in
    the pool instead of the thriftiest. The bill goes up on an axis no number
    in `ClassPolicy` carries, which is why the comparison cannot be numeric
    alone.
    """
    _install(monkeypatch, ("judgment", 0.9))
    effective, _, _ = classify.resolve_execution_class(
        "mechanical_fanout", "rename x to y", _roster(_CROSSED_RANKING)
    )
    assert effective == "mechanical_fanout"


def test_an_observation_never_narrows_the_pool_to_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`verification` drops the producer from the pool before ranking.

    Same `strength` axis as `review`, so the axis check alone lets it through —
    but `rank_providers` removes `producer` by name for this one class
    (`ranks.py`), and the route that falls out may be the cheap one. An
    observation that shrinks the pool can only raise the bill.
    """
    _install(monkeypatch, ("verification", 0.9))
    effective, _, _ = classify.resolve_execution_class(
        "review",
        "rename x to y",
        _roster(
            {
                "unclassified": ClassPolicy(
                    effort="medium", ceiling="medium", max_strength=9
                ),
                "review": ClassPolicy(effort="high", ceiling="high", max_strength=9),
                "verification": ClassPolicy(
                    effort="low", ceiling="low", max_strength=9
                ),
            }
        ),
    )
    assert effective == "review"


def test_a_failed_system1_never_moves_a_thrifty_class_off_its_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fall keeps the thrifty column, so it stays a descent.

    This leg started life asserting the opposite. An independent review showed
    that a dispatch declaring `mechanical_fanout` fell onto a DEARER route with
    the System1 merely down: the floor ranked name-stable, and the alphabetically
    first provider is not the thriftiest. The cure was not to forbid the fall
    but to fix the floor — `ranks.py` now ranks `unclassified` by descending
    `thrift`, the same column, so the fall cannot reshuffle the pool toward a
    dearer route. `test_the_unclassified_floor_ranks_by_thrift_not_by_name`
    holds that, and goes RED if the floor drifts back to name-stable.

    What this leg measures is what survived: with the column equal, the three
    numbers decide, the floor is lower on all of them, and the dispatch
    descends carrying its warning. Falling closed means never dearer — and the
    floor ranking cheap is what makes it never dearer.
    """
    _install(monkeypatch, system1.System1Unavailable("down"))
    effective, warning, _ = classify.resolve_execution_class(
        "mechanical_fanout",
        "rename x to y",
        _roster(
            {
                "unclassified": ClassPolicy(
                    effort="low", ceiling="low", max_strength=1
                ),
                "mechanical_fanout": ClassPolicy(
                    effort="high", ceiling="high", max_strength=9
                ),
            }
        ),
    )
    assert effective == "unclassified"
    assert warning is not None


def test_a_class_the_table_never_declares_is_not_comparable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A class absent from `[classes]` is UNKNOWN, not maximal.

    `_cost` modelled an absent class as the top of every axis, on the reading
    that no policy means no limit. But `_class_effort` returns `None` for it
    (`picker.py`), and `_resolve_effort` then falls to `_pick_effort` — the
    pre-policy ladder, which takes the route's own `effort`. That may be the
    CHEAPEST rung on the roster, not the dearest.

    So every class the table DOES declare reads as cheaper on the effort axis
    and wins, and the dispatch starts higher than the declared class ever
    would have. The declared class here is `design`, absent from the table and
    running whatever the route declares; the observed `deterministic` starts
    at `high`. Found by an independent review, 2026-09-21.
    """
    _install(monkeypatch, ("deterministic", 0.9))
    effective, _, _ = classify.resolve_execution_class(
        "design",
        "rename x to y",
        _roster(
            {
                "unclassified": ClassPolicy(
                    effort="low", ceiling="low", max_strength=1
                ),
                "deterministic": ClassPolicy(
                    effort="high", ceiling="high", max_strength=1
                ),
            }
        ),
    )
    assert effective == "design"


def test_an_observation_never_swaps_one_unordered_column_for_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`thrift` is cheaper than the rest; the rest have no order among them.

    Giving `strength`, `design` and the name-stable fallback one shared score
    said they were interchangeable, which is the mirror of the error it was
    meant to fix: declared `design` ranks the pool by descending `design`, and
    observed `judgment` ranks it by descending `strength`, so the dispatch
    leaves on the STRONGEST route instead of the best-designed one. Every
    number in `judgment` is lower, so nothing else stops it.

    No order exists to claim between those columns, so they are comparable
    only with themselves. Found by an independent review, 2026-09-21.
    """
    _install(monkeypatch, ("judgment", 0.9))
    effective, _, _ = classify.resolve_execution_class(
        "design",
        "rename x to y",
        _roster(
            {
                "unclassified": ClassPolicy(
                    effort="low", ceiling="low", max_strength=1
                ),
                "design": ClassPolicy(effort="high", ceiling="high", max_strength=9),
                "judgment": ClassPolicy(effort="low", ceiling="low", max_strength=9),
            }
        ),
    )
    assert effective == "design"


def test_the_thrifty_column_is_still_reachable_from_an_unordered_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid progress: `thrift` IS cheaper, so the descent still happens.

    The same table, with `mechanical_fanout` observed instead. Its column
    takes the cheapest route in the pool by construction, so it is below every
    other column and the observation lands.
    """
    _install(monkeypatch, ("mechanical_fanout", 0.9))
    effective, warning, _ = classify.resolve_execution_class(
        "design",
        "rename x to y",
        _roster(
            {
                "unclassified": ClassPolicy(
                    effort="low", ceiling="low", max_strength=1
                ),
                "design": ClassPolicy(effort="high", ceiling="high", max_strength=9),
                "mechanical_fanout": ClassPolicy(
                    effort="low", ceiling="low", max_strength=9
                ),
            }
        ),
    )
    assert effective == "mechanical_fanout"
    assert warning is not None


def test_a_lower_cap_alone_is_not_a_descent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strength is not price, so evicting the strong can leave the dearest.

    The cap filters the pool by `strength`, but the pool is ranked by
    `thrift`, and `ClassPolicy` never checks that those two agree — nothing
    stops a roster whose thriftiest route is also its strongest. Dropping the
    cap then evicts exactly that route and the dispatch lands on the dearest
    one, with the same column, the same effort and the same ceiling. The cap
    moved the bill UP while reading as a descent.

    So a lower cap stays a constraint — it may never rise — but it no longer
    proves anything by itself: some other axis must actually fall. Found by an
    independent review, 2026-09-21, with a roster of `a` (strength 9, thrift 9)
    and `b` (strength 0, thrift 1).
    """
    _install(monkeypatch, system1.System1Unavailable("down"))
    effective, warning, _ = classify.resolve_execution_class(
        "mechanical_fanout",
        "rename x to y",
        _roster(
            {
                "unclassified": ClassPolicy(
                    effort="low", ceiling="low", max_strength=1
                ),
                "mechanical_fanout": ClassPolicy(effort="low", ceiling="low"),
            }
        ),
    )
    assert effective == "mechanical_fanout"
    assert warning is not None


def test_the_column_table_cannot_drift_from_the_one_the_picker_uses() -> None:
    """`_COLUMNS` restates `ranks._AXES`, and nothing else would notice a lie.

    `classify` compares columns by name; `ranks` sorts by them with lambdas,
    which cannot be compared. So the two tables are written twice, and a class
    moved from `thrift` to `strength` in `ranks` alone would leave `classify`
    still calling it the cheap column — an observation would then descend onto
    a dearer route and every existing test would stay green. An independent
    review named this as uncovered, 2026-09-21.
    """
    from nspawn import ranks
    from nspawn.owner_roster import Provider

    probes = {
        "strength": Provider(name="p", strength=7),
        "thrift": Provider(name="p", thrift=7),
        "design": Provider(name="p", design=7),
    }
    for name, axis in ranks._AXES.items():
        column = classify._COLUMNS.get(name)
        assert column is not None, f"{name} ranks in ranks.py but has no column"
        assert axis(probes[column]) == 7, f"{name}: classify says {column}"
        for other, probe in probes.items():
            if other != column:
                assert axis(probe) is None, f"{name} also reads {other}"

    # The floor is not in `_AXES` — it is mapped inside `rank_providers` — so
    # it is pinned separately, against what that function actually does.
    cheap = Provider(name="zzz", strength=9, thrift=9)
    dear = Provider(name="aaa", strength=9, thrift=1)
    assert ranks.rank_providers([dear, cheap], UNCLASSIFIED) == [cheap, dear]
    assert classify._COLUMNS[UNCLASSIFIED] == "thrift"

    # And every class `classify` names must be one `ranks` or the floor knows.
    assert set(classify._COLUMNS) == set(ranks._AXES) | {UNCLASSIFIED}


def test_leaving_verification_is_not_a_descent_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widening the pool can only add routes, and added routes can be dearer.

    `verification` is the one class that drops the producer before ranking, so
    its flag was scored as an axis: leaving it read as a descent. But leaving
    it puts the producer BACK, and on the `strength` column a larger pool may
    now be topped by a stronger — dearer — route than the one the declared
    class would have taken. Narrowing the pool and widening it are not
    opposites in price.

    So the flag joins the cap: it may not rise, and it proves nothing on its
    own. Here both policies are identical, so nothing else separates them and
    the declared class must stand. Found by an independent review, 2026-09-21.
    """
    _install(monkeypatch, ("review", 0.9))
    same = ClassPolicy(effort="low", ceiling="low", max_strength=9)
    effective, _, _ = classify.resolve_execution_class(
        "verification",
        "rename x to y",
        _roster(
            {
                "unclassified": ClassPolicy(
                    effort="low", ceiling="low", max_strength=1
                ),
                "verification": same,
                "review": same,
            }
        ),
    )
    assert effective == "verification"


def test_a_lower_ceiling_alone_never_buys_a_wider_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two wrongs that only fire together: the flag one way, the ceiling as spend.

    Leaving `verification` was permitted because the flag was ordered — it may
    not rise. But the module's own comment names the hazard of the falling
    direction too: the producer comes back and the wider pool may be topped by
    a stronger route. The axis is unordered, like the columns, and only
    equality is safe.

    On its own that could not fire, because something in SPEND still had to
    fall. `ceiling` supplied it — and `ceiling` is not spend: `_resolve_effort`
    runs `policy.effort`, and `ceiling` appears only in `_class_effort`, to
    refuse a pin by name. Lowering it refuses more and spends nothing less, so
    scoring it as spend manufactured the strict descent that opened the hole.

    Declared `verification` (ceiling `high`) ranks a pool without the producer;
    observed `judgment` (ceiling `medium`) ranks it WITH the producer, which is
    the strongest route on the roster. Same effort, same column, same cap.
    Found by an independent review, 2026-09-21, with these exact policies.
    """
    _install(monkeypatch, ("judgment", 0.9))
    effective, _, _ = classify.resolve_execution_class(
        "verification",
        "rename x to y",
        _roster(
            {
                "unclassified": ClassPolicy(
                    effort="low", ceiling="low", max_strength=1
                ),
                "verification": ClassPolicy(effort="low", ceiling="high"),
                "judgment": ClassPolicy(effort="low", ceiling="medium"),
            }
        ),
    )
    assert effective == "verification"


def test_leaving_verification_is_blocked_even_when_the_spend_really_falls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag alone, with a genuine descent beside it to get past the rest.

    The combined leg above needs BOTH mistakes to fire, so neither cure dies
    on its own there. This one isolates the flag: the observed class really is
    cheaper — it starts at `low` where the declared one starts at `high` — so
    spend falls honestly and nothing else stands in the way. Only treating the
    producer-drop as unordered stops the swap, and the swap would put the
    strongest route back in the pool.
    """
    _install(monkeypatch, ("judgment", 0.9))
    effective, _, _ = classify.resolve_execution_class(
        "verification",
        "rename x to y",
        _roster(
            {
                "unclassified": ClassPolicy(
                    effort="low", ceiling="low", max_strength=1
                ),
                "verification": ClassPolicy(effort="high", ceiling="high"),
                "judgment": ClassPolicy(effort="low", ceiling="high"),
            }
        ),
    )
    assert effective == "verification"


def test_a_lower_ceiling_alone_moves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling alone, with the flag equal on both sides.

    Two judgment-column classes, neither dropping the producer, identical cap
    and identical starting effort; the ONLY difference is the ceiling. The
    ceiling is spent by nobody — it exists to refuse a pinned effort by name —
    so nothing about this dispatch gets cheaper and the declared class stands.
    Scoring the ceiling as spend would swap the class for no gain at all, and
    that is what let the wider-pool hole fire.
    """
    _install(monkeypatch, ("review", 0.9))
    effective, warning, _ = classify.resolve_execution_class(
        "judgment",
        "rename x to y",
        _roster(
            {
                "unclassified": ClassPolicy(
                    effort="low", ceiling="low", max_strength=1
                ),
                "judgment": ClassPolicy(effort="low", ceiling="ultra", max_strength=9),
                "review": ClassPolicy(effort="low", ceiling="low", max_strength=9),
            }
        ),
    )
    assert effective == "judgment"
    # The class does not move, which is the whole point of this leg — but the
    # disagreement is now SAID, so the owner's declared-versus-observed audit
    # can see a dispatch where a third party read the work differently and the
    # price stayed put.
    assert warning is not None and "observed" in warning


def test_a_refused_observation_still_says_what_was_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence when the System1 disagrees but the swap is refused hides the finding.

    Measured on the live service, 2026-09-21: `judgment` declared with a
    trivially mechanical prompt ran on `claude` with NO warning at all, three
    times. The System1 was working — it observed `verification` and
    `deterministic` — but neither can be reached from `judgment`: entering
    `verification` narrows the pool, and `deterministic` ranks on a column
    with no order against `strength`. The swap was correctly refused, and the
    disagreement vanished.

    That is the one thing the run must not swallow. The owner's third step is
    an audit of declared-versus-observed, and it reads exactly this warning;
    a dispatch where the classifier disagreed and nothing moved is the most
    interesting row that audit could have, and it was invisible.

    So the warning now covers three outcomes, not one: the class descended,
    the class could not be observed, or the class was observed and kept.
    """
    _install(monkeypatch, ("verification", 0.9))
    effective, warning, _ = classify.resolve_execution_class(
        "judgment", "run the tests and report", _roster()
    )
    assert effective == "judgment"
    assert warning is not None
    assert "verification" in warning and "judgment" in warning


#: The calibration table holds only for this model (see `classify._SUBQ_BIAS`).
_SUBQ = "subq-snap-preview"

#: Raw pick `verification`; under the SubQ bias table the pick is `review`.
_REVIEW_UNDER_BIAS = {
    "review": 0.30,
    "verification": 0.40,
    "design": 0.03,
    "deterministic": 0.03,
    "implementation": 0.03,
    "judgment": 0.03,
    "mechanical_fanout": 0.03,
    "planning": 0.03,
    "nenhuma": 0.02,
}


def _distribution(
    value: str, confidence: float | None, probabilities: dict[str, float] | None
) -> system1.Answer:
    return system1.Answer(
        id="execution_class",
        type="choice",
        value=value,
        probabilities=probabilities,
        confidence=confidence,
        legend=None,
    )


def _model_roster(model: str) -> Roster:
    return Roster(classes=dict(_CLASSES), system1=System1(model=model))


def test_subq_bias_moves_the_pick_off_the_raw_argmax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTT_SYSTEM1_MODEL", raising=False)
    _install(monkeypatch, _distribution("verification", 0.40, _REVIEW_UNDER_BIAS))
    effective, _, observation = classify.resolve_execution_class(
        "judgment", "check the candidate against acceptance", _model_roster(_SUBQ)
    )
    assert observation is not None
    assert observation.observed == "review"
    assert observation.confidence == pytest.approx(0.7455, abs=1e-3)
    assert effective == "review"


def test_subq_renormalised_confidence_below_the_floor_is_unclassified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTT_SYSTEM1_MODEL", raising=False)
    flat = {
        "review": 0.25,
        "deterministic": 0.25,
        "implementation": 0.25,
        "verification": 0.25,
    }
    _install(monkeypatch, _distribution("review", 0.9, flat))
    effective, warning, observation = classify.resolve_execution_class(
        "judgment", "check the candidate against acceptance", _model_roster(_SUBQ)
    )
    assert observation is not None
    assert observation.observed is None
    assert effective == UNCLASSIFIED
    assert warning is not None and "confident" in warning


def test_subq_answer_without_probabilities_keeps_the_raw_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTT_SYSTEM1_MODEL", raising=False)
    _install(monkeypatch, _distribution("verification", 0.9, None))
    _, _, observation = classify.resolve_execution_class(
        "judgment", "run the suite", _model_roster(_SUBQ)
    )
    assert observation is not None
    assert observation.observed == "verification"
    assert observation.confidence == 0.9


def test_another_model_gets_no_bias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTT_SYSTEM1_MODEL", raising=False)
    _install(monkeypatch, _distribution("verification", 0.6, _REVIEW_UNDER_BIAS))
    _, _, observation = classify.resolve_execution_class(
        "judgment", "check the candidate against acceptance", _model_roster("laya-nott")
    )
    assert observation is not None
    assert observation.observed == "verification"
    assert observation.confidence == 0.6


@pytest.mark.parametrize(
    "probabilities",
    [
        {"review": None},
        {"review": float("inf")},
        {"review": float("nan")},
        ["review"],
        {"review": 0.0},
        {"review": 0, "verification": 0.0},
        {"review": 1e-9, "verification": 1e-9},
        {},
    ],
)
def test_subq_invalid_distribution_is_unclassified_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, probabilities: Any
) -> None:
    """The distribution comes from the wire: a bad one is a failed observation."""
    monkeypatch.delenv("NOTT_SYSTEM1_MODEL", raising=False)
    _install(monkeypatch, _distribution("review", 0.9, probabilities))
    effective, warning, observation = classify.resolve_execution_class(
        "judgment", "check the candidate against acceptance", _model_roster(_SUBQ)
    )
    assert observation is not None and observation.observed is None
    assert effective == UNCLASSIFIED
    assert warning is not None and "invalid distribution" in warning
