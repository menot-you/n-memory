"""Deterministic classification (w2-kinds, u-r11, q6).

Pure derivation of kind, scope, authority_class, and instruction_taint.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from . import taint
from .capsule import AuthorityClass
from .extract import CandidateKind, extract
from .project import DEFAULT_PROJECT_ID

__all__ = [
    "CandidateKind",
    "Classification",
    "ClassificationContext",
    "ClassificationScope",
    "ClassifyError",
    "ContentOrigin",
    "KindUnderivable",
    "TaintedButObservedFact",
    "classify",
]

LONGFORM_DOC_MIN_CHARS = 400


class ClassificationScope(StrEnum):
    PROJECT = "project"
    GLOBAL = "global"
    SESSION = "session"

    @classmethod
    def from_str(cls, value: str) -> ClassificationScope:
        try:
            return cls(value.strip().lower())
        except ValueError:
            raise ValueError(f"unknown classification scope: {value}") from None


class ContentOrigin(StrEnum):
    EXTRACTED_CANDIDATE = "extracted-candidate"
    OWNER_STATED = "owner-stated"
    TOOL_OBSERVATION = "tool-observation"
    EXTERNAL_IMPORT = "external-import"
    SESSION_NOTE = "session-note"

    @classmethod
    def from_str(cls, value: str) -> ContentOrigin:
        try:
            return cls(value.strip().lower().replace("_", "-"))
        except ValueError:
            raise ValueError(f"unknown content origin: {value}") from None

    def default_scope(self) -> ClassificationScope:
        if self == ContentOrigin.SESSION_NOTE:
            return ClassificationScope.SESSION
        return ClassificationScope.PROJECT

    def authority_class(self) -> AuthorityClass:
        match self:
            case ContentOrigin.EXTRACTED_CANDIDATE:
                return AuthorityClass.AGENT_INFERRED
            case ContentOrigin.OWNER_STATED:
                return AuthorityClass.USER_STATED
            case ContentOrigin.TOOL_OBSERVATION:
                return AuthorityClass.OBSERVED_FACT
            case ContentOrigin.EXTERNAL_IMPORT:
                return AuthorityClass.EXTERNALLY_IMPORTED
            case ContentOrigin.SESSION_NOTE:
                return AuthorityClass.AGENT_INFERRED

    def born_tainted(self) -> bool:
        return self == ContentOrigin.EXTERNAL_IMPORT


class ClassifyError(Exception):
    pass


class KindUnderivable(ClassifyError):
    def __init__(self, content: str):
        super().__init__(f"could not derive candidate kind from content: {content!r}")
        self.content = content


class TaintedButObservedFact(ClassifyError):
    def __init__(self) -> None:
        super().__init__(
            "classification invariant violated: content is instruction-tainted but "
            "origin is tool-observation (observed-fact cannot carry taint)"
        )


@dataclass(frozen=True)
class ClassificationContext:
    project_id: str | None = None
    scope_override: ClassificationScope | None = None
    kind_override: CandidateKind | None = None
    taint_hint: bool = False


@dataclass(frozen=True)
class Classification:
    kind: CandidateKind
    scope: ClassificationScope
    project_id: str
    authority_class: AuthorityClass
    instruction_taint: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "scope": self.scope.value,
            "project_id": self.project_id,
            "authority_class": self.authority_class.value,
            "instruction_taint": self.instruction_taint,
        }


def classify(
    content: str,
    origin: ContentOrigin | str,
    context: ClassificationContext | None = None,
) -> Classification:
    """Classify a piece of content deterministically."""
    if isinstance(origin, str):
        origin = ContentOrigin.from_str(origin)
    if context is None:
        context = ClassificationContext()

    # Kind derivation
    if context.kind_override is not None:
        kind = context.kind_override
    else:
        candidates = extract(content)
        if candidates:
            kind = candidates[0].kind
        else:
            compact_len = len("".join(content.split()))
            if compact_len >= LONGFORM_DOC_MIN_CHARS:
                kind = CandidateKind.DOC
            else:
                raise KindUnderivable(content)

    scope = (
        context.scope_override
        if context.scope_override is not None
        else origin.default_scope()
    )
    project_id = (
        context.project_id
        if context.project_id and context.project_id.strip()
        else DEFAULT_PROJECT_ID
    )
    auth = origin.authority_class()
    instruction_taint = (
        origin.born_tainted() or context.taint_hint or taint.is_suspicious(content)
    )

    if instruction_taint and auth == AuthorityClass.OBSERVED_FACT:
        raise TaintedButObservedFact()

    return Classification(
        kind=kind,
        scope=scope,
        project_id=project_id,
        authority_class=auth,
        instruction_taint=instruction_taint,
    )
