"""The synthesis verdict operator: it reads the pre-registration, it does not
invent thresholds.

Every number that decides whether the synthesis path enters production comes
from the text of `evals/nmemory/tests/fixtures/PRE-REGISTERED-SYNTHESIS.txt` — the
set `Q`, the replicas `r`, the one-sided test's `α`, and the rules the file
defines. What that text does not cover is ambiguity and REFUSAL
(`EvalRefusal`, D18 exit 2) naming the measurement: an empty cell is never
worth 0, never satisfies a rule, and never gives anyone the win. The three
rules are conjunctive — any one that fails takes the path down —, and R2b
decides over the DISCORDANT PAIRS, which the file orders read one by one: a
pair without its reading is a refusal, not a verdict.

This module's sibling is `comparison/arena.py`, with the arena's
pre-registration.

The pre-registration moved byte for byte (identity 76fa72659 at docs/plans/nmemory-sintese/PRE-REGISTERED-SYNTHESIS.md, base 233dd4aaa).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from nmemory_evals.capture_gap import EvalRefusal

#: The spelled-out names the file uses to count its own rules.
_SPELLED = {"Uma": 1, "Duas": 2, "Três": 3, "Quatro": 4, "Cinco": 5}

_QUESTIONS = re.compile(r"\*\*(\d+) perguntas `multi-session`")
_REPLICAS = re.compile(r"`r = (\d+)`")
_ALPHA = re.compile(r"`α = (\d+),(\d+)`")
_RULE = re.compile(r"\*\*(R\d[ab]?)\*\*")
_COUNT = re.compile(r"(\w+) regras,\s+conjuntivas")


@dataclass(frozen=True)
class PreRegistration:
    """What the parser read from the text; `sha256` is of the text read, so the
    verdict can name what produced it. The law «after the first run no number
    changes» is imposed from outside: whoever judges passes the first run's
    `expected_sha256`, and divergence is a refusal."""

    sha256: str
    questions: int
    replicas: int
    alpha: float
    rules: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    preregistration_sha256: str
    failed: tuple[str, ...]
    reported: Mapping[str, Any]

    @property
    def enters(self) -> bool:
        return not self.failed


def _one(pattern: re.Pattern[str], text: str, what: str) -> re.Match[str]:
    found = list(pattern.finditer(text))
    if len(found) != 1:
        raise EvalRefusal(
            f"pre-registration: {what} defined {len(found)} times in the text, "
            "expected 1"
        )
    return found[0]


def parse_preregistration(text: str) -> PreRegistration:
    questions = int(_one(_QUESTIONS, text, "the set Q").group(1))
    replicas = int(_one(_REPLICAS, text, "the replicas r").group(1))
    a = _one(_ALPHA, text, "the α").groups()
    rules: list[str] = []
    for m in _RULE.finditer(text):
        if m.group(1) not in rules:
            rules.append(m.group(1))
    spelled = _one(_COUNT, text, "the rule count").group(1)
    if spelled not in _SPELLED:
        raise EvalRefusal(
            f"pre-registration: «{spelled} regras» is not a spelled-out number"
        )
    groups = {r[:2] for r in rules}
    if _SPELLED[spelled] != len(groups):
        raise EvalRefusal(
            f"pre-registration: the text says «{spelled} regras» and defines {len(groups)} "
            f"({', '.join(sorted(groups))}) — the rules it defines and the ones it names"
            " have to be the same"
        )
    return PreRegistration(
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
        questions,
        replicas,
        float(f"{a[0]}.{a[1]}"),
        tuple(rules),
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as err:
        raise EvalRefusal(f"{path}: input file missing: {err.strerror}") from err


def load_preregistration(path: Path) -> PreRegistration:
    return parse_preregistration(_read(path))


def _reject_constant(name: str) -> float:
    """`json.loads` accepts `NaN`/`Infinity` by default; a number that is not a
    number can never become a verdict (the empty-cell law)."""
    raise EvalRefusal(f"results out of shape: non-standard JSON constant {name}")


def load_results(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(_read(path), parse_constant=_reject_constant)
    except json.JSONDecodeError as err:
        raise EvalRefusal(f"{path}: results out of shape: {err}") from err
    if not isinstance(data, dict):
        raise EvalRefusal(f"{path}: results out of shape: expected a JSON object")
    return data


def _need(data: Mapping[str, Any], path: str) -> Any:
    """The value at `a.b.c`, or a refusal NAMING what was missing. Never
    returns 0."""
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise EvalRefusal(
                f"measurement missing, the matrix does not publish: {path}"
            )
        node = node[part]
    return node


def _number(data: Mapping[str, Any], path: str) -> float:
    """The numeric value at `a.b.c`, or a refusal. `True`, `"3"` and `NaN` are
    not measurements: a wrong type never satisfies a rule silently."""
    raw = _need(data, path)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise EvalRefusal(f"{path}: {raw!r} is not a numeric measurement")
    value = float(raw)
    if not math.isfinite(value):
        raise EvalRefusal(f"{path}: {value} is not finite and never wins silently")
    return value


def _series(data: Mapping[str, Any], path: str, replicas: int) -> list[float]:
    raw = _need(data, path)
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise EvalRefusal(f"{path}: expected a series per replica")
    if len(raw) != replicas:
        raise EvalRefusal(
            f"{path}: {len(raw)} replicas, the pre-registration requires {replicas} replicas"
        )
    out: list[float] = []
    for i, x in enumerate(raw):
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise EvalRefusal(f"{path}[{i}]: {x!r} is not a numeric measurement")
        value = float(x)
        if not math.isfinite(value):
            raise EvalRefusal(f"{path}[{i}]: {value} is not finite")
        out.append(value)
    return out


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _tail(hits: int, n: int) -> float:
    """P(X ≥ hits) with n trials and p = 0.5 — the exact one-sided sign test."""
    if n == 0:
        return 1.0
    tail: int = sum(math.comb(n, i) for i in range(hits, n + 1))
    # `ldexp(t, -n)` is `t / 2**n` without going through `int ** int`, which
    # the typeshed types as `Any` (it may give a float with a negative
    # exponent).
    return math.ldexp(tail, -n)


#: The text requires `before` and `after` in the SAME hermetic run, with the
#: SAME `k` and the SAME budget in TOKENS (not in items). Divergence here is
#: not a weaker verdict — it is another experiment, and a refusal.
ARM_IDENTITY: Final = ("run_id", "dataset_sha256", "binary", "k", "token_budget")


def _identity(data: Mapping[str, Any]) -> dict[str, Any]:
    before = {field: _need(data, f"identity.before.{field}") for field in ARM_IDENTITY}
    after = {field: _need(data, f"identity.after.{field}") for field in ARM_IDENTITY}
    divergent = [f for f in ARM_IDENTITY if before[f] != after[f]]
    if divergent:
        detail = "; ".join(f"{f}: {before[f]!r} ≠ {after[f]!r}" for f in divergent)
        raise EvalRefusal(
            "the two arms are not the same experiment, does not publish: " + detail
        )
    return before


def _r1(data: Mapping[str, Any], pre: PreRegistration) -> tuple[bool, dict[str, Any]]:
    before_t = _series(data, "cost.before.tokens", pre.replicas)
    after_t = _series(data, "cost.after.tokens", pre.replicas)
    before_c = _series(data, "cost.before.turns", pre.replicas)
    after_c = _series(data, "cost.after.turns", pre.replicas)
    # The censored sum over the intersection of hits goes out TOGETHER or
    # does not publish.
    intersection = {
        "before": _number(data, "cost.intersection.before"),
        "after": _number(data, "cost.intersection.after"),
    }
    usd_before = _number(data, "cost.usd.before")
    usd_after = _number(data, "cost.usd.after")
    production_usd = _number(data, "production.usd")
    production_turns = _number(data, "production.turns")

    saving = _mean(before_t) - _mean(after_t)
    # The setup's own noise: the variation between replicas of the SAME arm.
    noise = max(max(before_t) - min(before_t), max(after_t) - min(after_t))
    turns_ok = _mean(after_c) <= _mean(before_c)
    saving_usd = usd_before - usd_after
    per_question = saving_usd / pre.questions if pre.questions else 0.0
    reported = {
        "saving_tokens": saving,
        "saving_turns": _mean(before_c) - _mean(after_c),
        "replica_noise_tokens": noise,
        "saving_tokens_on_intersection": intersection["before"] - intersection["after"],
        "saving_usd": saving_usd,
        "production_usd": production_usd,
        "production_turns": production_turns,
        # DERIVED break-even, not chosen: how many questions until the saving
        # pays for what producing and reviewing the digests cost. The
        # acceptable horizon is the owner's product decision; here it is just
        # the number.
        "break_even_questions": (
            production_usd / per_question if per_question > 0 else None
        ),
    }
    return saving > noise and turns_ok, reported


def _r2a(data: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    before_all = _number(data, "recall.before.all")
    after_all = _number(data, "recall.after.all")
    before_any = _number(data, "recall.before.any")
    after_any = _number(data, "recall.after.any")
    # The per-k gain is reported separately: half of the any/all gap closes
    # only at k=10.
    k10 = {
        "all": _number(data, "recall.before_k10.all"),
        "any": _number(data, "recall.before_k10.any"),
    }
    reported = {
        "recall_all_delta": after_all - before_all,
        "recall_any_delta": after_any - before_any,
        "before_k10": k10,
        "k": _need(data, "k"),
    }
    return after_all > before_all and after_any >= before_any, reported


def _r2b(data: Mapping[str, Any], pre: PreRegistration) -> tuple[bool, dict[str, Any]]:
    pairs = _need(data, "accuracy.pairs")
    if not isinstance(pairs, Sequence) or len(pairs) != pre.questions:
        n = len(pairs) if isinstance(pairs, Sequence) else 0
        raise EvalRefusal(
            f"accuracy.pairs: {n} pairs for the {pre.questions} questions of Q"
        )
    worse: list[str] = []
    better: list[str] = []
    for pair in pairs:
        name = str(pair.get("question", "?"))
        before = _series(pair, "before", pre.replicas)
        after = _series(pair, "after", pre.replicas)
        for arm, series in (("before", before), ("after", after)):
            bad = [x for x in series if x not in (0.0, 1.0)]
            if bad:
                raise EvalRefusal(
                    f"accuracy.pairs[{name}].{arm}: {bad} is not hit/miss per"
                    " replica — paired accuracy is 0 or 1"
                )
        # The per-question score is the NUMBER OF correct replicas, and the
        # discordance is between those scores. Swapped replicas with the same
        # score (`[1,0]` vs `[0,1]`) are neither a demonstrated nor a
        # reproducible loss — it is the READER's noise, which the file itself
        # names, and `r = 4` exists to measure it.
        if sum(after) == sum(before):
            continue
        # Discordant: the file orders them read one by one, and the reading is
        # mandatory.
        if not str(pair.get("reading", "")).strip():
            raise EvalRefusal(
                f"discordant pair {name} without its reading: statistics to"
                " triage, reading to judge"
            )
        (worse if sum(after) < sum(before) else better).append(name)
    p = _tail(len(worse), len(worse) + len(better))
    reported = {
        "discordant_worse": worse,
        "discordant_better": better,
        "p_one_sided": p,
        "alpha": pre.alpha,
    }
    return p > pre.alpha, reported


#: The adjudicable object is the `(claim, locus)` pair, and whoever decomposes
#: is a model: without model, prompt, and sha in the receipt, a zero measures
#: the prompt, not the system.
DECOMPOSITION: Final = ("model", "prompt_sha256")
VERDICTS: Final = ("supported", "fabrication", "unresolved")


def _r3(data: Mapping[str, Any], pre: PreRegistration) -> tuple[bool, dict[str, Any]]:
    adjudications = _need(data, "fidelity.adjudications")
    digests = int(_number(data, "fidelity.digests"))
    unverifiable = list(_need(data, "fidelity.unverifiable"))
    if unverifiable:
        raise EvalRefusal(
            f"unverifiable provenance in {sorted(unverifiable)} of {digests} "
            "digests: does not count as pass or as fail, does not publish"
        )
    decomposition = {
        f: _need(data, f"fidelity.decomposition.{f}") for f in DECOMPOSITION
    }
    marks = [str(m) for m in _need(data, "fidelity.marks")]
    # High-sensitivity sieve: over-marking is the desired outcome, and EVERY
    # mark requires naming the locus. A mark without adjudication is the
    # finding to be decided, not dropped by omission.
    adjudicated = {str(a.get("claim")) for a in adjudications}
    pendentes = [m for m in marks if m not in adjudicated]
    if pendentes:
        raise EvalRefusal(
            f"sieve marks without adjudication: {pendentes} — no mark is a"
            " verdict by itself, and none dies by omission"
        )
    for a in adjudications:
        verdict = a.get("verdict")
        if verdict not in VERDICTS:
            raise EvalRefusal(
                f"adjudication of {a.get('claim')!r} with verdict {verdict!r}"
                f" outside {list(VERDICTS)}"
            )
        if verdict == "supported" and not str(a.get("locus", "")).strip():
            raise EvalRefusal(
                f"adjudication of {a.get('claim')!r} says supported without naming the"
                " locus: the locus is the mechanical, auditable burden"
            )
    unresolved = [
        str(a.get("claim")) for a in adjudications if a.get("verdict") == "unresolved"
    ]
    if unresolved:
        raise EvalRefusal(
            f"adjudication unresolved in {', '.join(unresolved)}: unresolved"
            " disagreement does not publish"
        )
    fabrications = [
        str(a.get("claim")) for a in adjudications if a.get("verdict") == "fabrication"
    ]
    # The blind control is proved, not declared: a judge guessing the digest
    # above chance invalidates the number.
    guesses = int(_number(data, "fidelity.blind.guesses"))
    correct = int(_number(data, "fidelity.blind.correct"))
    if guesses <= 0:
        raise EvalRefusal(
            "the blind control is proven, not declared: zero guesses is not a control"
        )
    if not 0 <= correct <= guesses:
        raise EvalRefusal(f"blind: {correct} hits in {guesses} guesses is not a count")
    blind_p = _tail(correct, guesses)
    if blind_p <= pre.alpha:
        raise EvalRefusal(
            f"blind broken: the judge guessed {correct}/{guesses} (p={blind_p:.4f}"
            f" ≤ α={pre.alpha}) which of the two was the digest"
        )
    reported = {
        "fabrications": fabrications,
        "sieve_marks": len(marks),
        "decomposition": decomposition,
        "adjudicated": len(adjudications),
        "blind_p": blind_p,
        "unverifiable": unverifiable,
    }
    return not fabrications, reported


def judge(
    data: Mapping[str, Any],
    prereg: PreRegistration,
    expected_sha256: str | None = None,
) -> Verdict:
    """The file's three rules over one run, conjunctive.

    `expected_sha256` is the sha256 of the first run's text: a different text
    is a refusal, never a verdict with new numbers.
    """
    if expected_sha256 is not None and expected_sha256 != prereg.sha256:
        raise EvalRefusal(
            f"pre-registration: sha256 of the text read {prereg.sha256} ≠ expected "
            f"{expected_sha256}"
        )
    questions = int(_need(data, "questions"))
    if questions != prereg.questions:
        raise EvalRefusal(
            f"the run measured {questions} questions and the pre-registration fixes "
            f"{prereg.questions}: it is another set, not the same verdict"
        )
    proposed = int(_number(data, "promotion.proposed"))
    promoted = int(_number(data, "promotion.promoted"))
    expired = int(_number(data, "promotion.expired"))
    if proposed <= 0:
        raise EvalRefusal(
            "promotion.proposed = 0: without proposals there is no promotion rate to"
            " report, and the owner receives the verdict WITH it"
        )
    if promoted + expired > proposed:
        raise EvalRefusal(
            f"promotion: {promoted} promoted + {expired} expired exceed"
            f" {proposed} proposed"
        )
    promotion = {
        "promotion_rate": promoted / proposed,
        "expiry_rate": expired / proposed,
    }
    identity = _identity(data)
    checks = {
        "R1": _r1(data, prereg),
        "R2a": _r2a(data),
        "R2b": _r2b(data, prereg),
        "R3": _r3(data, prereg),
    }
    missing = [r for r in prereg.rules if r not in checks]
    if missing:
        raise EvalRefusal(
            f"the pre-registration defines rules this operator does not compute: "
            f"{missing}"
        )
    reported: dict[str, Any] = {"identity": identity, **promotion}
    for rule, (_, detail) in checks.items():
        reported.update(
            {f"{rule}.{k}" if k in reported else k: v for k, v in detail.items()}
        )
    return Verdict(
        prereg.sha256,
        tuple(r for r in prereg.rules if not checks[r][0]),
        reported,
    )
