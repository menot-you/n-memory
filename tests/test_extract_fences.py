"""Code blocks stay opaque until their matching fence closes."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nmemory.extract import extract


@pytest.mark.parametrize(
    ("opening", "interior", "closing"),
    [
        ("```python", "~~~", "```"),
        ("~~~~text", "```", "~~~~"),
        ("````python", "```", "````"),
        ("~~~~text", "~~~", "~~~~"),
    ],
)
def test_other_fences_do_not_expose_code(
    opening: str, interior: str, closing: str
) -> None:
    assert extract(f"{opening}\n{interior}\nTODO: hidden code\n{closing}") == []


@settings(max_examples=100)
@given(
    st.sampled_from(["`", "~"]),
    st.integers(min_value=3, max_value=12),
    st.integers(min_value=0, max_value=6),
    st.sampled_from(["", "python", "text", "json", " language metadata"]),
)
def test_matching_or_longer_closer_restores_prose(
    marker: str, length: int, extra: int, info: str
) -> None:
    result = extract(
        f"{marker * length}{info}\nTODO: hidden code\n{marker * (length + extra)}\nTODO: visible prose"
    )
    assert [item.content for item in result] == ["TODO: visible prose"]


def test_fence_with_trailing_content_does_not_close_code() -> None:
    assert extract("```\n```not-a-closer\nTODO: hidden code\n```") == []
