"""Reader of the PrecisionMemBench harness report (arXiv 2605.11325).

The pre-registration defines `precision_A(S, b)` as the precision EXACTLY AS
THE HARNESS reports it, over the suite's `active` cases, for system `S` at
budget `b ∈ B_GRID`. This reader reads the harness's REAL shape, measured
against it on 2026-09-17 (`commit 61a712a1`, 77 non-session cases):

* `provider` is the name the harness gives the arm;
* `retrieval.meanPrecision` is ITS aggregate — that is the one that counts, and
  the reader checks it against the cases to catch a file edited after the run;
* `cases[].retrievalPrecision` is `null` when the case does not exercise
  retrieval, and THAT is the `active` boundary: there is no `status` field.

## The budget does not come from the harness, so it is declared and falsifiable

The harness budgets in COUNTS — `ContextBudget = {maxBeliefs: 20,
maxPinnedFacts: 10, maxQuestions: 15}` — and none of the 77 cases defines its
own budget. The pre-registration's `B_GRID` is in TOKENS. So the run must
declare, in the `nott_budget` block, which member of `B_GRID` it measured, the
counts it resolved, and the worst case in tokens those counts produce; the
reader refuses if that worst case does not FIT the declared budget, or if the
declared system is not the harness's `provider`. Without this, `token_budget`
was free text and any run could call itself any cell.

## What is verified, and what is merely declared

Independent review of #609, BLOCKING finding: a self-consistent block does not
prove the EXECUTED budget. What stayed verifiable and what did not:

* the COUNTS are **declared and cross-checked**: the harness writes
  `provenance.appliedBudget` and the reader refuses if the declared one is not
  exactly that. Second pass of the #609 review, accepted: this proves
  CONSISTENCY, not origin — whoever edits one field edits both. Calling it the
  «executor's receipt» claimed more than the file supports. Truly closing this
  needs executor evidence linked by a reliable external reference;
* the TOKEN conversion stays **declared, not verified**: it depends on the
  corpus and the tokenizer, which do not live in this repository. That is why
  the field is called `declared_worst_case_tokens` and not `worst_case_tokens`.
  Closing this needs the serialized per-question context as measured by the
  executor;
* the SEEDING is executor evidence, not a score inference: the harness writes
  `ingestion.perBelief` with one entry per seeded belief, and that is what the
  reader requires. The first version inferred «store not seeded» from
  `meanRecall == 0` — the review's second pass showed the implication is FALSE
  (zero recall can be a valid run that returned irrelevant things) and that
  inferring validity from the score itself can exclude legitimate losses.

**Three of the `B_GRID`'s four columns are the same configuration.** Measured
2026-09-17: the suite's whole corpus is 2431 tokens (35 beliefs, `cl100k_base`),
so 4096, 8192 and 16384 all resolve for the 35 beliefs and the 4 questions.
That is a property of the benchmark the pre-registration chose, not of the
measurement: `collapsed_budgets` names them so the table does not look like
four independent observations — grouped by (system, tokenizer, counts), because
equal counts on different systems are not the same observation. Known limit:
DIFFERENT counts can produce identical contexts (35 and 70 on a 35-belief
corpus), and this grouping does not see that.

`active` = `retrievalPrecision is not None` is the denominator the harness
aggregate uses, `entries.map(r => r.retrievalPrecision)
.filter(v => v !== null)` at `buildRetrievalReport.ts:104-106`. It includes
zero precision and `passed: false` cases; using only the passes, or only
positive precision, would bias the population upward. The producer quote is
documentary — the LOCAL contract is pinned by the fixture and the tests; the
equivalence with upstream is declared by commit and lines, not executed here.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from nmemory_evals.capture_gap import EvalRefusal
from nmemory_evals.comparison.arena import Cell

#: The counts a run resolved: beliefs, pinned facts, questions.
Resolved = tuple[int, int, int]

_BUDGET_KEYS: Final = (
    "system",
    "token_budget",
    "max_beliefs",
    "max_pinned_facts",
    "max_questions",
    "tokenizer",
    "declared_worst_case_tokens",
)

#: The tokenizers whose name this campaign measured. A free-form name left
#: `declared_worst_case_tokens` without a unit (#609 review finding).
_TOKENIZERS: Final = frozenset({"cl100k_base", "o200k_base"})

#: Slack ONLY for the float's binary representation. The aggregate is not
#: compared by distance: it is compared against the producer's UNIQUE rounding
#: result. The #609 review's second pass showed any distance leaves an interval
#: where A.1 (`precision_A ≤ 0.22`) flips sides — with 5e-5, mean 0.22 and
#: aggregate 0.22004 both passed and A.1 stopped firing.
_FLOAT_SLACK: Final = 1e-12


def _js_round4(x: float) -> float:
    """The producer's `Math.round(x*10000)/10000`, tie-breaking included.

    JavaScript's `Math.round` rounds halves UP (toward +∞); Python's `round`
    goes to the even. Without reproducing the tie-break, a legitimate aggregate
    of a case sitting exactly at the middle would have been refused.
    """
    return math.floor(x * 10000.0 + 0.5) / 10000.0


@dataclass(frozen=True)
class PrecisionReport:
    system: str
    token_budget: int
    active_cases: int
    precision_a: float
    resolved: Resolved
    tokenizer: str

    @property
    def cell(self) -> tuple[Cell, float]:
        return (self.system, "precision_A", self.token_budget), self.precision_a


def _positive_int(bloco: dict[str, Any], chave: str) -> int:
    valor = bloco[chave]
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        raise EvalRefusal(
            f"nott_budget.{chave} = {valor!r}: a positive integer, or there is no cell"
        )
    return valor


def _exact_int(bloco: dict[str, Any], chave: str) -> int:
    """An EXACT integer, no coercion: `24.9` is not 24 and is not 25."""
    valor = bloco[chave]
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise EvalRefusal(f"{chave} = {valor!r}: an integer, no coercion")
    return valor


def _texto(bloco: dict[str, Any], chave: str) -> str:
    valor = bloco[chave]
    if not isinstance(valor, str) or not valor.strip():
        raise EvalRefusal(
            f"nott_budget.{chave} empty: without it the cell is not identifiable"
        )
    return valor


def _finite_fraction(valor: Any, onde: str) -> float:
    """A number in [0, 1] and FINITE. `json.loads` accepts `NaN` and `Infinity`,
    and `abs(NaN - x) > tol` is false: without this, `NaN` went straight through
    the aggregate check (#609 review finding)."""
    if isinstance(valor, bool) or not isinstance(valor, int | float):
        raise EvalRefusal(f"{onde}: not a number: {valor!r}")
    f = float(valor)
    if not math.isfinite(f) or not 0.0 <= f <= 1.0:
        raise EvalRefusal(f"{onde}: outside [0, 1] or not finite: {valor!r}")
    return f


def read_precision_report(path: Path, *, b_grid: tuple[int, ...]) -> PrecisionReport:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not {"provider", "retrieval", "cases"} <= set(raw):
        raise EvalRefusal(
            f"{path}: does not have the harness shape (`provider`, `retrieval`, `cases`) "
            "— a report that did not come from the harness does not produce `precision_A`"
        )
    bloco = raw.get("nott_budget")
    if not isinstance(bloco, dict) or not set(_BUDGET_KEYS) <= set(bloco):
        faltam = sorted(
            set(_BUDGET_KEYS) - set(bloco if isinstance(bloco, dict) else {})
        )
        raise EvalRefusal(
            f"{path}: `nott_budget` absent or incomplete (missing {faltam}) — the "
            "harness budgets in counts and B_GRID in tokens, so the run "
            "declares the cell it measured or it does not enter the table"
        )

    provider = str(raw["provider"])
    system = _texto(bloco, "system")
    if system != provider:
        raise EvalRefusal(
            f"{path}: nott_budget.system {system!r} is not the harness provider "
            f"{provider!r} — that relabels one vendor's arm with another name"
        )

    budget = _positive_int(bloco, "token_budget")
    if budget not in b_grid:
        raise EvalRefusal(
            f"{path}: token_budget {budget} is not a member of the B_GRID read from the "
            f"pre-registration {list(b_grid)} — a cell outside the grid decides "
            "no rule and would enter the table as if it did"
        )
    resolved: Resolved = (
        _positive_int(bloco, "max_beliefs"),
        _positive_int(bloco, "max_pinned_facts"),
        _positive_int(bloco, "max_questions"),
    )
    tokenizer = _texto(bloco, "tokenizer")
    if tokenizer not in _TOKENIZERS:
        raise EvalRefusal(
            f"{path}: tokenizer {tokenizer!r} is not one of the measured ones "
            f"({sorted(_TOKENIZERS)}) — without a known unit the declared "
            "worst case is not a number of tokens"
        )
    pior = _positive_int(bloco, "declared_worst_case_tokens")
    if pior > budget:
        raise EvalRefusal(
            f"{path}: the declared worst case ({pior} tokens) does not fit the declared "
            f"budget ({budget}) — the cell's label has to be falsifiable"
        )

    # The counts are the EXECUTOR's receipt: the harness writes the budget it
    # applied, and the declared one must be exactly that. Without this the block
    # was only self-consistent (BLOCKING finding of the #609 review).
    provenance = raw.get("provenance")
    if not isinstance(provenance, dict):
        raise EvalRefusal(
            f"{path}: `provenance` is not an object: {type(provenance).__name__}"
        )
    aplicado = provenance.get("appliedBudget")
    if not isinstance(aplicado, dict):
        raise EvalRefusal(
            f"{path}: `provenance.appliedBudget` absent — without the budget the "
            "harness applied, the declared counts are the run's word alone"
        )
    try:
        # NO coercion: `int(24.9)` truncated to 24 and matched a declared 24
        # (new defect flagged in the #609 review's 2nd pass).
        receipt: Resolved = (
            _exact_int(aplicado, "maxBeliefs"),
            _exact_int(aplicado, "maxPinnedFacts"),
            _exact_int(aplicado, "maxQuestions"),
        )
    except KeyError as err:
        raise EvalRefusal(
            f"{path}: `provenance.appliedBudget` without {err} — the shape the "
            "harness writes has all three counts"
        ) from err
    if receipt != resolved:
        raise EvalRefusal(
            f"{path}: the declared counts {resolved} are not the ones the harness "
            f"applied {receipt} — the cell's label does not describe this run"
        )

    casos = raw["cases"]
    if not isinstance(casos, list):
        raise EvalRefusal(f"{path}: `cases` is not a list")
    retrieval = raw["retrieval"] or {}
    if not isinstance(retrieval, dict):
        raise EvalRefusal(f"{path}: `retrieval` is not an object")
    # Counters against the list: deleting an unfavorable case and leaving the
    # counter standing passed without a refusal (#609 review finding).
    # MANDATORY, not optional: the #609 review's 2nd pass deleted the three
    # counters, adjusted the aggregate, and the run then fired A.1 on a single
    # case.
    for chave, medido in (
        ("caseCount", len(casos)),
        ("totalCases", len(casos)),
        (
            "totalPassed",
            sum(1 for c in casos if isinstance(c, dict) and c.get("passed")),
        ),
    ):
        if chave not in retrieval:
            raise EvalRefusal(
                f"{path}: `retrieval.{chave}` absent — without the harness counter "
                "there is no way to see that the case list changed after the run"
            )
        if retrieval[chave] != medido:
            raise EvalRefusal(
                f"{path}: `retrieval.{chave}` = {retrieval[chave]!r} but the cases "
                f"say {medido} — the case list changed after the run"
            )
    ids: list[str] = []
    for caso in casos:
        if not isinstance(caso, dict):
            raise EvalRefusal(f"{path}: a case is not an object: {caso!r}")
        cid = caso.get("caseId")
        if not isinstance(cid, str) or not cid.strip():
            raise EvalRefusal(
                f"{path}: case without a textual `caseId`: {cid!r} — without identity "
                "there is no way to see that a case disappeared"
            )
        ids.append(cid)
    if len(set(ids)) != len(ids):
        raise EvalRefusal(f"{path}: repeated `caseId` — the same question twice")

    # The SEEDING is executor evidence. It replaces the «zero recall therefore
    # empty store» inference, which the #609 review's 2nd pass showed to be
    # false: zero recall can be a valid run that returned irrelevant things, and
    # inferring validity from the score excludes legitimate losses.
    ingestao = raw.get("ingestion")
    if not isinstance(ingestao, dict):
        raise EvalRefusal(
            f"{path}: `ingestion` absent or out of shape — without proof that the "
            "store was seeded, this run's precision does not describe the product"
        )
    semeadas = ingestao.get("perBelief")
    if not isinstance(semeadas, list) or not semeadas:
        raise EvalRefusal(f"{path}: `ingestion.perBelief` empty: nothing was seeded")
    nomes = [
        b.get("beliefId")
        for b in semeadas
        if isinstance(b, dict) and isinstance(b.get("beliefId"), str) and b["beliefId"]
    ]
    if len(nomes) != len(semeadas) or len(set(nomes)) != len(nomes):
        raise EvalRefusal(
            f"{path}: `ingestion.perBelief` with an entry missing `beliefId` or duplicated"
        )
    declaradas = ingestao.get("beliefCount")
    if declaradas != len(semeadas):
        raise EvalRefusal(
            f"{path}: `ingestion.beliefCount` = {declaradas!r} but there are "
            f"{len(semeadas)} seeded beliefs"
        )
    ativos: list[float] = []
    for caso in casos:
        if not isinstance(caso, dict) or "retrievalPrecision" not in caso:
            raise EvalRefusal(f"{path}: a case without `retrievalPrecision`: {caso!r}")
        precisao = caso["retrievalPrecision"]
        if precisao is None:
            continue
        if isinstance(precisao, bool) or not isinstance(precisao, int | float):
            raise EvalRefusal(
                f"{path}: `retrievalPrecision` is not a number: {precisao!r}"
            )
        if not 0.0 <= float(precisao) <= 1.0:
            raise EvalRefusal(f"{path}: precision outside [0, 1]: {precisao!r}")
        ativos.append(float(precisao))
    if not ativos:
        raise EvalRefusal(
            f"{path}: no active case (every `retrievalPrecision` is null, or there "
            "are no cases); `precision_A` does not exist and an empty cell is not worth 0"
        )

    agregado = _finite_fraction(
        retrieval.get("meanPrecision"), f"{path}: `retrieval.meanPrecision`"
    )
    esperado = _js_round4(sum(ativos) / len(ativos))
    if abs(agregado - esperado) > _FLOAT_SLACK:
        raise EvalRefusal(
            f"{path}: the harness aggregate ({agregado}) is not the rounding "
            f"of the {len(ativos)} active cases ({esperado}) — the file changed "
            "after the run"
        )
    # Recall follows the SAME rule: an invented recall aggregate was the easiest
    # way to make a broken run look valid. And no validity is inferred from it:
    # zero recall can be a valid run that returned irrelevant things — what
    # proves the seeding is the `ingestion` block.
    recalls = [
        _finite_fraction(c["retrievalRecall"], f"{path}: `retrievalRecall`")
        for c in casos
        if isinstance(c, dict) and c.get("retrievalRecall") is not None
    ]
    recall_declarado = retrieval.get("meanRecall")
    if recalls:
        esperado_r = _js_round4(sum(recalls) / len(recalls))
        lido = _finite_fraction(recall_declarado, f"{path}: `retrieval.meanRecall`")
        if abs(lido - esperado_r) > _FLOAT_SLACK:
            raise EvalRefusal(
                f"{path}: `meanRecall` ({lido}) is not the rounding of the "
                f"{len(recalls)} cases with recall ({esperado_r})"
            )
    elif recall_declarado is not None:
        raise EvalRefusal(
            f"{path}: `meanRecall` = {recall_declarado!r} without a single case "
            "with recall — the aggregate does not come from the cases"
        )

    return PrecisionReport(
        system=system,
        token_budget=budget,
        active_cases=len(ativos),
        precision_a=agregado,
        resolved=resolved,
        tokenizer=tokenizer,
    )


#: harness `provider` → row of the pre-registration's table. Pinned on BOTH
#: sides: a provider not in here refuses, and a row the pre-registration does
#: not name refuses too. Without this the run picked the row its number fit
#: into, which is the same as picking the result.
_ROW_OF_PROVIDER: Final = {
    "nmemory": "nMEM",
    "zep": "Zep",
    "letta": "Letta",
    "mem0": "mem0",
}


def table_from_reports(
    paths: list[Path], known_systems: frozenset[str], b_grid: tuple[int, ...]
) -> dict[Cell, float]:
    """The `precision_A` cells from the reports, ready for the verdict.

    `known_systems` is the read pre-registration's `{nMEM} ∪ LIVE`: a row it
    does not name does not enter the table. Two cells with the same (row,
    budget) and different values refuse — it is the same arm measured twice, and
    picking one of them was picking the result.
    """
    tabela: dict[Cell, float] = {}
    for path in paths:
        report = read_precision_report(path, b_grid=b_grid)
        linha = _ROW_OF_PROVIDER.get(report.system)
        if linha is None:
            raise EvalRefusal(
                f"{path}: provider {report.system!r} has no known row "
                f"(known: {sorted(_ROW_OF_PROVIDER)})"
            )
        if linha not in known_systems:
            raise EvalRefusal(
                f"{path}: the row {linha!r} is not named by the pre-registration "
                f"({sorted(known_systems)}) — the table does not invent rows"
            )
        cell, valor = report.cell
        cell = (linha, cell[1], cell[2])
        anterior = tabela.get(cell)
        if anterior is not None and anterior != valor:
            raise EvalRefusal(
                f"{path}: the cell {cell} is already {anterior} and this report says "
                f"{valor} — two measurements of the same cell, and picking one was "
                "picking the result"
            )
        tabela[cell] = valor
    return tabela


def collapsed_budgets(
    reports: list[PrecisionReport],
) -> tuple[frozenset[int], ...]:
    """The groups of budgets that resolved to the SAME configuration.

    Two budgets with the same counts measured the same run under another name:
    they count as one observation, not two. Returns only the groups with more
    than one budget, ordered by smallest member.
    """
    por_config: dict[tuple[str, str, Resolved], set[int]] = {}
    for r in reports:
        por_config.setdefault((r.system, r.tokenizer, r.resolved), set()).add(
            r.token_budget
        )
    return tuple(
        frozenset(bs)
        for _, bs in sorted(
            ((min(bs), bs) for bs in por_config.values() if len(bs) > 1),
        )
    )
