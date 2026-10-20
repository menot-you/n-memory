"""Property-based invariants of nmemory that the parity judge does not observe.

The judge compares this port against the Rust oracle over a FIXED corpus. These
properties instead search the input space for a counterexample: what has to stay
true across generated space fingerprints, capsule references, and taint candidates.

Every strategy here is deliberately narrowed and each docstring names its
ceiling. A property no generated input can falsify is either true or untested,
and an unbounded `st.text()` usually proves neither.
"""

from __future__ import annotations

import string
from dataclasses import replace
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from nmemory.capsule_ref import (
    HANDLE_PREFIXES,
    MAX_SLUG_LEN,
    CapsuleHandle,
    ParsedCapsuleRef,
    resolve_capsule_ref,
)
from nmemory.space import DIMENSION, SpaceFingerprint
from nmemory.taint import (
    RULE_PROXIMITY_WINDOW,
    RULES,
    TaintRule,
    normalize,
    scan,
)

# --------------------------------------------------------------------------
# SpaceFingerprint — the embedding space is identified by ONE string
# --------------------------------------------------------------------------

# `:` is the field separator of `as_str`. A component carrying one makes the
# encoding ambiguous — put `:` back in this alphabet and the round-trip below
# finds the collision in half a second (measured). Both live components are
# module constants (`"openai"`, `DEFAULT_OPENAI_MODEL`), so the colon-free space
# IS the reachable one; if either ever becomes caller-supplied, widening this
# alphabet is the falsifier already waiting.
_space_component = st.text(
    alphabet=string.ascii_letters + string.digits + "-_./", min_size=1, max_size=24
)

_space_fingerprints = st.builds(
    SpaceFingerprint,
    provider=_space_component,
    model=_space_component,
    dimension=st.integers(min_value=1, max_value=1 << 16),
    prompt_version=_space_component,
)

_SPACE_FIELDS = ("provider", "model", "dimension", "prompt_version")


@given(space=_space_fingerprints)
def test_property_the_space_string_carries_the_whole_space_identity(
    space: SpaceFingerprint,
) -> None:
    """`as_str` round-trips all four fields, so it is injective.

    Injectivity is what the consumer actually needs: no parser for this string
    exists anywhere in the module, and `postgres.py` compares the persisted
    space to `expected.as_str()` by string equality to decide `full reindex
    required`. Two different spaces sharing one string means stale 3072-wide
    vectors get reused under a swapped model, silently. Asserting it separately
    would be dead weight — recovering the four fields already implies it.
    """
    assert space.as_str().split(":") == [
        space.provider,
        space.model,
        str(space.dimension),
        space.prompt_version,
    ]


@given(data=st.data())
def test_property_one_moved_component_invalidates_the_space(
    data: st.DataObject,
) -> None:
    """Exactly one component moved off `current()` stops being current.

    Generating a random fingerprint would exercise only the False branch, so
    the current space is asserted current first, then mutated one field at a
    time — dimension included, which is the one pgvector reads as a row width.
    """
    current = SpaceFingerprint.current()
    assert current.as_str() == SpaceFingerprint.current().as_str()

    field = data.draw(st.sampled_from(_SPACE_FIELDS))
    value: Any
    if field == "dimension":
        value = data.draw(
            st.integers(min_value=1, max_value=1 << 16).filter(lambda d: d != DIMENSION)
        )
    else:
        value = data.draw(
            _space_component.filter(lambda s: s != getattr(current, field))
        )

    moved = replace(current, **{field: value})
    assert moved.as_str() != current.as_str()
    assert moved.as_str() != SpaceFingerprint.current().as_str()


# --------------------------------------------------------------------------
# capsule_ref — a handle is human-readable, cap-<n> stays the identity
# --------------------------------------------------------------------------

_slug_part = st.text(
    alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=8
)
_slugs = (
    st.lists(_slug_part, min_size=1, max_size=5)
    .map("-".join)
    .filter(lambda s: len(s) <= MAX_SLUG_LEN)
)

# `failure-pattern` is the only prefix carrying a `-`; see the two properties
# below for why it is generated in one and excluded from the other.
_TOKEN_PREFIXES = tuple(p for p in HANDLE_PREFIXES if "-" not in p)


@st.composite
def _handles(draw: st.DrawFn, prefixes: tuple[str, ...] = HANDLE_PREFIXES) -> Any:
    sequence = draw(st.integers(min_value=1, max_value=10**12))
    handle = CapsuleHandle.new(
        f"cap-{sequence}",
        draw(st.sampled_from(prefixes)),
        draw(st.one_of(st.none(), _slugs)),
    )
    assert handle is not None
    return handle


class _FakeStore:
    """The one method `resolve_capsule_ref` reads off a store."""

    def __init__(self, handle: str | None) -> None:
        self._handle = handle

    def handle_of(self, capsule_id: str) -> str | None:
        return self._handle


@given(handle=_handles())
def test_property_a_handle_round_trips_through_the_reference_grammar(
    handle: CapsuleHandle,
) -> None:
    """`parse(str(handle))` recovers prefix, sequence, and slug exactly.

    The search is over PREFIX × SLUG together, because the two collide: a slug
    may carry `-`, which is also the sequence separator, so a `parse` that split
    from the right reads `task-5-foo-bar` as sequence `5-foo` and raises.

    `cap` being a proper prefix of `capability` is double-guarded — the marker
    is `f"{prefix}-"` AND the longer prefix is matched first — so removing
    either one alone keeps this green; both must go before `capability-5-x`
    parses as `cap` + `ability-5-x` (measured, not assumed).
    """
    parsed = ParsedCapsuleRef.parse(str(handle))
    assert (parsed.prefix, parsed.sequence, parsed.slug) == (
        handle.prefix,
        handle.sequence,
        handle.slug,
    )


@given(handle=_handles())
def test_property_canonical_id_is_idempotent(handle: CapsuleHandle) -> None:
    """Re-parsing a canonical id yields the same canonical id, and is canonical.

    `cap-<n>` is the store identity every reference collapses to; a second pass
    through the grammar must be a no-op, or a caller that normalises twice
    addresses a different row than one that normalises once.
    """
    once = ParsedCapsuleRef.parse(str(handle)).canonical_id()
    twice = ParsedCapsuleRef.parse(once)
    assert twice.canonical_id() == once == f"cap-{handle.sequence}"
    assert twice.is_canonical()


@given(reference=_handles(), stored=_handles(), missing=st.booleans())
def test_property_a_parsed_reference_never_resolves_to_another_capsule(
    reference: CapsuleHandle, stored: CapsuleHandle, missing: bool
) -> None:
    """A successful parse resolves to `cap-<its own n>` or does not resolve.

    Reference and stored handle are drawn INDEPENDENTLY, so most examples are a
    kind or slug disagreement: the search is for a `resolved` verdict handing
    back a capsule the caller did not name. `failure-pattern` is generated here
    on purpose — the verdict it gets today is a false rejection, which this
    property permits and the one below does not.
    """
    store = _FakeStore(None if missing else str(stored))
    res = resolve_capsule_ref(store, str(reference))
    expected_id = f"cap-{reference.sequence}"

    assert res.id in (None, expected_id)
    if missing:
        assert res.status == "not_found"
        return

    if res.status == "resolved":
        assert res.id == expected_id
        assert res.handle == str(stored)
        parsed = ParsedCapsuleRef.parse(str(reference))
        if not parsed.is_canonical():
            assert str(stored).startswith(f"{parsed.prefix}-")
            if parsed.slug is not None:
                assert str(stored).endswith(f"-{parsed.slug}")


@given(handle=_handles(prefixes=_TOKEN_PREFIXES))
def test_property_the_exact_stored_handle_always_resolves(
    handle: CapsuleHandle,
) -> None:
    """The store's own handle, handed back verbatim, resolves — as does cap-<n>.

    Without this direction the never-wrong-id property above is one-sided: a
    resolver answering `kind_mismatch` to everything satisfies it vacuously.

    `failure-pattern` is EXCLUDED, and that exclusion is a finding, not a taste:
    `resolve_capsule_ref` re-derives the stored prefix with `handle.split("-",2)`,
    which reads `failure-pattern-42-slug` as prefix `failure`, so no
    failure-pattern capsule is addressable by its own handle. The upstream
    implementation compares structured `handle.prefix()` instead and has no such hole.
    """
    store = _FakeStore(str(handle))

    res = resolve_capsule_ref(store, str(handle))
    assert res.status == "resolved"
    assert res.id == f"cap-{handle.sequence}"
    assert res.handle == str(handle)

    canonical = resolve_capsule_ref(store, f"cap-{handle.sequence}")
    assert canonical.status == "resolved"
    assert canonical.id == f"cap-{handle.sequence}"


# --------------------------------------------------------------------------
# taint — deterministic instruction-smuggling detection
# --------------------------------------------------------------------------

_SINGLE_GROUP_RULES = [(r, g) for r, g in RULES if len(g) == 1]
_MULTI_GROUP_RULES = [(r, g) for r, g in RULES if len(g) > 1]
_ALL_TERMS = sorted({t for _, groups in RULES for group in groups for t in group})
_ANCHOR_TERMS = sorted({t for _, groups in _SINGLE_GROUP_RULES for t in groups[0]})

# Latin-1 letters and every ASCII shape the normaliser special-cases (`[`, `:`,
# `]`, quotes, tabs, newlines). `İ` is absent: its `.lower()` expands to two
# code points, which no rule term can start or end on, so including it would
# assert about an unreachable branch instead of searching a real one.
_FILLER = st.text(
    alphabet=string.ascii_letters + string.digits + " \t\n\r[](){}:;,.\"'-_!?#áÇüÑ",
    max_size=40,
)


def _span_stable(terms: list[str]) -> list[str]:
    """Terms no sibling alternative prefixes.

    `_finding_for` picks the closest hit per group and ties break toward the
    term declared first, so planting `instructions` is measured as
    `instruction` — one byte shorter. That shifts the measured window by the
    suffix and would flip the verdict AT the boundary, which is a property of
    alternation, not of the window this test is aiming at.
    """
    return [t for t in terms if not any(o != t and t.startswith(o) for o in terms)]


# Whitespace the normaliser DROPS, because a `:` or `]` follows it. Planting one
# right after a term is what puts trailing bytes inside a reported span, and
# random filler almost never lands on it: 800 examples of plain filler produced
# zero (measured), which would have left the `rstrip` below untested.
_EDGE_TAIL = st.sampled_from([" :", "  ]", "\t:", "\n ]", " \t :"])


@st.composite
def _tainted(draw: st.DrawFn) -> tuple[str, TaintRule]:
    """Filler with rule terms planted in it, plus one guaranteed single-group hit."""
    anchor_rule, anchor_groups = draw(st.sampled_from(_SINGLE_GROUP_RULES))
    pieces: list[str] = [draw(_FILLER), draw(st.sampled_from(anchor_groups[0]))]
    if draw(st.booleans()):
        pieces.append(draw(_EDGE_TAIL))
    for _ in range(draw(st.integers(0, 4))):
        pieces.append(draw(_FILLER))
        pieces.append(draw(st.sampled_from(_ALL_TERMS)))
        if draw(st.booleans()):
            pieces.append(draw(_EDGE_TAIL))
    pieces.append(draw(_FILLER))
    return "".join(pieces), anchor_rule


@given(planted=_tainted())
def test_property_every_span_indexes_back_into_the_original_content(
    planted: tuple[str, TaintRule],
) -> None:
    """Each reported span slices the PRE-normalisation string back to its term.

    Findings are reported against `content` as the caller sent it, while matching
    runs on lowercased, whitespace-collapsed text — so the offset table is the
    only thing holding the two coordinate systems together. The comparison is
    `rstrip`ed on the right ONLY: when a term is followed by whitespace that the
    normaliser drops before a `:` or `]`, the span end lands one char past the
    term. Nothing is stripped on the left, where a drifting offset would show up.
    A single-group term is always planted so the loop can never pass empty.
    """
    content, anchor_rule = planted
    findings = scan(content)
    assert anchor_rule in {f.rule for f in findings}

    for finding in findings:
        for match in finding.matches:
            start, end = match.span
            assert 0 <= start <= end <= len(content)
            assert normalize(content[start:end]).text.rstrip() == match.term


@st.composite
def _windowed(draw: st.DrawFn) -> tuple[str, TaintRule]:
    rule, groups = draw(st.sampled_from(_MULTI_GROUP_RULES))
    terms = [draw(st.sampled_from(group)) for group in groups[:-1]]
    terms.append(draw(st.sampled_from(_span_stable(groups[-1]))))

    fixed = sum(len(t) for t in terms)
    total = draw(
        st.one_of(
            st.integers(RULE_PROXIMITY_WINDOW - 3, RULE_PROXIMITY_WINDOW + 3),
            st.integers(fixed, RULE_PROXIMITY_WINDOW + 200),
        )
    )

    gaps = [0] * (len(terms) - 1)
    remaining = max(total - fixed, 0)
    for i in range(len(gaps) - 1):
        gaps[i] = draw(st.integers(0, remaining))
        remaining -= gaps[i]
    gaps[-1] = remaining

    content = terms[0] + "".join("x" * g + t for g, t in zip(gaps, terms[1:]))
    return content, rule


@given(planted=_windowed())
def test_property_the_proximity_window_is_the_falsifiable_edge(
    planted: tuple[str, TaintRule],
) -> None:
    """A multi-group rule fires exactly while its terms span <= 220 chars.

    Generation targets the boundary directly (a band around 220, plus a wide
    band), so both verdicts are searched instead of only the easy one. Filler is
    `x` — never whitespace — because the window is measured in NORMALISED
    coordinates and a whitespace run collapses to one char, which would test the
    normaliser instead of the window. The identity assertion pins that.

    The window itself is pinned by value: the layout is built from the imported
    constant, so widening it alone would move this test's own expectation with
    it and prove nothing.
    """
    content, rule = planted
    assert RULE_PROXIMITY_WINDOW == 220
    assert normalize(content).text == content

    fired = {f.rule for f in scan(content)}
    assert (rule in fired) == (len(content) <= RULE_PROXIMITY_WINDOW)
