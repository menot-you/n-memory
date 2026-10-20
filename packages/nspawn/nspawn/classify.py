"""The execution class a dispatch actually is, observed by the System1.

The class on the wire is self-declared, and a caller that wants a strong model
writes `judgment`. Here a third party reads the prompt and says what the work
is; the effective class is the CHEAPER of the two, so the observation can only
close escalation, never open it.

Every failure of the observation is one door: the class falls to
`unclassified` and, because a declared class may be cheaper than that, the
effective class is still the `min` of the two. Falling closed that raised a
ceiling would not be falling closed.

This is not in `picker.py` (pure by contract: no vendor call), not in
`system1.py` (configuration of the verbs, not a domain rule) and not in
`supervisor.py` (large enough already).
"""

from __future__ import annotations

import math

from . import system1
from .owner_roster import EFFORT_LADDER, UNCLASSIFIED, Roster
from .rows import ClassObservation

#: The single question id of the single question asked per dispatch.
_QUESTION = "execution_class"

#: The option the library requires when the list may not cover what comes in —
#: without it the model must pick a wrong option and the distribution lies.
_NONE = "nenhuma"

#: Below this the classifier did not distinguish, and an answer with no declared
#: hesitation is not evidence of certainty either: both count as a failure.
_MIN_CONFIDENCE = 0.5

_SCOPE = "EXECUTION_CLASS"

#: The model the bias table below is fitted for.
_SUBQ_MODEL = "subq-snap-preview"

_SUBQ_BIAS = {
    "design": 1.0,
    "deterministic": 1.75,
    "implementation": 1.25,
    "judgment": 0.25,
    "mechanical_fanout": 0.0,
    "nenhuma": -6.0,
    "planning": -0.5,
    "review": 2.0,
    "verification": -0.25,
}


#: The least probability the calibration reads: one at or below it is the floor.
_FLOOR = 1e-6


def _calibrated(probabilities: dict[str, float]) -> tuple[str, float]:
    """The pick and its renormalised probability after the SubQ bias."""
    z = {
        c: math.log(max(_FLOOR, p)) + _SUBQ_BIAS.get(c, 0.0)
        for c, p in probabilities.items()
    }
    top = max(z.values())
    weights = {c: math.exp(v - top) for c, v in z.items()}
    pick = max(weights, key=weights.__getitem__)
    return pick, weights[pick] / sum(weights.values())


#: The head of the prompt is what carries the statement of the task; the body
#: is attachments. The whole `state` travels in the request, and "milliseconds
#: and marginal cost" only holds with the cut.
_STATE_LIMIT = 4000

#: The classifier runs INSIDE the single-threaded stdio loop of a dispatch: a
#: status or result frame of another live run waits behind it. The owner admitted
#: the synchronous call as "milliseconds, marginal cost", and the library default
#: (120 s, no retry) is not that. Ten seconds is two orders of magnitude above the
#: expected answer and still a wait a caller can sit through; the owner shortens it
#: for every verb with `system1.timeout_seconds` in the roster, and a longer roster
#: value never lengthens THIS path.
CLASSIFY_TIMEOUT_SECONDS = 10.0

#: A class with no policy, or a cap of `None`, costs more than any u8 cap.
_UNCAPPED = 256

#: The provider column `rank_providers` sorts by, per class. A class absent
#: here ranks name-stable, which is its own column.
#:
#: The columns form a PARTIAL order, not a ladder. `thrift` descending picks
#: the cheapest route in the pool by construction, so it is below every other
#: column. Between `strength`, `design` and name-stable there is no cost order
#: to claim: scoring them equal would make them interchangeable, and an
#: observation could swap `design` for `strength` and leave on the strongest
#: route instead of the best-designed one; scoring them apart would invent an
#: order nothing in the code supports. So they are comparable only with
#: themselves.
_COLUMNS = {
    "judgment": "strength",
    "planning": "strength",
    "review": "strength",
    "implementation": "strength",
    "verification": "strength",
    "mechanical_fanout": "thrift",
    "design": "design",
    # The floor ranks by thrift as well (`ranks.py`), which is what makes the
    # fail-closed fall a descent instead of an arbitrary reshuffle.
    UNCLASSIFIED: "thrift",
}
_THRIFT = "thrift"

_INSTRUCTIONS = (
    "Read the task below and answer with the kind of work it is. Judge the "
    "work itself, not how urgent or important the author says it is. Answer "
    f"{_NONE} when the text states no task at all."
)

#: One description per class. Keyed by `EXECUTION_CLASSES`, which `test_mcp_l6`
#: pins to the exact eight, so a class arriving without a description here is a
#: `KeyError` in a test long before it is one in a dispatch. The words are the
#: ones Laya is trained and evaluated on (its gold set's `question.criteria`):
#: a production question worded otherwise is a question the model never saw.
_DESCRIPTIONS = {
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
    "verification": (
        "Proving a claim by running or measuring it: tests, gates, "
        "reproductions, evidence that something holds."
    ),
    "implementation": (
        "Making a bounded, already-agreed code or configuration change: "
        "implementing a feature, fix or port whose result is settled."
    ),
    "design": (
        "Shaping how something looks or reads: interface, layout, wording, "
        "visual or interaction work."
    ),
    "mechanical_fanout": (
        "Bulk work whose rule is already known and repeats over many places: "
        "renames, ports of one pattern, sweeps across files."
    ),
    "deterministic": (
        "Work with one correct output and no latitude: running a formatter, "
        "reading a value, applying a stated edit."
    ),
}


def _vocabulary() -> tuple[str, ...]:
    """The eight classes.

    Imported inside the function on purpose: `mcp` imports `supervisor`, which
    imports this module, so a module-level import would close a cycle. The
    vocabulary stays read from its owner rather than retyped here.
    """
    from .mcp import EXECUTION_CLASSES

    return EXECUTION_CLASSES


def _cost(roster: Roster, name: str) -> tuple[int, int, int] | None:
    """What a class buys: strength cap, effort ceiling, and the effort it RUNS.

    THREE AXES, because a class buys three things and the third is the one
    that is actually spent. With no pinned effort the picker resolves the turn
    to `policy.effort` (`picker.py`, `_clamp_to_served(provider,
    policy.effort)`); the picker's own refusal says it in words — a class
    "buys effort X and tops out at Y". Measuring the tops-out and ignoring the
    buys let an observation raise the effort actually billed, which is the one
    thing this module forbids. Found by an independent review, 2026-09-21.

    The three are independent by construction: `ClassPolicy` only checks
    `effort <= ceiling` INSIDE one class, so the cheapest class by cap and
    ceiling may be the dearest at the start.

    A class the table never declares has NO cost here — it returns `None`, and
    `_is_cheaper` refuses to compare it. Modelling it as the top of every axis
    was wrong in the one direction that matters: `_class_effort` returns `None`
    for it, `_resolve_effort` falls to `_pick_effort`, and the dispatch runs
    the pre-policy ladder — the route's own `effort`, which may be the cheapest
    rung on the roster. Calling it maximal made every declared class read as
    cheaper on the effort axis and win, which raised the bill. Found by an
    independent review, 2026-09-21.

    `max_strength` of zero is a real cap and the cheapest one; only `None`
    means uncapped.
    """
    policy = roster.classes.get(name)
    if policy is None:
        return None
    cap = _UNCAPPED if policy.max_strength is None else policy.max_strength
    return (
        cap,
        EFFORT_LADDER.index(policy.ceiling),
        EFFORT_LADDER.index(policy.effort),
    )


def _column_is_cheaper_or_equal(candidate: str, against: str) -> bool:
    """Whether moving to `candidate`'s ranking column cannot cost more.

    True when the column does not change, and when it becomes `thrift` — the
    only column with a known place in the order. Any other move between
    columns is unordered, and an unordered move is not a descent.
    """
    here = _COLUMNS.get(candidate)
    there = _COLUMNS.get(against)
    return here == there or here == _THRIFT


def _drops_producer(name: str) -> bool:
    """Whether `rank_providers` removes the producer from the pool first.

    True for `verification` alone (`ranks.py`). It changes WHICH routes are
    available, in a direction that is not a price either way.
    """
    from . import ranks

    return name == ranks.VERIFICATION


def _column_rank(name: str) -> int:
    """The column as an order, valid only after a sideways move is excluded.

    `_column_is_cheaper_or_equal` rules out swapping one unordered column for
    another; once it has, what remains is either no change or a move to
    `thrift`, and moving to `thrift` is a real descent that has to count.
    """
    return 0 if _COLUMNS.get(name) == _THRIFT else 1


def _lane(roster: Roster, name: str) -> int:
    """0 on the fast lane, 1 on the route's model; only called on declared classes."""
    return 0 if roster.classes[name].fast else 1


def _is_cheaper(roster: Roster, candidate: str, against: str) -> bool:
    """Cheaper means cheaper on EVERY axis, and dearer on none.

    NOT a comparison of the cost tuples. `<` on a tuple is lexicographic: it
    orders by the strength cap and only then looks at the effort ceiling, so a
    class with a LOWER cap and a HIGHER ceiling reads as cheaper and wins — and
    the dispatch walks away with an effort ceiling the declared class could not
    reach. An observation that RAISES a ceiling is an escalation, which is the
    one thing this module exists to forbid, and the fail-closed `min` against
    `unclassified` has the same trap.

    The two axes are independent by construction: `ClassPolicy` only ever
    checks `ceiling >= effort` INSIDE one class, never coherence between them,
    so a roster may cross them. When they cross, neither class is cheaper and
    the declared one stands — the System1 lowers or it does nothing.

    Measured by an independent review, 2026-09-21: every class in the host
    table moves both axes together, so no test built on it could see this.
    """
    # The fail-closed floor is scored like everything else, with no exemption.
    # An earlier cure exempted it from the two routing axes so the fall could
    # always happen, and that was the hole: `unclassified` ranks name-stable,
    # the alphabetically first provider is not the thriftiest, and a dispatch
    # declaring `mechanical_fanout` fell onto a DEARER route with the System1
    # merely down. Falling closed means never dearer, not always
    # `unclassified` — when the floor is not cheaper, the declared class
    # standing IS the closed fall.
    candidate_cost = _cost(roster, candidate)
    against_cost = _cost(roster, against)
    # Either side undeclared is a comparison that cannot be made, and an
    # unmakeable comparison leaves the declared class standing.
    if candidate_cost is None or against_cost is None:
        return False
    if not _column_is_cheaper_or_equal(candidate, against):
        return False
    # The producer-dropping flag is UNORDERED, exactly like the columns.
    # Entering `verification` narrows the pool, which can evict the thriftiest
    # route; leaving it widens the pool, which on the `strength` column can
    # admit a stronger — dearer — one. Neither direction is safe, so only
    # equality is.
    if _drops_producer(candidate) != _drops_producer(against):
        return False

    # What is LEFT splits in two, and only one half proves anything.
    #
    # LIMITS are what the class is allowed but does not spend. The strength
    # cap narrows the pool by `strength` while the pool is ranked by `thrift`,
    # and nothing makes those agree, so dropping it can evict the cheapest
    # route. The effort ceiling is spent by nobody: `_resolve_effort` runs
    # `policy.effort`, and the ceiling appears only in `_class_effort`, to
    # refuse a pinned effort by name. Lowering it refuses more and spends
    # nothing less. A limit may never RISE and proves no descent.
    #
    # SPEND is what the dispatch actually pays: the effort it starts at, and
    # the column the pool is ranked by. A descent has to show up in one of
    # those two, or there is no descent.
    #
    # Every line of this split was bought by an independent review, and the
    # ceiling belonged to the wrong half until the eighth. 2026-09-21.
    here_limits = (candidate_cost[0], candidate_cost[1])
    there_limits = (against_cost[0], against_cost[1])
    if any(a > b for a, b in zip(here_limits, there_limits, strict=True)):
        return False

    # The fast lane is spend too: a class that runs on `model_fast` pays less
    # than one on the route's model, so leaving the lane is a climb.
    here_spend = (candidate_cost[2], _column_rank(candidate), _lane(roster, candidate))
    there_spend = (against_cost[2], _column_rank(against), _lane(roster, against))
    if any(a > b for a, b in zip(here_spend, there_spend, strict=True)):
        return False
    return here_spend != there_spend


def _observe(prompt: str, roster: Roster) -> tuple[str, float | None, str | None]:
    """The observed class, its confidence, and the reason it is not an answer."""
    criteria = {name: _DESCRIPTIONS[name] for name in _vocabulary()}
    criteria[_NONE] = "The text states no task to classify."
    try:
        answers = system1.choice(
            prompt[:_STATE_LIMIT],
            {_QUESTION: {"instructions": _INSTRUCTIONS, "criteria": criteria}},
            timeout=min(system1.resolve_timeout(roster), CLASSIFY_TIMEOUT_SECONDS),
            scope=_SCOPE,
        )
    except system1.System1Unavailable as unavailable:
        # Operational, never a verdict. Any other exception from the library is
        # a bug and rises.
        return UNCLASSIFIED, None, f"the classifier is unavailable: {unavailable}"
    answer = answers.get(_QUESTION)
    if answer is None:
        return UNCLASSIFIED, None, "the classifier did not answer the question"
    value, confidence = answer.value, answer.confidence
    # The bias follows the model this question was asked on: its own route's, or the base.
    if (
        answer.probabilities is not None
        and system1.resolve_model(roster, scope=_SCOPE) == _SUBQ_MODEL
    ):
        # No mass above the floor would give a pick of the bias alone.
        if (
            not isinstance(answer.probabilities, dict)
            or not all(
                type(p) in (int, float) and 0.0 <= p <= 1.0
                for p in answer.probabilities.values()
            )
            or not any(p > _FLOOR for p in answer.probabilities.values())
        ):
            return UNCLASSIFIED, None, "the classifier sent an invalid distribution"
        value, confidence = _calibrated(answer.probabilities)
    if value not in _vocabulary():
        return UNCLASSIFIED, None, f"the classifier answered {value!r}"
    if confidence is None or confidence < _MIN_CONFIDENCE:
        return (
            UNCLASSIFIED,
            None,
            f"the classifier was {confidence} confident",
        )
    return str(value), confidence, None


def resolve_execution_class(
    declared: str, prompt: str, roster: Roster, *, pinned: bool = False
) -> tuple[str, str | None, ClassObservation | None]:
    """The effective class, the warning when it is not the declared one, and
    what System1 was sent and said.

    A roster with no `[classes]` governs nothing, so there is nothing to
    measure against, the System1 is not asked at all, and there is no
    observation.

    `pinned` is true when the caller pinned an effort or a provider. Only a
    confident answer inside the class vocabulary lowers a pinned dispatch. No
    answer, an unsure answer, an invalid distribution, or an answer outside the
    vocabulary is a failed observation: it is no evidence against the pin, so
    the declared class stands (measured 2026-10-16: `planning` with
    `effort=high` was refused against the `unclassified` ceiling whenever
    System1 was down).
    """
    if not roster.classes:
        return declared, None, None
    observed, confidence, failure = _observe(prompt, roster)
    effective = (
        observed
        if not (pinned and failure is not None)
        and _is_cheaper(roster, observed, declared)
        else declared
    )
    observation = ClassObservation(
        text=prompt[:_STATE_LIMIT],
        declared=declared,
        observed=None if failure is not None else observed,
        confidence=None if failure is not None else confidence,
        effective=effective,
    )
    if failure is not None:
        return (
            effective,
            (
                f'execution class "{declared}" could not be observed ({failure}); '
                f'it runs as "{effective}"'
            ),
            observation,
        )
    if observed == declared:
        return declared, None, observation
    if effective == declared:
        # The System1 disagreed and the swap was REFUSED — the observed class is
        # not cheaper on every axis, or it is not comparable at all. Saying
        # nothing here loses the most interesting row the owner's audit of
        # declared-versus-observed could have: a dispatch where a third party
        # read the work differently and the price stayed put. Measured silent
        # on the live service, 2026-09-21.
        return (
            declared,
            (
                f'execution class "{declared}" was declared, "{observed}" observed '
                f"with confidence {confidence}; it is not cheaper on every axis, "
                f'so it runs as "{declared}"'
            ),
            observation,
        )
    return (
        effective,
        (
            f'execution class "{declared}" was declared, "{observed}" observed '
            f'with confidence {confidence}; it runs as "{effective}"'
        ),
        observation,
    )
