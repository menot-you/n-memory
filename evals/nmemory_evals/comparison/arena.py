"""The arena's verdict operator: it reads the pre-registration, it does not
invent thresholds.

Every number that decides «nMEMORY is not the first» (the pre-registration's
loss consequence) comes from the text of
`evals/nmemory/tests/fixtures/PRE-REGISTERED-LOSS.txt`: the `B_GRID` grid, the
`LIVE` set, each metric's arity, and the rules
`- **X.n** (…): `∃ v ∈ CONJUNTO, … : termo ≤|≥ termo` → o nMEMORY NÃO é o primeiro`
(`∃ v ∈ SET, … : term ≤|≥ term → nMEMORY is NOT the first`).
What that grammar does not cover is ambiguity and REFUSAL (`EvalRefusal`,
D18 exit 2) with the line named — the operator never interprets. An empty cell
in the table is never worth 0 and never fires: the matrix does not publish
(a refusal naming the missing cells). Conjunctive tie-break: any rule that
fires is defeat; the file must name exactly the rules it defines.

The pre-registration moved byte for byte (identity 73f7063bf at docs/plans/nmemory-supremacy/PRE-REGISTERED-LOSS.md, base 233dd4aaa).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from nmemory_evals.capture_gap import EvalRefusal
from nmemory_evals.comparison.base import Receipt

#: (system, metric, budget) → value; budget `None` for metrics without a `b`.
Cell = tuple[str, str, int | None]
LOSS = "o nMEMORY NÃO é o primeiro"
_LIVE_SYSTEMS: frozenset[str] = frozenset({"Zep", "Letta", "mem0"})

_B_GRID = re.compile(r"`B_GRID = \{([^}]*)\}`")
_LIVE = re.compile(r"`LIVE` = \{([^}]*)\}")
_NAMED = re.compile(r"^- `(\w+)` = ", re.MULTILINE)
_METRIC = re.compile(r"^- `(\w+)\(S(, b)?\)` = ", re.MULTILINE)
_RULE_ID = re.compile(r"^- \*\*([A-Z]\.\d+)\*\*")
_RULE = re.compile(r"^- \*\*[A-Z]\.\d+\*\* \([^)]*\): `([^`]*)` → (.+)$")
_TIEBREAK = re.compile(r"nenhuma das \S+ regras ((?:[A-Z]\.\d+(?:, )?)+)\s+dispar")
_QUANT = re.compile(r"^∃ (\w+) ∈ (B_GRID|LIVE)$")
_COMPARE = re.compile(r"^(.+?) (≤|≥) (.+)$")
_CALL = re.compile(r"^(\w+)\((\w+)(?:, (\w+))?\)$")
_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")


@dataclass(frozen=True)
class Term:
    """`metric(system[, budget])` or a literal number."""

    metric: str | None = None
    system: str | None = None
    budget: str | None = None
    number: float | None = None


@dataclass(frozen=True)
class Rule:
    id: str
    line: int
    quantifiers: tuple[tuple[str, str], ...]
    left: Term
    op: str
    right: Term


@dataclass(frozen=True)
class PreRegistration:
    """What the parser read. `sha256` is of the TEXT read, so the verdict can
    name what produced it; the identity the file names for itself is the git
    sha (`git log -1 --format=%h -- <file>`), not this one. The law «after the
    first run no number changes» is imposed from outside: whoever judges passes
    the first run's `expected_sha256` to `judge`, and divergence is a refusal.
    """

    sha256: str
    b_grid: tuple[int, ...]
    live: tuple[str, ...]
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class Verdict:
    preregistration_sha256: str
    fired: tuple[str, ...]

    @property
    def first(self) -> bool:
        return not self.fired

    @property
    def sentence(self) -> str | None:
        return None if self.first else LOSS


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _one(pattern: re.Pattern[str], text: str, what: str) -> tuple[str, int]:
    """The captured value and its line; 0 or 2+ definitions refuse, naming the lines."""
    found = list(pattern.finditer(text))
    if len(found) != 1:
        lines = ", ".join(str(_line(text, m.start())) for m in found)
        where = f" (lines {lines})" if found else ""
        raise EvalRefusal(
            f"pre-registration: {what} defined {len(found)} times{where}, expected 1"
        )
    return found[0].group(1), _line(text, found[0].start())


def _term(
    raw: str,
    rule_id: str,
    line: int,
    bound: set[str],
    systems: set[str],
    metrics: dict[str, bool],
    b_grid: tuple[int, ...],
) -> Term:
    if _NUMBER.match(raw):
        return Term(number=float(raw))
    call = _CALL.match(raw)
    if call is None:
        raise EvalRefusal(
            f"pre-registration line {line} ({rule_id}): term {raw!r} outside the grammar"
        )
    metric, system, budget = call.groups()
    if metric not in metrics:
        raise EvalRefusal(
            f"pre-registration line {line} ({rule_id}): metric {metric!r} not defined"
        )
    if metrics[metric] != (budget is not None):
        raise EvalRefusal(
            f"pre-registration line {line} ({rule_id}): arity of {metric!r} does not match"
        )
    if system not in bound and system not in systems:
        raise EvalRefusal(
            f"pre-registration line {line} ({rule_id}): system {system!r} unbound"
        )
    if budget is not None and budget not in bound:
        if not _NUMBER.match(budget):
            raise EvalRefusal(
                f"pre-registration line {line} ({rule_id}): budget {budget!r} unbound"
            )
        if budget not in {str(b) for b in b_grid}:
            raise EvalRefusal(
                f"pre-registration line {line} ({rule_id}): budget {budget} outside B_GRID"
            )
    return Term(metric=metric, system=system, budget=budget)


def _rule(
    joined: str,
    line: int,
    systems: set[str],
    metrics: dict[str, bool],
    b_grid: tuple[int, ...],
) -> Rule:
    rid = _RULE_ID.match(joined)
    assert rid is not None
    rule_id = rid.group(1)
    m = _RULE.match(joined)
    if m is None:
        raise EvalRefusal(
            f"pre-registration line {line} ({rule_id}): rule outside the grammar"
        )
    expr, consequence = m.groups()
    if not consequence.startswith(LOSS):
        raise EvalRefusal(
            f"pre-registration line {line} ({rule_id}): consequence is not «{LOSS}»"
        )
    quant_part, _, compare = expr.rpartition(" : ")
    quantifiers: list[tuple[str, str]] = []
    for q in quant_part.split(", ") if quant_part else []:
        qm = _QUANT.match(q)
        if qm is None:
            raise EvalRefusal(
                f"pre-registration line {line} ({rule_id}): quantifier {q!r} outside the grammar"
            )
        quantifiers.append((qm.group(1), qm.group(2)))
    cm = _COMPARE.match(compare)
    if cm is None or compare.count("≤") + compare.count("≥") != 1:
        raise EvalRefusal(
            f"pre-registration line {line} ({rule_id}): comparison {compare!r} is neither ≤ nor ≥"
        )
    bound = {v for v, _ in quantifiers}
    left = _term(cm.group(1), rule_id, line, bound, systems, metrics, b_grid)
    right = _term(cm.group(3), rule_id, line, bound, systems, metrics, b_grid)
    return Rule(rule_id, line, tuple(quantifiers), left, cm.group(2), right)


def parse_preregistration(text: str) -> PreRegistration:
    b_grid = tuple(int(x) for x in _one(_B_GRID, text, "B_GRID")[0].split(", "))
    live = tuple(_one(_LIVE, text, "LIVE")[0].split(", "))
    systems = (set(_NAMED.findall(text)) - {"LIVE"}) | set(live)
    metrics = {name: bool(b) for name, b in _METRIC.findall(text)}
    lines = text.splitlines()
    rules: list[Rule] = []
    for n, raw in enumerate(lines, 1):
        if not _RULE_ID.match(raw):
            continue
        parts = [raw]
        for follow in lines[n:]:
            if not follow.startswith("  "):
                break
            parts.append(follow.strip())
        rules.append(_rule(" ".join(parts), n, systems, metrics, b_grid))
    ids = [r.id for r in rules]
    for rid in sorted(set(ids)):
        at = [r.line for r in rules if r.id == rid]
        if len(at) > 1:
            where = ", ".join(str(x) for x in at)
            raise EvalRefusal(f"pre-registration: rule {rid} repeated on lines {where}")
    tiebreak, tb_line = _one(_TIEBREAK, text, "tie-break")
    named = tiebreak.split(", ")
    if sorted(named) != sorted(ids):
        raise EvalRefusal(
            f"pre-registration line {tb_line} (tie-break): names {named}, "
            f"rules defined {ids}"
        )
    return PreRegistration(
        hashlib.sha256(text.encode("utf-8")).hexdigest(), b_grid, live, tuple(rules)
    )


def _bindings(rule: Rule, prereg: PreRegistration) -> list[dict[str, str | int]]:
    domains: dict[str, tuple[str | int, ...]] = {
        "B_GRID": prereg.b_grid,
        "LIVE": prereg.live,
    }
    names = [v for v, _ in rule.quantifiers]
    return [
        dict(zip(names, values, strict=True))
        for values in itertools.product(*(domains[d] for _, d in rule.quantifiers))
    ]


def _cell(term: Term, binding: Mapping[str, str | int]) -> Cell:
    assert term.metric is not None and term.system is not None
    system = str(binding.get(term.system, term.system))
    budget: int | None = None
    if term.budget is not None:
        budget = int(binding.get(term.budget, term.budget))
    return system, term.metric, budget


def _value(
    term: Term, binding: Mapping[str, str | int], table: Mapping[Cell, float]
) -> float:
    return term.number if term.number is not None else table[_cell(term, binding)]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as err:
        raise EvalRefusal(f"{path}: input file missing: {err.strerror}") from err


def load_preregistration(path: Path) -> PreRegistration:
    return parse_preregistration(_read(path))


def load_table(path: Path, *, require_receipts: bool = True) -> dict[Cell, float]:
    """Reads `[{"system", "metric", "budget" (or null), "value"}, …]` into the verdict's table.

    Validates vendor `Receipt` records (`endpoint`, `sdk_version`, `response_id`,
    `data`), fixed `reader`/`judge` identities, and `corpus_sha256` consistency
    for every `LIVE` vendor row by default (`require_receipts=True`).
    """
    try:
        rows = json.loads(_read(path))
        if not isinstance(rows, list):
            raise TypeError("results table must be a JSON list")
        has_any_receipt = require_receipts or any(
            isinstance(r, dict) and "receipt" in r for r in rows
        )
        table: dict[Cell, float] = {}
        role_refs: dict[str, tuple[str, str]] = {}
        corpus_by_metric: dict[str, str] = {}
        for r in rows:
            if not isinstance(r, dict):
                raise TypeError("each row must be an object")
            system = str(r["system"])
            metric = str(r["metric"])
            budget = None if r["budget"] is None else int(r["budget"])
            value = float(r["value"])
            if system in _LIVE_SYSTEMS and (has_any_receipt or "receipt" in r):
                raw_rec = r.get("receipt")
                if not isinstance(raw_rec, dict):
                    raise EvalRefusal(
                        f"{path}: LIVE system {system!r} ({metric}, {budget}) missing receipt"
                    )
                try:
                    Receipt(**raw_rec)
                except TypeError as err:
                    raise EvalRefusal(
                        f"{path}: LIVE system {system!r} invalid receipt: {err}"
                    ) from err
            for role in ("reader", "judge"):
                if role in r and r[role] is not None:
                    blk = r[role]
                    if (
                        not isinstance(blk, dict)
                        or not str(blk.get("model", "")).strip()
                        or not str(blk.get("prompt_sha", "")).strip()
                    ):
                        raise EvalRefusal(
                            f"{path}: row ({system}, {metric}, {budget}) has invalid {role} identity"
                        )
                    ident = (str(blk["model"]).strip(), str(blk["prompt_sha"]).strip())
                    prev = role_refs.get(role)
                    if prev is not None and prev != ident:
                        raise EvalRefusal(
                            f"{path}: divergent {role} identity across cells: {prev} vs {ident}"
                        )
                    role_refs[role] = ident
            if "corpus_sha256" in r and r["corpus_sha256"] is not None:
                csha = str(r["corpus_sha256"]).strip()
                if not csha:
                    raise EvalRefusal(
                        f"{path}: row ({system}, {metric}, {budget}) has blank corpus_sha256"
                    )
                prev_csha = corpus_by_metric.get(metric)
                if prev_csha is not None and prev_csha != csha:
                    raise EvalRefusal(
                        f"{path}: divergent corpus_sha256 for {metric}: {prev_csha} vs {csha}"
                    )
                corpus_by_metric[metric] = csha
            cell: Cell = (system, metric, budget)
            if cell in table and table[cell] != value:
                raise EvalRefusal(
                    f"{path}: conflicting values for cell {cell}: {table[cell]} vs {value}"
                )
            table[cell] = value
        return table
    except EvalRefusal:
        raise
    except (KeyError, TypeError, ValueError) as err:
        raise EvalRefusal(f"{path}: results table of the wrong shape: {err!r}") from err


def judge(
    table: Mapping[Cell, float],
    prereg: PreRegistration,
    expected_sha256: str | None = None,
) -> Verdict:
    """Computes the defeat sentence by ≤/≥ over the table, exactly as the file defines.

    `expected_sha256` is the sha256 of the first run's text: a different text
    is a refusal, never a verdict with new numbers.
    """
    if expected_sha256 is not None and expected_sha256 != prereg.sha256:
        raise EvalRefusal(
            f"pre-registration: sha256 of the text read {prereg.sha256} ≠ expected {expected_sha256}"
        )
    bad = sorted((c for c, v in table.items() if not math.isfinite(v)), key=str)
    if bad:
        cells = "; ".join(f"{s}, {m}, {b}" for s, m, b in bad)
        raise EvalRefusal(f"a non-finite cell never wins silently: {cells}")
    needed: set[Cell] = set()
    for rule in prereg.rules:
        for binding in _bindings(rule, prereg):
            for term in (rule.left, rule.right):
                if term.number is None:
                    needed.add(_cell(term, binding))
    missing = sorted(needed - set(table), key=str)
    if missing:
        cells = "; ".join(f"{s}, {m}, {b}" for s, m, b in missing)
        raise EvalRefusal(f"cell missing, the matrix does not publish: {cells}")
    fired = tuple(
        rule.id
        for rule in prereg.rules
        if any(
            (_value(rule.left, b, table) <= _value(rule.right, b, table))
            if rule.op == "≤"
            else (_value(rule.left, b, table) >= _value(rule.right, b, table))
            for b in _bindings(rule, prereg)
        )
    )
    return Verdict(prereg.sha256, fired)
