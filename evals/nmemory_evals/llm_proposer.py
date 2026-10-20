"""The `claude -p` transport as a `Proposer` for `nmemory.extract_llm`.

Every spawn is the `claude` binary (the owner's law): the proposer calls it by
SUBPROCESS, with this box's own auth — never a third-party provider for the
owner's transcripts — and the prompt goes as the LAST argument after `--`:
turns are content, never a flag. `--output-format json --json-schema` returns
the envelope with `structured_output` already validated against the schema
(probed on 2026-09-02 with 2.1.258: the field is `structured_output`, the raw
text stays in `result`). `--tools "" --strict-mcp-config --setting-sources ""`
strip the child's tools, MCP, and settings; `--no-session-persistence` keeps 59
sessions with the owner's real turns from being written to
`~/.claude/projects/`. Measured: from 52,445 cache tokens and US$0.21 per call
down to 5,594 and US$0.024.

THE WIRE IS NOT THE DoD'S, and the reason is measured (pilot 2026-09-02, turn 0
of 6da118b7, 37 anchors, 18,721 characters, `claude-sonnet-5`): asking the
model for OFFSETS gave 2 proposals, 1/37 caught, and 20,930 output tokens spent
counting characters; asking for the VERBATIM passage
(`{"turn_index", "quote", "kind"}`) and resolving `start = turn.find(quote)`
here gave 57 proposals and 31/37. What the DoD protects stays intact: the
`Span` that reaches `propose()` is still offsets over the turn's bytes,
`propose()` re-verifies, and a quote that is not in the turn is a rejection —
never an approximation. A quote repeated in the turn resolves to the FIRST
occurrence — a declared choice, never «the best one» — and is COUNTED in the
receipt (`ambiguous_quotes`, and per batch the absolute spans), so whoever
reads the number knows how many gold may have been caught by an ambiguous
citation. The receipt names the wire (`wire`) for the same reason: whoever
reads the number must know what it measured.

A batch is ≤ `batch_turns` turns in a single prompt, numbered from 0 within
the batch; the absolute indices are `first_turn + relative`.

**Admission is PER ELEMENT, and the radius of a refusal is the measurement that
paid for it.** A bad element — a quote missing from the turn, a kind outside
the enum, a turn_index outside the batch, an `InvalidSpan` from the core — is
DISCARDED ALONE, with its reason in `dropped`, and the good ones in the SAME
batch survive. Before, the first bad element raised `BatchRejected` and killed
the whole batch: four runs of the same batch gave
`0.2162 · 0.0 · 0.0 · 0.0` and the three null ones spent 33,302, 10,530, and
22,936 output tokens to return ZERO proposals. The reason names the QUOTE, not
just the index — without the quote there is no fixture of the real defect,
which is why this defect survived four runs.

The BATCH still dies whole for two causes, and only those: the payload's SHAPE
(without `{"spans": [...]}` there is no element to admit) and an unreadable
envelope. That is `BatchRejected`, COUNTED in `refusals` and RE-ASKED once. A
batch where everything was dropped and nothing survived counts a refusal all
the same and is re-asked: the reasons appear on BOTH axes — `dropped` per
element, `refusals` per attempt — because they count different things. If it
rejects again, the batch counts zero proposals and is named in the receipt. A
transport failure (missing binary, non-zero exit, timeout) is not re-asked: it
is an `EvalRefusal` and it rises — the eval does not judge without an oracle.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from nmemory.extract import CandidateKind
from nmemory.extract_llm import (
    InvalidSpan,
    Span,
    propose,
)
from nmemory.provider import provider_env

from nmemory_evals.capture_gap import EvalRefusal

#: The historical fallback: the model of the 2026-09-05 pilot. Only valid when
#: no provider is declared in the env — see `default_model`.
DEFAULT_MODEL = "claude-sonnet-5"
WIRE = "quote-resolved-by-find"


def default_model() -> str:
    """The model the owner's law mandates, resolved from the env.

    Owner's law (2026-09-15, translated from Portuguese): «I don't want sonnet,
    it burns my quota. use zai».
    This host's env carries `ZAI_DEFAULT_MODEL`; the owner's
    recipe exports it as `ANTHROPIC_MODEL`. Whoever exported it by hand WINS,
    then comes `ZAI_*`, and the historical literal is only the end of the line —
    a fixed name `claude-sonnet-5` asked the Z.ai endpoint for a model it does
    not serve, and the receipt recorded a model that never ran (measured
    2026-09-16).
    """
    for var in ("ANTHROPIC_MODEL", "ZAI_DEFAULT_MODEL"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return DEFAULT_MODEL


#: What the model sees before the turns. Chosen over TWO iterations on turn 0
#: of 6da118b7 (the pilot above); no gold text entered here.
PROMPT = (
    "You are reading numbered conversation turns between an engineer and an agent. "
    "Propose every passage worth keeping as long-term memory: a fact, a decision, a "
    "procedure, a constraint, a preference, a failure pattern, a capability. Be "
    "exhaustive: a long turn such as a pasted report may hold dozens of such passages, "
    "and each one is its own proposal. Each proposal MUST be a VERBATIM passage copied "
    "character-for-character from exactly one turn, never new prose, never a "
    "paraphrase, never spanning two turns. kind is one of: "
    + ", ".join(k.value for k in CandidateKind)
    + '. Answer with the JSON object {"spans": [{"turn_index": int, "quote": str, '
    '"kind": str}, ...]} and nothing else: no prose, no markdown fence.\n\n'
)

#: The note the SECOND question carries, between the base prompt and the turns.
#: Measured: a retry's two prompts were byte-identical, and `rep-3` refused
#: `element 0: kind 'project' is not a CandidateKind` twice, identically.
#: The refusal reason already existed in `record.refusals` — it was being
#: thrown away.
RETRY = (
    "Your previous answer to this exact request was REJECTED. The reason, "
    "verbatim: {reason}\nThis is the retry: fix exactly that and answer again, "
    "in the same JSON object and nothing else.\n\n"
)

SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "spans": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "turn_index": {"type": "integer"},
                        "quote": {"type": "string"},
                        # DERIVED from `CandidateKind`, never copied: a new
                        # kind in `extract.py` reaches the wire without editing
                        # this file. The prompt's prose already named them; the
                        # WIRE did not.
                        "kind": {
                            "type": "string",
                            "enum": [k.value for k in CandidateKind],
                        },
                    },
                    "required": ["turn_index", "quote", "kind"],
                },
            }
        },
        "required": ["spans"],
    }
)

FLAGS: tuple[str, ...] = (
    "-p",
    "--output-format",
    "json",
    "--tools",
    "",
    "--strict-mcp-config",
    "--setting-sources",
    "",
    "--no-session-persistence",
    "--json-schema",
    SCHEMA,
)

WIRE_KEYS = frozenset({"turn_index", "quote", "kind"})

#: `(claude args, timeout_s) -> stdout`. The real one pins the binary from the
#: PATH; the test passes a double that returns the recorded envelope.
Transport = Callable[[Sequence[str], int], str]


class ProposerError(EvalRefusal):
    """Root of this module's refusals: D18 exit 2 when it rises to the CLI."""


class BatchRejected(ProposerError):
    """A batch whose answer is not the requested shape. Counted and re-asked once."""


class TransportFailure(ProposerError):
    """The binary did not answer: non-zero exit or timeout. Rises, is not re-asked."""


class ClaudeUnavailable(ProposerError):
    """No `claude` in the PATH."""


def claude_transport() -> Transport:
    binary = shutil.which("claude")
    if binary is None:
        raise ClaudeUnavailable(
            "no `claude` binary on PATH: no transport, nothing to propose"
        )

    def run(args: Sequence[str], timeout_s: int) -> str:
        try:
            # `stdin=DEVNULL`: without it `claude -p` waits 3 s for stdin and
            # warns. cwd in /tmp: no CLAUDE.md to discover up the path.
            done = subprocess.run(
                [binary, *args],
                cwd="/tmp",
                # The owner's recipe applied to the child (`nmemory.provider`):
                # without it the env went out raw and the model followed the law
                # while the ENDPOINT did not — argv asked Anthropic for `glm-…`
                # with no key (review 220001fa). Half the law is none.
                env=provider_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as err:
            raise TransportFailure(f"claude timed out after {timeout_s}s") from err
        if done.returncode != 0:
            raise TransportFailure(
                f"claude exit {done.returncode}: {done.stderr.strip()[:500]}"
            )
        return done.stdout

    return run


def parse_spans(payload: object, batch: Sequence[str]) -> tuple[list[Span], list[str]]:
    """`structured_output` → `(Span`s relative to the batch, reasons of the dropped)`.

    Typed refusal, never coercion: `bool` is not `int`, `"0"` is not `0`,
    `FACT` is not `fact`, a quote missing from the turn is not approximated.
    What changes with per-element admission is the RADIUS: the payload's SHAPE
    raises `BatchRejected` because without it there is no element to admit; a
    bad ELEMENT is returned in `dropped`, naming its quote, and does not touch
    its siblings.

    The quote enters via `repr` truncated to 200: a turn has 18,721 measured
    characters and a fabricated quote can be nearly all of it — the receipt
    does not swallow it whole —, and `repr` equally names a quote that is not
    even a `str`.
    """
    if not isinstance(payload, dict) or set(payload) != {"spans"}:
        raise BatchRejected(
            f"payload is not {{'spans': [...]}}: {type(payload).__name__}"
        )
    elements = payload["spans"]
    if not isinstance(elements, list):
        raise BatchRejected(f"spans is not an array: {type(elements).__name__}")
    spans: list[Span] = []
    dropped: list[str] = []
    for i, element in enumerate(elements):
        if not isinstance(element, dict) or set(element) != WIRE_KEYS:
            dropped.append(f"element {i} is not {{turn_index, quote, kind}}")
            continue
        turn_index, quote, kind = (
            element["turn_index"],
            element["quote"],
            element["kind"],
        )
        if type(turn_index) is not int or not 0 <= turn_index < len(batch):
            dropped.append(
                f"element {i}: turn_index {turn_index!r} is not in "
                f"[0, {len(batch)}), quote {quote!r:.200}"
            )
            continue
        if type(quote) is not str or not quote:
            dropped.append(
                f"element {i}: quote {quote!r:.200} is not a non-empty string"
            )
            continue
        if type(kind) is not str or kind not in {k.value for k in CandidateKind}:
            dropped.append(
                f"element {i}: kind {kind!r} is not a CandidateKind, "
                f"quote {quote!r:.200}"
            )
            continue
        start = batch[turn_index].find(quote)
        if start < 0:
            dropped.append(
                f"element {i}: quote {quote!r:.200} is not verbatim "
                f"in turn {turn_index}"
            )
            continue
        spans.append(Span(turn_index, start, start + len(quote), CandidateKind(kind)))
    return spans, dropped


def _envelope(stdout: str) -> dict[str, Any]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as err:
        raise BatchRejected(f"claude output is not JSON: {err}") from err
    if not isinstance(data, dict):
        raise BatchRejected(f"claude output is not an object: {type(data).__name__}")
    if data.get("is_error") is not False:
        raise BatchRejected(
            f"claude reported an error: {str(data.get('result'))[:200]}"
        )
    if "structured_output" not in data:
        raise BatchRejected("claude output carries no structured_output")
    return data


def _int_field(usage: object, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    return value if type(value) is int else None


@dataclass
class BatchRecord:
    #: The proposer's call (one per transcript in the eval) and the batch within it.
    call: int
    batch: int
    first_turn: int
    turns: int
    attempts: int = 0
    proposals: int = 0
    #: The BATCH's refusals, one per attempt: payload shape, unreadable envelope,
    #: or nothing survived admission.
    refusals: list[str] = field(default_factory=list)
    #: Per-ELEMENT refusals, each naming the quote that caused it. An element
    #: here did not keep any sibling from being proposed.
    dropped: list[str] = field(default_factory=list)
    #: ABSOLUTE spans whose quote occurs more than once in the turn (first occurrence).
    ambiguous: list[tuple[int, int, int]] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass
class ProposerReceipt:
    model: str
    claude_version: str
    called_at: str
    batch_turns: int
    timeout_s: int
    flags: list[str]
    batches: list[BatchRecord] = field(default_factory=list)

    @property
    def proposer_refusals(self) -> int:
        return sum(len(b.refusals) for b in self.batches)

    @property
    def batches_zeroed(self) -> int:
        return sum(1 for b in self.batches if b.attempts == 2 and len(b.refusals) == 2)

    @property
    def elements_dropped(self) -> int:
        return sum(len(b.dropped) for b in self.batches)

    @property
    def ambiguous_quotes(self) -> int:
        return sum(len(b.ambiguous) for b in self.batches)

    def to_dict(self) -> dict[str, Any]:
        inputs = [b.input_tokens for b in self.batches]
        outputs = [b.output_tokens for b in self.batches]
        costs = [b.cost_usd for b in self.batches]
        return {
            "wire": WIRE,
            "model": self.model,
            "claude_version": self.claude_version,
            "called_at": self.called_at,
            "batch_turns": self.batch_turns,
            "timeout_s": self.timeout_s,
            "flags": list(self.flags),
            "proposer_refusals": self.proposer_refusals,
            "elements_dropped": self.elements_dropped,
            "batches_zeroed": self.batches_zeroed,
            "ambiguous_quotes": self.ambiguous_quotes,
            "tokens": {
                "input": None
                if None in inputs
                else sum(i for i in inputs if i is not None),
                "output": None
                if None in outputs
                else sum(o for o in outputs if o is not None),
                "cost_usd": None
                if None in costs
                else round(sum(c for c in costs if c is not None), 6),
            },
            "batches": [asdict(b) for b in self.batches],
        }


class ClaudeProposer:
    """A `Proposer` over `claude -p`. A validating constructor; the receipt accumulates."""

    def __init__(
        self,
        model: str | None = None,
        batch_turns: int = 20,
        timeout_s: int = 120,
        *,
        transport: Transport | None = None,
    ) -> None:
        # `None` = «the provider the owner's law mandates»; a blank string
        # remains the caller's error, not a silent default.
        model = default_model() if model is None else model
        if batch_turns < 1 or timeout_s < 1 or not model.strip():
            raise ValueError(
                "batch_turns >= 1, timeout_s >= 1 and a model name are required"
            )
        self._transport = transport if transport is not None else claude_transport()
        self._timeout_s = timeout_s
        self._batch_turns = batch_turns
        self._model = model
        self._calls = 0
        self.receipt = ProposerReceipt(
            model=model,
            claude_version=self._transport(["--version"], timeout_s).strip(),
            called_at=datetime.now(UTC).isoformat(),
            batch_turns=batch_turns,
            timeout_s=timeout_s,
            flags=list(FLAGS),
        )

    def __call__(self, turns: Sequence[str]) -> list[Span]:
        call = self._calls
        self._calls += 1
        spans: list[Span] = []
        for batch_index, first in enumerate(range(0, len(turns), self._batch_turns)):
            batch = turns[first : first + self._batch_turns]
            record = BatchRecord(call, batch_index, first, len(batch))
            self.receipt.batches.append(record)
            for span in self._ask(batch, record):
                turn = batch[span.turn_index]
                if turn.count(turn[span.start : span.end]) > 1:
                    record.ambiguous.append(
                        (first + span.turn_index, span.start, span.end)
                    )
                spans.append(
                    Span(first + span.turn_index, span.start, span.end, span.kind)
                )
        return spans

    def _ask(self, batch: Sequence[str], record: BatchRecord) -> list[Span]:
        turns = "".join(f"### turn {i}\n{t}\n" for i, t in enumerate(batch))
        while record.attempts < 2:
            record.attempts += 1
            # INSIDE the loop: the second question carries the first refusal's
            # reason, where the model reads it. Built outside, the re-ask was
            # byte-identical.
            note = RETRY.format(reason=record.refusals[-1]) if record.refusals else ""
            args = [*FLAGS, "--model", self._model, "--", PROMPT + note + turns]
            try:
                envelope = _envelope(self._transport(args, self._timeout_s))
            except BatchRejected as err:
                record.refusals.append(str(err))
                continue
            _account(record, envelope)
            try:
                relative, dropped = parse_spans(envelope["structured_output"], batch)
            except BatchRejected as err:
                record.refusals.append(str(err))
                continue
            kept = [span for span in relative if _fenced(batch, span, dropped)]
            record.dropped.extend(dropped)
            if dropped and not kept:
                # Nothing survived: re-asking is what remains, and the reason
                # gets counted.
                record.refusals.append("; ".join(dropped))
                continue
            record.proposals = len(kept)
            return kept
        return []


def _fenced(batch: Sequence[str], span: Span, dropped: list[str]) -> bool:
    """The core's fence, PER ELEMENT. `propose()` refuses the WHOLE call by law
    (`extract_llm.py:10-12`) — it never «skips the bad span» —, so the call
    becomes one of a single span: the core's law stays intact and the bad one
    dies alone. The refusal remains the core's; this module only counts it."""
    try:
        propose(batch, _constant([span]))
    except InvalidSpan as err:
        quote = batch[span.turn_index][span.start : span.end]
        dropped.append(f"quote {quote!r:.200}: {err}")
        return False
    return True


def _account(record: BatchRecord, envelope: dict[str, Any]) -> None:
    """Sums the tokens and cost the envelope declares; whatever is missing stays `None`."""
    usage = envelope.get("usage")
    prompt_tokens = _int_field(usage, "input_tokens")
    if prompt_tokens is not None:
        cached = (
            _int_field(usage, "cache_creation_input_tokens") or 0,
            _int_field(usage, "cache_read_input_tokens") or 0,
        )
        record.input_tokens = (record.input_tokens or 0) + prompt_tokens + sum(cached)
    output_tokens = _int_field(usage, "output_tokens")
    if output_tokens is not None:
        record.output_tokens = (record.output_tokens or 0) + output_tokens
    cost = envelope.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        record.cost_usd = (record.cost_usd or 0.0) + float(cost)


def _constant(spans: Sequence[Span]) -> Callable[[Sequence[str]], Sequence[Span]]:
    def proposer(_turns: Sequence[str]) -> Sequence[Span]:
        return spans

    return proposer
