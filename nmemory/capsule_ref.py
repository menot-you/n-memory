"""Human-readable, immutable capsule handles and their accepted reference grammar.

`cap-<n>` remains the canonical store identity. A handle adds the capsule's
birth kind and a short content slug without changing that identity, for
example `plan-42-index-project-context`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_SLUG_LEN: int = 48

HANDLE_PREFIXES: tuple[str, ...] = (
    "failure-pattern",
    "brainstorm",
    "constraint",
    "capability",
    "procedure",
    "decision",
    "fact",
    "task",
    "epic",
    "plan",
    "doc",
    "dod",
    "cap",
)

_DIACRITIC_MAP: dict[str, str] = {
    "à": "a",
    "á": "a",
    "â": "a",
    "ã": "a",
    "ä": "a",
    "å": "a",
    "ā": "a",
    "ă": "a",
    "ą": "a",
    "ç": "c",
    "ć": "c",
    "ĉ": "c",
    "ċ": "c",
    "č": "c",
    "è": "e",
    "é": "e",
    "ê": "e",
    "ë": "e",
    "ē": "e",
    "ĕ": "e",
    "ė": "e",
    "ę": "e",
    "ě": "e",
    "ì": "i",
    "í": "i",
    "î": "i",
    "ï": "i",
    "ĩ": "i",
    "ī": "i",
    "ĭ": "i",
    "į": "i",
    "ı": "i",
    "ñ": "n",
    "ń": "n",
    "ņ": "n",
    "ň": "n",
    "ò": "o",
    "ó": "o",
    "ô": "o",
    "õ": "o",
    "ö": "o",
    "ø": "o",
    "ō": "o",
    "ŏ": "o",
    "ő": "o",
    "ù": "u",
    "ú": "u",
    "û": "u",
    "ü": "u",
    "ũ": "u",
    "ū": "u",
    "ŭ": "u",
    "ů": "u",
    "ű": "u",
    "ų": "u",
    "ý": "y",
    "ÿ": "y",
    "ď": "d",
    "ĝ": "g",
    "ğ": "g",
    "ġ": "g",
    "ģ": "g",
    "ĥ": "h",
    "ĵ": "j",
    "ķ": "k",
    "ĺ": "l",
    "ļ": "l",
    "ľ": "l",
    "ł": "l",
    "ŕ": "r",
    "ŗ": "r",
    "ř": "r",
    "ś": "s",
    "ŝ": "s",
    "ş": "s",
    "š": "s",
    "ţ": "t",
    "ť": "t",
    "ŵ": "w",
    "ź": "z",
    "ż": "z",
    "ž": "z",
}


def fold_diacritic(c: str) -> str:
    """Fold one lowercase Latin character to the base form."""
    return _DIACRITIC_MAP.get(c, c)


class CapsuleRefSyntaxError(ValueError):
    """A malformed capsule reference."""

    def __init__(
        self, message: str = "expected cap-<n>, <kind>-<n>, or <kind>-<n>-<slug>"
    ) -> None:
        super().__init__(message)


def valid_slug(slug: str) -> bool:
    if (
        not slug
        or len(slug) > MAX_SLUG_LEN
        or slug.startswith("-")
        or slug.endswith("-")
    ):
        return False
    for part in slug.split("-"):
        if not part:
            return False
        for ch in part:
            if not ch.isascii() or not (ch.islower() or ch.isdigit()):
                return False
    return True


class CapsuleHandle:
    """The persisted immutable handle components for one canonical capsule id."""

    def __init__(self, sequence: int, prefix: str, slug: str | None = None) -> None:
        self._sequence = sequence
        self._prefix = prefix
        self._slug = slug

    @classmethod
    def new(
        cls, canonical_id: str, prefix: str, slug: str | None = None
    ) -> CapsuleHandle | None:
        """Build a validated handle from a canonical cap-<n> id and persisted components."""
        try:
            parsed = ParsedCapsuleRef.parse(canonical_id)
        except CapsuleRefSyntaxError:
            return None
        if not parsed.is_canonical() or prefix not in HANDLE_PREFIXES:
            return None
        if slug is not None and not valid_slug(slug):
            return None
        return cls(parsed.sequence, prefix, slug)

    @property
    def prefix(self) -> str:
        """The immutable birth-kind prefix (task, plan, cap, ...)."""
        return self._prefix

    @property
    def slug(self) -> str | None:
        """The immutable birth-content slug."""
        return self._slug

    @property
    def sequence(self) -> int:
        """The sequence number shared with the canonical id."""
        return self._sequence

    def __str__(self) -> str:
        if self._slug:
            return f"{self._prefix}-{self._sequence}-{self._slug}"
        return f"{self._prefix}-{self._sequence}"

    def __repr__(self) -> str:
        return f"CapsuleHandle(sequence={self._sequence}, prefix={self._prefix!r}, slug={self._slug!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CapsuleHandle):
            return False
        return (
            self._sequence == other._sequence
            and self._prefix == other._prefix
            and self._slug == other._slug
        )


class ParsedCapsuleRef:
    """One syntactically valid capsule reference supplied at a public boundary."""

    def __init__(self, prefix: str, sequence: int, slug: str | None = None) -> None:
        self._prefix = prefix
        self._sequence = sequence
        self._slug = slug

    @classmethod
    def parse(cls, input_str: str) -> ParsedCapsuleRef:
        """Parse canonical ids, typed short references, and exact full handles."""
        matched_prefix: str | None = None
        rest: str = ""
        for prefix in HANDLE_PREFIXES:
            pfx_marker = f"{prefix}-"
            if input_str.startswith(pfx_marker):
                matched_prefix = prefix
                rest = input_str[len(pfx_marker) :]
                break

        if matched_prefix is None:
            raise CapsuleRefSyntaxError()

        if "-" in rest:
            num_str, slug_str = rest.split("-", 1)
            slug = slug_str
        else:
            num_str = rest
            slug = None

        if (
            not num_str
            or not num_str.isdigit()
            or num_str.startswith("0")
            or (slug is not None and not valid_slug(slug))
        ):
            raise CapsuleRefSyntaxError()

        try:
            seq = int(num_str)
            if seq <= 0:
                raise CapsuleRefSyntaxError()
        except ValueError:
            raise CapsuleRefSyntaxError() from None

        return cls(matched_prefix, seq, slug)

    def canonical_id(self) -> str:
        """The canonical store id addressed by this reference."""
        return f"cap-{self._sequence}"

    def is_canonical(self) -> bool:
        """Whether the caller used the exact canonical cap-<n> form."""
        return self._prefix == "cap" and self._slug is None

    @property
    def prefix(self) -> str:
        """The caller-supplied prefix."""
        return self._prefix

    @property
    def sequence(self) -> int:
        """The caller-supplied sequence."""
        return self._sequence

    @property
    def slug(self) -> str | None:
        """The caller-supplied slug."""
        return self._slug

    def __repr__(self) -> str:
        return f"ParsedCapsuleRef(prefix={self._prefix!r}, sequence={self._sequence}, slug={self._slug!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ParsedCapsuleRef):
            return False
        return (
            self._prefix == other._prefix
            and self._sequence == other._sequence
            and self._slug == other._slug
        )


def prefix_for_kind(kind: Any) -> str | None:
    """Map a persisted classification kind to its human handle prefix."""
    if kind is None:
        return None
    k_str = kind.value if hasattr(kind, "value") else str(kind).lower()
    mapping = {
        "fact": "fact",
        "procedure": "procedure",
        "decision": "decision",
        "task": "task",
        "epic": "epic",
        "plan": "plan",
        "brainstorm": "brainstorm",
        "doc": "doc",
        "constraint": "constraint",
        "capability": "capability",
        "failure_pattern": "failure-pattern",
        "failure-pattern": "failure-pattern",
        "dod": "dod",
    }
    return mapping.get(k_str)


def slugify(content: str, max_chars: int = MAX_SLUG_LEN) -> str:
    """Generate the immutable ASCII slug from the first non-empty content line."""
    headline = ""
    for line in content.splitlines():
        trimmed = line.strip()
        if trimmed:
            headline = trimmed
            break

    slug_chars: list[str] = []
    separator = False
    for ch in headline.lower():
        folded = fold_diacritic(ch)
        if folded.isascii() and (folded.isalnum()):
            if separator and slug_chars and len(slug_chars) < max_chars:
                slug_chars.append("-")
            separator = False
            if len(slug_chars) == max_chars:
                break
            slug_chars.append(folded)
        elif slug_chars:
            separator = True

    while slug_chars and slug_chars[-1] == "-":
        slug_chars.pop()

    slug = "".join(slug_chars)
    return slug if slug else "memory"


def contains_exact_ref(content: str, reference: str) -> bool:
    """Whether reference occurs as a complete capsule-reference token."""
    pattern = rf"(?<![a-zA-Z0-9-]){re.escape(reference)}(?![a-zA-Z0-9-])"
    return bool(re.search(pattern, content))


@dataclass(frozen=True)
class CapsuleRefResolution:
    """The result of resolving a caller-supplied capsule reference against the store."""

    status: (
        str  # "resolved", "not_found", "invalid_ref", "kind_mismatch", "slug_mismatch"
    )
    id: str | None = None
    handle: str | None = None
    expected_handle: str | None = None
    detail: str | None = None


def resolve_capsule_ref(store: Any, reference: str) -> CapsuleRefResolution:
    """Resolve a capsule reference (canonical, typed-short, or full-handle) against store handles."""
    try:
        parsed = ParsedCapsuleRef.parse(reference)
    except CapsuleRefSyntaxError as exc:
        return CapsuleRefResolution(status="invalid_ref", detail=str(exc))

    cid = parsed.canonical_id()
    handle = store.handle_of(cid)
    if handle is None:
        return CapsuleRefResolution(status="not_found", id=cid)

    if parsed.is_canonical():
        return CapsuleRefResolution(status="resolved", id=cid, handle=handle)

    stored_ref = ParsedCapsuleRef.parse(handle)
    handle_prefix = stored_ref.prefix
    handle_slug = stored_ref.slug

    if parsed.prefix != handle_prefix:
        return CapsuleRefResolution(
            status="kind_mismatch", id=cid, expected_handle=handle
        )

    if parsed.slug is not None and parsed.slug != handle_slug:
        return CapsuleRefResolution(
            status="slug_mismatch", id=cid, expected_handle=handle
        )

    return CapsuleRefResolution(status="resolved", id=cid, handle=handle)


def compose_handle(capsule_id: str, prefix: str, slug: str | None) -> str:
    """The immutable handle derived from raw columns of `capsule_handles`."""
    seq = capsule_id.split("-", 1)[1] if "-" in capsule_id else capsule_id
    base = prefix if prefix.endswith(f"-{seq}") else f"{prefix}-{seq}"
    return f"{base}-{slug}" if slug else base
