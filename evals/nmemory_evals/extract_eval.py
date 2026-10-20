"""Replay evaluator of `nmemory_turn_extract` over the real transcripts (U3).

Runs the deterministic extractor over the TURNS of every transcript the gold
set declares and measures Precision and Recall against the human labels.

The anchor is VERBATIM and is the only one available: a turn is POSITIVE when
a statement of that session's gold set occurs literally in it (whitespace
normalized), and NEGATIVE otherwise. This is what PLAN.md:139 already required
of the gold set — «o texto verbatim». Matching by similarity would be the
judge inventing the verdict (PLAN.md:146), and that is why a session without a
single anchored statement is not measured with zero positives: it is a TYPED
D18 REFUSAL (exit 2), because precision over a corpus without positives is a
number that does not measure the extractor.

The negatives are the non-persistable turns of the transcript itself. The
hand-written list of ten strings is gone: it measured the extractor against
what its author already expected of it.

The `.jsonl` is NEVER committed (PLAN.md:157-160) — it carries env, tool
output, and possibly secrets. Only the external path enters the gold set.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nmemory.extract import (
    ExtractCandidate,
    extract,
)
from nmemory.extract_llm import (
    LlmProposal,
    Proposer,
    propose,
)

from nmemory_evals.capture_gap import (
    EvalRefusal,
    GoldCaptureItem,
    load_capture_gold_set,
    transcript_turns,
)


def normalize(text: str) -> str:
    """Collapses whitespace and case. It is the ONLY slack the match admits: a
    transcript breaks lines where the gold set does not, and nothing else."""
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass(frozen=True)
class ExtractMetrics:
    tp: int
    fn: int
    fp: int
    tn: int
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True)
class SessionEval:
    session_id: str
    transcript_path: str
    total_statements: int
    #: Gold set statements found verbatim in the session's transcript.
    anchored_statements: int
    turns: int
    positive_turns: int
    metrics: ExtractMetrics
    #: This session's D18 refusal reason, or `None` when it was measured.
    refusal: str | None


@dataclass(frozen=True)
class ExtractEvalReport:
    sessions_measured: int
    sessions_refused: int
    #: Sessions whose gold set declares no `transcript_path`. Not refusals:
    #: the evaluator does not see them, and saying so differs from counting
    #: them as zero.
    sessions_without_transcript: list[str]
    total_statements: int
    anchored_statements: int
    total_turns: int
    metrics: ExtractMetrics
    by_session: list[SessionEval]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions_measured": self.sessions_measured,
            "sessions_refused": self.sessions_refused,
            "sessions_without_transcript": self.sessions_without_transcript,
            "total_statements": self.total_statements,
            "anchored_statements": self.anchored_statements,
            "total_turns": self.total_turns,
            "metrics": asdict(self.metrics),
            "by_session": [asdict(s) for s in self.by_session],
        }


def _compute_metrics(tp: int, fn: int, fp: int, tn: int) -> ExtractMetrics:
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (
        2.0 * (precision * recall) / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    return ExtractMetrics(
        tp=tp,
        fn=fn,
        fp=fp,
        tn=tn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
    )


def evaluate_session(
    session_id: str, transcript_path: str, statements: list[str]
) -> SessionEval:
    empty = _compute_metrics(0, 0, 0, 0)
    try:
        turns = transcript_turns(Path(transcript_path))
    except EvalRefusal as err:
        return SessionEval(
            session_id=session_id,
            transcript_path=transcript_path,
            total_statements=len(statements),
            anchored_statements=0,
            turns=0,
            positive_turns=0,
            metrics=empty,
            refusal=str(err),
        )

    needles = [normalize(s) for s in statements]
    haystacks = [normalize(t) for t in turns]
    anchored = sum(1 for n in needles if any(n in h for h in haystacks))

    if anchored == 0:
        return SessionEval(
            session_id=session_id,
            transcript_path=transcript_path,
            total_statements=len(statements),
            anchored_statements=0,
            turns=len(turns),
            positive_turns=0,
            metrics=empty,
            refusal=(
                f"none of the {len(statements)} gold set statements appears "
                f"verbatim in the {len(turns)} turns of {transcript_path}: without "
                "an anchor there is no positive, and precision over a corpus without "
                "positives does not measure the extractor. The gold set has to carry the "
                "verbatim text (PLAN.md:139)"
            ),
        )

    tp = fn = fp = tn = 0
    positive_turns = 0
    for turn, hay in zip(turns, haystacks, strict=True):
        is_positive = any(n in hay for n in needles)
        fires = bool(extract(turn))
        if is_positive:
            positive_turns += 1
            if fires:
                tp += 1
            else:
                fn += 1
        elif fires:
            fp += 1
        else:
            tn += 1

    return SessionEval(
        session_id=session_id,
        transcript_path=transcript_path,
        total_statements=len(statements),
        anchored_statements=anchored,
        turns=len(turns),
        positive_turns=positive_turns,
        metrics=_compute_metrics(tp=tp, fn=fn, fp=fp, tn=tn),
        refusal=None,
    )


def evaluate_extractor(items: list[GoldCaptureItem] | None = None) -> ExtractEvalReport:
    if items is None:
        items = load_capture_gold_set()

    declared: dict[str, str] = {}
    statements: dict[str, list[str]] = {}
    missing: list[str] = []
    for item in items:
        if item.transcript_path is None:
            if item.session_id not in missing:
                missing.append(item.session_id)
            continue
        declared.setdefault(item.session_id, item.transcript_path)
        statements.setdefault(item.session_id, []).append(item.statement)

    by_session = [
        evaluate_session(sid, declared[sid], statements[sid])
        for sid in sorted(declared)
    ]
    measured = [s for s in by_session if s.refusal is None]

    return ExtractEvalReport(
        sessions_measured=len(measured),
        sessions_refused=len(by_session) - len(measured),
        sessions_without_transcript=sorted(missing),
        total_statements=sum(s.total_statements for s in by_session),
        anchored_statements=sum(s.anchored_statements for s in by_session),
        total_turns=sum(s.turns for s in by_session),
        metrics=_compute_metrics(
            tp=sum(s.metrics.tp for s in measured),
            fn=sum(s.metrics.fn for s in measured),
            fp=sum(s.metrics.fp for s in measured),
            tn=sum(s.metrics.tn for s in measured),
        ),
        by_session=by_session,
    )


def render_extract_eval_markdown(report: ExtractEvalReport) -> str:
    m = report.metrics
    lines = [
        "# Evaluation of the Deterministic Extractor (`nmemory_turn_extract`) over transcripts",
        "",
        f"- **Sessions Measured**: {report.sessions_measured}",
        f"- **Sessions REFUSED (D18 exit 2)**: {report.sessions_refused}",
        (
            f"- **Sessions without `transcript_path`**: "
            f"{', '.join(report.sessions_without_transcript) or '—'}"
        ),
        f"- **Gold Set Statements**: {report.total_statements}",
        f"- **Anchored verbatim in the transcript**: {report.anchored_statements}",
        f"- **Turns Read**: {report.total_turns}",
        f"- **True Positives (TP)**: {m.tp}",
        f"- **False Negatives (FN)**: {m.fn}",
        f"- **False Positives (FP)**: {m.fp}",
        f"- **True Negatives (TN)**: {m.tn}",
        "",
        "## Global Metrics (over the measured sessions only)",
        "",
        f"- **Precision**: **{m.precision * 100:.1f}%**",
        f"- **Recall**: **{m.recall * 100:.1f}%**",
        f"- **F1-Score**: **{m.f1 * 100:.1f}%**",
        "",
        "## Per Session",
        "",
        "| Session | Statements | Anchored | Turns | TP | FN | FP | Precision | Recall |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for s in report.by_session:
        if s.refusal is not None:
            lines.append(
                f"| `{s.session_id}` | {s.total_statements} | 0 | {s.turns} | "
                "— | — | — | REFUSED | REFUSED |"
            )
            continue
        sm = s.metrics
        lines.append(
            f"| `{s.session_id}` | {s.total_statements} | {s.anchored_statements} | "
            f"{s.turns} | {sm.tp} | {sm.fn} | {sm.fp} | "
            f"**{sm.precision * 100:.1f}%** | **{sm.recall * 100:.1f}%** |"
        )

    refused = [s for s in report.by_session if s.refusal is not None]
    if refused:
        lines.extend(["", "## Typed Refusals", ""])
        for s in refused:
            lines.append(f"- `{s.session_id}`: {s.refusal}")

    return "\n".join(lines) + "\n"


OVERLAP = 0.5

#: `(turn_index, start, end)` — the shape common to both extractors.
SpanTriple = tuple[int, int, int]


@dataclass(frozen=True)
class GoldSpan:
    item_id: str
    turn_index: int
    start: int
    end: int


@dataclass(frozen=True)
class SpanBlock:
    proposals: int
    #: Proposals that capture at least one gold.
    capturing: int
    #: Gold captured by at least one proposal.
    captured: int
    recall_spans: float
    precision_spans: float
    #: Deterministic candidates whose content is not verbatim in the turn.
    #: Always 0 in the `llm` block: `propose()` never lets one be born.
    unanchorable: int


@dataclass(frozen=True)
class TranscriptSpans:
    transcript: str
    turns: int
    gold_anchored: int
    #: block → (proposals, capturing, captured)
    blocks: dict[str, tuple[int, int, int]]
    #: The detail that lets the measurement be sliced LATER without re-running
    #: it: the gold spans, each block's proposals, and the gold ids each block
    #: captured.
    gold: list[GoldSpan] = field(default_factory=list)
    proposals: dict[str, list[SpanTriple]] = field(default_factory=dict)
    captured: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class SpanEvalReport:
    proposer: str
    gold_items: int
    gold_anchored: int
    unanchored: list[tuple[str, str]]
    transcripts: list[str]
    turns: int
    blocks: dict[str, SpanBlock]
    by_transcript: list[TranscriptSpans] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposer": self.proposer,
            "overlap": OVERLAP,
            "gold_items": self.gold_items,
            "gold_anchored": self.gold_anchored,
            "gold_unanchored": len(self.unanchored),
            "unanchored": [{"id": i, "reason": r} for i, r in self.unanchored],
            "transcripts": list(self.transcripts),
            "turns": self.turns,
            "blocks": {name: asdict(b) for name, b in self.blocks.items()},
            "by_transcript": [
                {
                    "transcript": t.transcript,
                    "turns": t.turns,
                    "gold_anchored": t.gold_anchored,
                    "gold": [asdict(g) for g in t.gold],
                    "blocks": {
                        name: {
                            "proposals": [list(s) for s in t.proposals[name]],
                            "capturing": c,
                            "captured": list(t.captured[name]),
                        }
                        for name, (_, c, _) in t.blocks.items()
                    },
                }
                for t in self.by_transcript
            ],
        }


def gold_spans(
    items: Sequence[GoldCaptureItem],
) -> tuple[dict[str, list[GoldSpan]], list[tuple[str, str]]]:
    """The gold spans per transcript, and the anchor-less items with their
    reasons. Zero anchored items is vacuity: a refusal, never `recall 0.0`."""
    by_path: dict[str, list[GoldSpan]] = {}
    unanchored: list[tuple[str, str]] = []
    for item in items:
        anchor = item.anchor
        if anchor is None:
            unanchored.append((item.id, item.anchor_reason or "sem anchor_reason"))
            continue
        by_path.setdefault(str(anchor["transcript_path"]), []).append(
            GoldSpan(
                item.id,
                int(anchor["turn_index"]),
                int(anchor["start"]),
                int(anchor["end"]),
            )
        )
    if not by_path:
        raise EvalRefusal(
            f"none of the {len(items)} gold set items has an anchor: without a "
            "gold span there is no denominator, and recall over zero items "
            "measures no extractor at all"
        )
    return by_path, unanchored


def _captures(g: GoldSpan, p: SpanTriple) -> bool:
    turn_index, start, end = p
    if turn_index != g.turn_index:
        return False
    overlap = min(g.end, end) - max(g.start, start)
    return overlap / (g.end - g.start) >= OVERLAP


def score_spans(
    gold: Sequence[GoldSpan], proposals: Sequence[SpanTriple]
) -> tuple[list[str], int]:
    """`(captured gold ids, proposals that capture)` under `overlap / len(gold) >= OVERLAP`."""
    captured = [g.item_id for g in gold if any(_captures(g, p) for p in proposals)]
    capturing = sum(1 for p in proposals if any(_captures(g, p) for g in gold))
    return captured, capturing


def deterministic_spans(
    turns: Sequence[str],
    extractor: Callable[[str], Sequence[ExtractCandidate]] = extract,
) -> tuple[list[SpanTriple], int]:
    """`extract(turn)` per turn, each content located by `turn.find`.
    Returns `(spans, unanchorable)`; a content that is not verbatim counts as
    `unanchorable` and is left out."""
    spans: list[SpanTriple] = []
    unanchorable = 0
    for turn_index, turn in enumerate(turns):
        for candidate in extractor(turn):
            start = turn.find(candidate.content)
            if start < 0:
                unanchorable += 1
                continue
            spans.append((turn_index, start, start + len(candidate.content)))
    return spans, unanchorable


def verify_proposals(path: Path, proposals: Sequence[LlmProposal]) -> list[SpanTriple]:
    """Re-reads the transcript and requires every proposal to be its turn's slice."""
    fresh = transcript_turns(path)
    verified: list[SpanTriple] = []
    for p in proposals:
        turn = fresh[p.turn_index] if p.turn_index < len(fresh) else ""
        if turn[p.start : p.end] != p.content:
            raise EvalRefusal(
                f"proposal {p.turn_index}:{p.start}-{p.end} of {path} is not verbatim: "
                f"turn {p.turn_index} re-read from the transcript does not hold those "
                "bytes, and an extractor that fabricates is not scored"
            )
        verified.append((p.turn_index, p.start, p.end))
    return verified


def _block(
    proposals: int, capturing: int, captured: int, anchored: int, unanchorable: int
) -> SpanBlock:
    return SpanBlock(
        proposals=proposals,
        capturing=capturing,
        captured=captured,
        recall_spans=round(captured / anchored, 4) if anchored else 0.0,
        precision_spans=round(capturing / proposals, 4) if proposals else 0.0,
        unanchorable=unanchorable,
    )


def evaluate_spans(
    items: Sequence[GoldCaptureItem],
    proposer: Proposer | None,
    extractor: Callable[[str], Sequence[ExtractCandidate]] = extract,
) -> SpanEvalReport:
    """The `deterministic` block and, with a proposer, `llm`, over the SAME gold."""
    by_path, unanchored = gold_spans(items)
    names = ["deterministic"] + (["llm"] if proposer is not None else [])
    totals = {name: [0, 0, 0] for name in names}
    unanchorable = 0
    by_transcript: list[TranscriptSpans] = []
    total_turns = 0
    for path in sorted(by_path):
        gold = by_path[path]
        turns = transcript_turns(Path(path))
        total_turns += len(turns)
        spans_by_name: dict[str, list[SpanTriple]] = {}
        spans_by_name["deterministic"], missing = deterministic_spans(turns, extractor)
        unanchorable += missing
        if proposer is not None:
            spans_by_name["llm"] = verify_proposals(
                Path(path), propose(turns, proposer)
            )
        blocks: dict[str, tuple[int, int, int]] = {}
        captured_ids: dict[str, list[str]] = {}
        for name, spans in spans_by_name.items():
            captured_ids[name], capturing = score_spans(gold, spans)
            blocks[name] = (len(spans), capturing, len(captured_ids[name]))
            for slot, value in enumerate(blocks[name]):
                totals[name][slot] += value
        by_transcript.append(
            TranscriptSpans(
                path, len(turns), len(gold), blocks, gold, spans_by_name, captured_ids
            )
        )

    anchored = sum(len(g) for g in by_path.values())
    return SpanEvalReport(
        proposer="llm" if proposer is not None else "deterministic",
        gold_items=len(items),
        gold_anchored=anchored,
        unanchored=unanchored,
        transcripts=sorted(by_path),
        turns=total_turns,
        blocks={
            name: _block(
                proposals,
                capturing,
                captured,
                anchored,
                unanchorable if name == "deterministic" else 0,
            )
            for name, (proposals, capturing, captured) in totals.items()
        },
        by_transcript=by_transcript,
    )


def render_span_eval_markdown(report: SpanEvalReport) -> str:
    lines = [
        f"# Recall de spans — `extract-eval --proposer {report.proposer}`",
        "",
        f"- **Itens do gold set**: {report.gold_items}",
        f"- **Itens ancorados**: {report.gold_anchored}",
        f"- **Itens sem anchor (fora do denominador)**: {len(report.unanchored)}",
        f"- **Transcripts**: {len(report.transcripts)} · **Turnos**: {report.turns}",
        f"- **Regra**: proposta no mesmo turno com overlap/len(gold) >= {OVERLAP}",
        "",
        "| Bloco | Propostas | Captam | Captados | recall_spans | precision_spans | unanchorable |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, b in report.blocks.items():
        lines.append(
            f"| `{name}` | {b.proposals} | {b.capturing} | {b.captured} | "
            f"**{b.recall_spans * 100:.1f}%** | **{b.precision_spans * 100:.1f}%** | "
            f"{b.unanchorable} |"
        )
    lines.extend(["", "## Por transcript", ""])
    for t in report.by_transcript:
        cells = " · ".join(
            f"`{name}` {p}/{c}/{g}" for name, (p, c, g) in t.blocks.items()
        )
        lines.append(
            f"- `{t.transcript}`: {t.turns} turnos, {t.gold_anchored} âncoras — "
            f"propostas/captam/captados: {cells}"
        )
    if report.unanchored:
        lines.extend(["", "## Sem anchor (contados à parte)", ""])
        for item_id, reason in report.unanchored:
            lines.append(f"- `{item_id}`: {reason}")
    return "\n".join(lines) + "\n"
