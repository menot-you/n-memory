"""The review brief grammar. The registry counts each distinct head without a limit."""

from __future__ import annotations

import re

HEX = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
CANDIDATE = re.compile(r"Candidate: ([^\s@]+)@((?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64}))")
SPEC = re.compile(r"Spec revision: ([0-9a-fA-F]{64})")


class InvalidCandidate(ValueError):
    """The supplied review coordinates or brief are invalid."""


def coordinates(subject: str, candidate: str) -> tuple[str, str]:
    if (
        not isinstance(subject, str)
        or not subject
        or len(subject) > 1024
        or re.search(r"[\s@]", subject)
    ):
        raise InvalidCandidate(
            "Name one artifact with a nonblank subject without spaces or @."
        )
    if subject in ("main", "master", "origin/main", "origin/master"):
        raise InvalidCandidate(
            f"{subject} is a base branch, not an artifact. Name the artifact's repository-qualified lineage."
        )
    if not isinstance(candidate, str) or HEX.fullmatch(candidate) is None:
        raise InvalidCandidate(
            f"Subject {subject} needs a candidate with 40 or 64 hex characters."
        )
    return subject, candidate.lower()


def spec_revision(prompt: str) -> str | None:
    lines = [line for line in prompt.splitlines() if line.startswith("Spec revision:")]
    if len(lines) > 1:
        raise InvalidCandidate(
            "Use at most one Spec revision: line. Indent a quoted line."
        )
    if not lines:
        return None
    match = SPEC.fullmatch(lines[0])
    if match is None:
        raise InvalidCandidate("Use Spec revision: <64-hex sha256>.")
    return match[1].lower()


def brief(text: str) -> tuple[str, str, str | None]:
    lines = [line for line in text.splitlines() if line.startswith("Candidate:")]
    if len(lines) != 1 or (match := CANDIDATE.fullmatch(lines[0])) is None:
        raise InvalidCandidate(
            "Use exactly one Candidate: <subject>@<40 or 64 hex> line. Indent a quoted line."
        )
    subject, candidate = coordinates(match[1], match[2])
    return subject, candidate, spec_revision(text)
