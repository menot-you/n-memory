"""L2 ``rank_providers`` — pure ordering over an already-eligible pool."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from nspawn.owner_roster import UNCLASSIFIED, Provider
from nspawn.ranks import (
    DESIGN,
    DETERMINISTIC,
    IMPLEMENTATION,
    JUDGMENT,
    MECHANICAL_FANOUT,
    PLANNING,
    REVIEW,
    VERIFICATION,
    rank_providers,
)

_STRENGTH_CLASSES = (JUDGMENT, PLANNING, REVIEW, IMPLEMENTATION)


def _provider(
    name: str,
    *,
    strength: int | None = None,
    thrift: int | None = None,
    design: int | None = None,
) -> Provider:
    return Provider(name=name, strength=strength, thrift=thrift, design=design)


def test_empty_pool_ranks_empty() -> None:
    assert rank_providers([], JUDGMENT) == []


def test_judgment_ranks_by_descending_strength() -> None:
    low = _provider("low", strength=1)
    high = _provider("high", strength=9)
    mid = _provider("mid", strength=5)
    assert rank_providers([low, high, mid], JUDGMENT) == [high, mid, low]


def test_planning_and_review_share_the_strength_axis() -> None:
    low = _provider("low", strength=1)
    high = _provider("high", strength=9)
    assert rank_providers([low, high], PLANNING) == [high, low]
    assert rank_providers([low, high], REVIEW) == [high, low]


def test_implementation_ranks_by_descending_strength() -> None:
    # Named so that the name-stable fallback would put the weaker route first.
    weak = _provider("a", strength=1)
    strong = _provider("b", strength=9)
    assert rank_providers([weak, strong], IMPLEMENTATION) == [strong, weak]


def test_mechanical_fanout_ranks_by_descending_thrift() -> None:
    low = _provider("low", thrift=1)
    high = _provider("high", thrift=9)
    assert rank_providers([low, high], MECHANICAL_FANOUT) == [high, low]


def test_design_ranks_by_descending_design() -> None:
    low = _provider("low", design=1)
    high = _provider("high", design=9)
    assert rank_providers([low, high], DESIGN) == [high, low]


def test_verification_excludes_the_producer_by_name() -> None:
    producer = _provider("producer", strength=9)
    other = _provider("other", strength=1)
    result = rank_providers([producer, other], VERIFICATION, producer="producer")
    assert result == [other]


def test_verification_with_no_producer_match_keeps_the_whole_pool() -> None:
    a = _provider("a", strength=1)
    b = _provider("b", strength=9)
    result = rank_providers([a, b], VERIFICATION, producer="nobody")
    assert result == [b, a]


def test_verification_without_a_producer_argument_ranks_everyone() -> None:
    a = _provider("a", strength=1)
    b = _provider("b", strength=9)
    assert rank_providers([a, b], VERIFICATION) == [b, a]


def test_undeclared_axis_sorts_strictly_after_every_declared_value() -> None:
    declared_zero = _provider("declared-zero", strength=0)
    undeclared = _provider("undeclared", strength=None)
    assert rank_providers([undeclared, declared_zero], JUDGMENT) == [
        declared_zero,
        undeclared,
    ]


def test_ties_on_the_axis_break_by_name() -> None:
    b = _provider("b", strength=5)
    a = _provider("a", strength=5)
    assert rank_providers([b, a], JUDGMENT) == [a, b]


def test_ties_among_undeclared_break_by_name() -> None:
    b = _provider("b")
    a = _provider("a")
    assert rank_providers([b, a], JUDGMENT) == [a, b]


def test_deterministic_class_is_name_stable() -> None:
    b = _provider("b", strength=1)
    a = _provider("a", strength=9)
    assert rank_providers([b, a], DETERMINISTIC) == [a, b]


def test_none_class_is_name_stable() -> None:
    b = _provider("b", strength=1)
    a = _provider("a", strength=9)
    assert rank_providers([b, a], None) == [a, b]


def test_unrecognized_class_string_is_name_stable() -> None:
    b = _provider("b", strength=1)
    a = _provider("a", strength=9)
    assert rank_providers([b, a], "not-a-real-class") == [a, b]


def test_input_sequence_is_not_mutated() -> None:
    a = _provider("a", strength=1)
    b = _provider("b", strength=9)
    pool = [a, b]
    rank_providers(pool, JUDGMENT)
    assert pool == [a, b]


_names = st.lists(
    st.text(alphabet="abcdefghij", min_size=1, max_size=4), min_size=0, max_size=6
)


@st.composite
def _provider_pool(draw: st.DrawFn) -> list[Provider]:
    names = draw(_names.map(lambda values: sorted(set(values))))
    axis = draw(st.sampled_from(["strength", "thrift", "design"]))
    values = draw(
        st.lists(
            st.one_of(st.none(), st.integers(min_value=0, max_value=255)),
            min_size=len(names),
            max_size=len(names),
        )
    )
    pool: list[Provider] = []
    for name, value in zip(names, values, strict=True):
        pool.append(
            Provider(
                name=name,
                strength=value if axis == "strength" else None,
                thrift=value if axis == "thrift" else None,
                design=value if axis == "design" else None,
            )
        )
    return pool


@given(_provider_pool(), st.sampled_from(_STRENGTH_CLASSES))
def test_property_output_is_a_permutation_of_the_input(
    pool: list[Provider], execution_class: str
) -> None:
    ranked = rank_providers(pool, execution_class)
    assert sorted(p.name for p in ranked) == sorted(p.name for p in pool)


@given(_provider_pool(), st.sampled_from(_STRENGTH_CLASSES))
def test_property_declared_strength_always_precedes_undeclared(
    pool: list[Provider], execution_class: str
) -> None:
    ranked = rank_providers(pool, execution_class)
    declared_flags = [p.strength is not None for p in ranked]
    # Once an undeclared entry (False) appears, every later entry stays False.
    assert declared_flags == sorted(declared_flags, reverse=True)


@given(_provider_pool(), st.sampled_from(_STRENGTH_CLASSES))
def test_property_declared_strength_values_are_non_increasing(
    pool: list[Provider], execution_class: str
) -> None:
    ranked = rank_providers(pool, execution_class)
    declared_values = [p.strength for p in ranked if p.strength is not None]
    assert declared_values == sorted(declared_values, reverse=True)


@given(_provider_pool())
def test_property_verification_never_returns_the_named_producer(
    pool: list[Provider],
) -> None:
    for provider in pool:
        ranked = rank_providers(pool, VERIFICATION, producer=provider.name)
        assert all(candidate.name != provider.name for candidate in ranked)


@given(_provider_pool())
def test_property_fallback_classes_are_name_sorted(pool: list[Provider]) -> None:
    ranked = rank_providers(pool, DETERMINISTIC)
    assert [p.name for p in ranked] == sorted(p.name for p in pool)


def test_the_unclassified_floor_ranks_by_thrift_not_by_name() -> None:
    """The cheapest class must pick the cheapest route, not the first letter.

    `unclassified` is the floor a dispatch falls to when the System1 cannot say
    what the work is, and the owner's rule for that fall is "fail closed at
    the lowest ceiling". Ranking it name-stable made the fall arbitrary: the
    alphabetically first provider is not the thriftiest, so a dispatch could
    fall onto a DEARER route than the class it declared — which is the one
    thing the fall exists to prevent. Ordering the floor by descending
    `thrift`, the same column `mechanical_fanout` uses, makes the fall a
    descent by construction.

    Found by two independent reviews, 2026-09-21.
    """
    cheap = _provider("zzz-cheap", strength=9, thrift=9)
    dear = _provider("aaa-dear", strength=9, thrift=1)
    assert rank_providers([dear, cheap], UNCLASSIFIED) == [cheap, dear]
