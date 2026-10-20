"""Programmatic evaluation questions and ground truth derivation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from nmemory_evals.world import Event, EventKind, World, WorldConfig


class QuestionTemplate(StrEnum):
    WHO_DEPLOYED = "who_deployed"
    WHICH_DEPLOY_CAUSED = "which_deploy_caused"
    WHO_RESOLVED = "who_resolved"
    CURRENT_OWNER = "current_owner"
    PARAPHRASED_OWNER = "paraphrased_owner"
    OUT_OF_WORLD = "out_of_world"


@dataclass
class Question:
    qid: str
    template: QuestionTemplate
    question: str
    terms: list[str]
    answerable: bool
    golden_event_seqs: list[int]
    golden_capsule_ids: list[str]
    golden_answer: str


@dataclass(frozen=True)
class NarratedEvent:
    seq: int
    valid_from: str
    narrative: str
    #: The command that re-derives this event from its world (nMEMORY's `proof_hint`).
    proof_hint: str


@dataclass(frozen=True)
class BenchmarkCases:
    events: list[NarratedEvent]
    questions: list[Question] = field(default_factory=list)


class QaError(Exception):
    """Programmatic question derivation failure."""


def out_of_world_count(total: int) -> int:
    """Calculate the 20% out-of-world question allocation."""
    if total < 5:
        raise QaError("question config rejected: at least five questions are required")
    return max(1, total // 5)


def generate(world: World, total: int) -> list[Question]:
    """Generate programmatic questions with guaranteed goldens from world events."""
    out_of_world = out_of_world_count(total)
    answerable = total - out_of_world

    deploys = events_of_kind(world, EventKind.DEPLOY)
    incidents = events_of_kind(world, EventKind.INCIDENT_OPEN)
    resolutions = events_of_kind(world, EventKind.RESOLVE)
    owners = newest_owners(world)

    require_candidate(deploys, "who_deployed")
    require_candidate(incidents, "which_deploy_caused")
    require_candidate(resolutions, "who_resolved")
    require_candidate(owners, "current_owner")

    newest_transfer = max(owners, key=lambda event: event.seq, default=None)
    if newest_transfer is None:
        raise QaError("world has no candidate for template paraphrased_owner")

    questions: list[Question] = []
    questions.append(from_event(1, QuestionTemplate.PARAPHRASED_OWNER, newest_transfer))

    for index in range(1, answerable):
        template_index = (index - 1) % 4
        cycle = (index - 1) // 4
        if template_index == 0:
            template = QuestionTemplate.WHO_DEPLOYED
            source = deploys[cycle % len(deploys)]
        elif template_index == 1:
            template = QuestionTemplate.WHICH_DEPLOY_CAUSED
            source = incidents[cycle % len(incidents)]
        elif template_index == 2:
            template = QuestionTemplate.WHO_RESOLVED
            source = resolutions[cycle % len(resolutions)]
        else:
            template = QuestionTemplate.CURRENT_OWNER
            source = owners[cycle % len(owners)]
        questions.append(from_event(index + 1, template, source))

    for index in range(out_of_world):
        service = f"svc-{900 + index}"
        questions.append(
            Question(
                qid=f"q-{len(questions) + 1:03d}",
                template=QuestionTemplate.OUT_OF_WORLD,
                question=f"Who owns or deployed out-of-world service {service}?",
                terms=[service],
                answerable=False,
                golden_event_seqs=[],
                golden_capsule_ids=[],
                golden_answer="abstain",
            )
        )

    return questions


def proof_command(config: WorldConfig, seq: int) -> str:
    """The command that prints event `seq` of the world `config` generates.

    Every n1 capsule asserts a fact, and nMEMORY admits a fact only with proof someone
    can re-obtain (#686). The world is a pure function of its config, so the proof is
    to generate it again: `nmemory-evals scenario show` does, and prints the event.
    """
    return (
        f"nmemory-evals scenario show --seed {config.seed} --services {config.services}"
        f" --engineers {config.engineers} --events {config.events} --seq {seq}"
    )


def build_cases(
    world: World,
    total: int,
    render_fn: Callable[[Event], str],
    config: WorldConfig,
) -> BenchmarkCases:
    """Build narrated benchmark events and programmatic questions."""
    events = [
        NarratedEvent(
            seq=event.seq,
            valid_from=event.at,
            narrative=render_fn(event),
            proof_hint=proof_command(config, event.seq),
        )
        for event in world.events
    ]
    questions = generate(world, total)
    return BenchmarkCases(events=events, questions=questions)


def resolve_capsule_ids(
    questions: list[Question],
    capsule_by_event: dict[int, str],
) -> None:
    """Bind ingested capsule IDs to golden questions."""
    for question in questions:
        if question.answerable:
            resolved_ids: list[str] = []
            for seq in question.golden_event_seqs:
                cid = capsule_by_event.get(seq)
                if cid is None:
                    raise QaError(f"world event {seq} has no ingested capsule id")
                resolved_ids.append(cid)
            question.golden_capsule_ids = resolved_ids


def events_of_kind(world: World, kind: EventKind) -> list[Event]:
    return [event for event in world.events if event.kind == kind]


def newest_owners(world: World) -> list[Event]:
    newest: dict[str, Event] = {}
    for event in world.events:
        if event.kind == EventKind.OWNERSHIP_TRANSFER:
            newest[event.service] = event
    return [newest[k] for k in sorted(newest.keys())]


def require_candidate(rows: list[Event], template: str) -> None:
    if not rows:
        raise QaError(f"world has no candidate for template {template}")


def from_event(number: int, template: QuestionTemplate, event: Event) -> Question:
    match template:
        case QuestionTemplate.WHO_DEPLOYED:
            question = f"Who deployed {event.service} {event.version} in {event.id}?"
            terms = [
                event.service,
                event.version,
                "deploy",
                "deployed",
            ]
            golden_answer = event.engineer
        case QuestionTemplate.WHICH_DEPLOY_CAUSED:
            question = f"Which deploy caused incident {event.id} for {event.service}?"
            terms = [
                event.id,
                event.service,
                "incident",
                "caused",
            ]
            golden_answer = event.caused_by or ""
        case QuestionTemplate.WHO_RESOLVED:
            incident_name = event.incident or "unknown"
            question = f"Who resolved incident {incident_name} for {event.service}?"
            terms = [
                event.incident or "",
                event.service,
                "resolved",
                "resolution",
            ]
            golden_answer = event.engineer
        case QuestionTemplate.CURRENT_OWNER:
            question = f"Who is the current owner of {event.service}?"
            terms = [
                event.service,
                "owner",
                "ownership",
                "transferred",
            ]
            golden_answer = event.engineer
        case QuestionTemplate.PARAPHRASED_OWNER:
            question = "Which teammate took custody most recently?"
            terms = ["teammate", "custody", "recently"]
            golden_answer = event.engineer
        case QuestionTemplate.OUT_OF_WORLD:
            raise ValueError("out-of-world has no source event")

    return Question(
        qid=f"q-{number:03d}",
        template=template,
        question=question,
        terms=terms,
        answerable=True,
        golden_event_seqs=[event.seq],
        golden_capsule_ids=[],
        golden_answer=golden_answer,
    )
