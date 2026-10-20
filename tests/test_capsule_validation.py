"""Canonical capsule inputs cannot coerce malformed trust metadata into facts."""

from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    CapsuleError,
    Freshness,
    InvalidConfidence,
    Provenance,
    Scope,
)


def fields():
    return {
        "content": "observed content",
        "provenance": Provenance("fixture", "test", "digest"),
        "confidence": 0.5,
        "freshness": Freshness(datetime(2026, 9, 19, tzinfo=UTC)),
        "scope": Scope("test"),
        "authority_class": AuthorityClass.OBSERVED_FACT,
        "instruction_taint": False,
    }


@settings(max_examples=100)
@given(
    value=st.one_of(
        st.none(),
        st.integers(),
        st.text(),
        st.lists(st.integers(), max_size=3),
        st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
    )
)
def test_nonboolean_taint_never_becomes_clean_canonical_content(value):
    document = Capsule(**fields()).to_dict()
    document["instruction_taint"] = value
    with pytest.raises(CapsuleError, match="instruction_taint"):
        Capsule.from_dict(document)


@pytest.mark.parametrize("value", [False, True])
def test_boolean_confidence_is_not_a_numeric_probability(value):
    args = fields()
    args["confidence"] = value
    with pytest.raises(CapsuleError, match="confidence"):
        Capsule(**args)


@settings(max_examples=100)
@given(confidence=st.floats(min_value=0.0, max_value=1.0), taint=st.booleans())
def test_valid_probability_and_taint_survive_canonical_round_trip(confidence, taint):
    args = fields()
    args.update(confidence=confidence, instruction_taint=taint)
    value = Capsule(**args)
    round_trip = Capsule.from_canonical_json(value.to_canonical_json())
    assert round_trip.confidence == confidence
    assert round_trip.instruction_taint is taint


@pytest.mark.parametrize(
    "confidence", [float("nan"), float("inf"), -float("inf"), -0.1, 1.1]
)
def test_nonfinite_and_out_of_range_confidence_is_refused(confidence):
    args = fields()
    args["confidence"] = confidence
    with pytest.raises(InvalidConfidence):
        Capsule(**args)


@settings(max_examples=100)
@given(
    value=st.one_of(
        st.none(), st.booleans(), st.integers(), st.lists(st.text(), max_size=2)
    )
)
def test_nontext_authority_cannot_construct_an_unrepresentable_capsule(value):
    document = Capsule(**fields()).to_dict()
    document["authority_class"] = value
    with pytest.raises(CapsuleError, match="authority_class"):
        Capsule.from_dict(document)
