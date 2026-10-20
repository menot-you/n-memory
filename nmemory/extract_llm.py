"""LLM extraction: the model proposes OFFSETS, the code slices.

The deterministic extractor (`extract.py`) captures 23.8% of the gold set; the owner
decided on LLM extraction. NOTT's rule for this (Lock 10): an LLM proposal
starts `staged` and inherits taint from the BYTES of the source turn. The method: the LLM returns only `Span`s — turn, start, end, kind — and the content
is `turns[turn_index][start:end]`, sliced here. Fabrication is unrepresentable:
there is no path by which a byte not present in a turn can reach a capsule.

A proposer that returns a span that does not fit within the turns is broken:
`propose` rejects the ENTIRE call with `InvalidSpan` and returns nothing —
never "skips the bad span", never coerces. NO NETWORK here: the transport (claude -p)
lives at the port, in another member (N5b). A `Proposer` is any callable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from . import NMemoryError
from .extract import CandidateKind


class ProposalError(NMemoryError):
    """Root refusal class for this module."""


class InvalidSpan(ProposalError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"proposal rejected: {reason}; nothing was proposed")
        self.reason = reason


@dataclass(frozen=True)
class Span:
    turn_index: int
    start: int
    end: int
    kind: CandidateKind

    def __post_init__(self) -> None:
        if self.turn_index < 0 or self.start < 0 or self.start >= self.end:
            raise InvalidSpan(
                f"span {self.turn_index}:{self.start}-{self.end} needs "
                "0 <= turn_index and 0 <= start < end"
            )
        if not isinstance(self.kind, CandidateKind):
            raise InvalidSpan(f"kind {self.kind!r} is not a CandidateKind")


class Proposer(Protocol):
    # Positional-only: `propose` calls `proposer(turns)` and never by keyword, so the
    # parameter NAME is not contract. Requiring it rejected every `Callable[[...], ...]` and
    # every function whose argument was not named `turns` — measured in `nmemory_evals.llm_proposer`,
    # which passes exactly one of these.
    def __call__(self, turns: Sequence[str], /) -> Sequence[Span]: ...


@dataclass(frozen=True)
class LlmProposal:
    content: str
    kind: CandidateKind
    derived_from: tuple[str, ...]
    turn_index: int
    start: int
    end: int

    def __post_init__(self) -> None:
        # `content` is the slice of the SINGLE source turn, or the proposal is not created.
        if (
            len(self.derived_from) != 1
            or self.content != self.derived_from[0][self.start : self.end]
        ):
            raise InvalidSpan(
                f"content of {self.turn_index}:{self.start}-{self.end} is not the "
                "verbatim slice of its single source turn"
            )


def propose(turns: Sequence[str], proposer: Proposer) -> list[LlmProposal]:
    proposals: list[LlmProposal] = []
    for span in proposer(turns):
        if span.turn_index >= len(turns):
            raise InvalidSpan(
                f"turn_index {span.turn_index} is beyond the {len(turns)} turns given"
            )
        turn = turns[span.turn_index]
        if span.end > len(turn):
            raise InvalidSpan(
                f"span {span.start}-{span.end} runs past the {len(turn)} characters "
                f"of turn {span.turn_index}"
            )
        content = turn[span.start : span.end]
        if not content.strip():
            raise InvalidSpan(
                f"span {span.turn_index}:{span.start}-{span.end} slices only whitespace"
            )
        proposals.append(
            LlmProposal(
                content=content,
                kind=span.kind,
                derived_from=(turn,),
                turn_index=span.turn_index,
                start=span.start,
                end=span.end,
            )
        )
    return proposals
