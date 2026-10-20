"""Boundary examples protecting prose, decoration and concrete entity identity."""

from dataclasses import FrozenInstanceError

import pytest

from nmemory.extract import (
    entity_in,
    extract,
    is_indented_code,
    opener_prefix,
    split_sentences,
    strip_bracket_stamp,
    strip_markers,
    strip_ordered_marker,
    strip_speaker_tag,
    word_pair_cue,
)


@pytest.mark.parametrize(
    "prefix", ["- ", "* ", "+ ", "> ", "# ", "[ ] ", "[x] ", "[X] ", "1. "]
)
def test_indented_markdown_remains_prose(prefix: str) -> None:
    result = extract("    " + prefix + "TODO: preserve receipts")
    assert [item.content for item in result] == ["TODO: preserve receipts"]


def test_indentation_boundary_and_non_ascii_whitespace() -> None:
    assert not is_indented_code("   ordinary")
    assert is_indented_code("    ordinary")
    assert is_indented_code("\tordinary")
    assert not is_indented_code("\u2003" * 4 + "ordinary")
    assert not is_indented_code("XXXXordinary")
    assert [item.content for item in extract("    code\nTODO: visible prose")] == [
        "TODO: visible prose"
    ]


@pytest.mark.parametrize(
    "prefix",
    [
        "#   ",
        ">   ",
        "-   ",
        "*   ",
        "+   ",
        "[x]   ",
        "[X]   ",
        "[ ]   ",
        "1.   ",
        "[12]   alice:   ",
    ],
)
def test_decoration_whitespace_is_not_content(prefix: str) -> None:
    assert (
        strip_markers(prefix + "TODO: preserve receipts")[0]
        == "TODO: preserve receipts"
    )


def test_unrecognized_marker_bytes_are_preserved() -> None:
    assert strip_markers("XX# TODO: preserve receipts") == (
        "XX# TODO: preserve receipts",
        False,
    )
    assert strip_markers("XX> TODO: preserve receipts") == (
        "XX> TODO: preserve receipts",
        False,
    )
    assert strip_markers("alice: TODO: preserve receipts") == (
        "alice: TODO: preserve receipts",
        False,
    )
    assert strip_markers("[12] alice: bob: TODO: preserve receipts") == (
        "bob: TODO: preserve receipts",
        False,
    )


def test_stamp_boundaries_and_first_closing_bracket() -> None:
    assert strip_bracket_stamp("[" + "1" * 31 + "] body") == "body"
    assert strip_bracket_stamp("[" + "1" * 32 + "] body") is None
    assert strip_bracket_stamp("[1] body] tail") == "body] tail"
    assert strip_bracket_stamp("[1]  body") == "body"
    assert strip_bracket_stamp("[1[2] body") is None
    assert strip_bracket_stamp("[1 text") is None
    assert strip_bracket_stamp("[1a] body") == "body"


def test_speaker_boundaries() -> None:
    assert strip_speaker_tag("a" * 24 + ": body") == "body"
    assert strip_speaker_tag("a" * 25 + ": body") is None
    assert strip_speaker_tag("alice:   body") == "body"
    assert strip_speaker_tag("a: body") == "body"
    assert strip_speaker_tag("alice body") is None
    assert strip_speaker_tag("alice body ") is None
    for label in [
        "todo",
        "epic",
        "plan",
        "idea",
        "doc",
        "procedure",
        "constraint",
        "capability",
        "failure",
        "decided",
        "install",
        "step",
    ]:
        assert strip_speaker_tag(label + ": body") is None


def test_ordered_marker_empty_body_and_nondigit_prefix() -> None:
    assert strip_ordered_marker("1. ") == ""
    assert strip_ordered_marker("a1. body") == "a1. body"
    assert strip_ordered_marker("1.   body") == "body"


def test_punctuation_and_multisentence_boundaries() -> None:
    assert split_sentences("; alpha; beta") == [";", "alpha;", "beta"]
    assert split_sentences(";") == [";"]
    assert split_sentences("Alpha. Beta! Gamma? Delta") == [
        "Alpha.",
        "Beta!",
        "Gamma?",
        "Delta",
    ]
    assert split_sentences("alpha Beta") == ["alpha Beta"]
    assert split_sentences(". Alpha") == [". Alpha"]
    assert split_sentences("Alpha!") == ["Alpha!"]
    assert split_sentences("Alpha?") == ["Alpha?"]


def test_cue_boundaries_and_exact_goal_identity() -> None:
    assert extract("TODO: action")[0].content == "TODO: action"
    assert extract("TODO: action?") == []
    assert extract("[ ] choose action?")[0].cue == "todo-checkbox:'[ ]'"
    assert extract("Step 1")[0].cue == "step-prefix:'step 1'"
    for opener in ["to", "para"]:
        assert (
            extract(f"{opener} install: execute command")[0].cue
            == f"goal-colon:'{opener} …:'"
        )
    assert extract("ordinary: neutral prose") == []
    for separator in [",", "?"]:
        assert opener_prefix("could we" + separator, ["could we"]) == "could we"
    assert word_pair_cue("must not", [("must", "not")]) == "must not"
    assert word_pair_cue("must later", [("must", "not")]) is None
    assert word_pair_cue("may not", [("must", "not")]) is None


@pytest.mark.parametrize(
    "entity", ["/x", r"folder\file", "ns::item", "v2", "file.txt", "aB", "XML"]
)
def test_concrete_entity_identity(entity: str) -> None:
    assert entity_in(entity) == entity
    assert entity_in(f"({entity})") == entity
    assert entity_in("a foo(bar) " + entity) == entity


@pytest.mark.parametrize("text", ["``", "`foo", "foo`", "a", "Ordinary", "foo(bar)"])
def test_nonentities_are_not_promoted(text: str) -> None:
    assert entity_in(text) is None


def test_single_character_code_entity_and_case() -> None:
    assert entity_in("`a`") == "`a`"
    assert entity_in("XMP") == "XMP"
    assert entity_in("xM") == "xM"


def test_candidates_are_immutable() -> None:
    candidate = extract("TODO: action")[0]
    with pytest.raises(FrozenInstanceError):
        candidate.content = "different source"  # type: ignore[misc]


def test_abbreviations_do_not_hide_later_sentence_boundaries() -> None:
    assert split_sentences("A. Smith. Other") == ["A. Smith.", "Other"]
    assert split_sentences("Alpha. lower. Next") == ["Alpha. lower.", "Next"]
    assert split_sentences(". Alpha. Beta") == [". Alpha.", "Beta"]
    assert split_sentences("Go. Next") == ["Go.", "Next"]


def test_kind_refusal_identifies_exact_rejected_value() -> None:
    from nmemory.extract import CandidateKind

    with pytest.raises(ValueError) as caught:
        CandidateKind.from_str("unsupported")
    assert str(caught.value) == "unknown candidate kind: unsupported"


@pytest.mark.parametrize(
    "wire",
    [
        "fact",
        "procedure",
        "decision",
        "task",
        "epic",
        "plan",
        "brainstorm",
        "doc",
        "constraint",
        "capability",
        "failure_pattern",
        "dod",
    ],
)
def test_all_kind_wire_values_round_trip(wire: str) -> None:
    from nmemory.extract import CandidateKind, ExtractCandidate

    kind = CandidateKind.from_str(wire)
    assert kind.value == wire
    assert ExtractCandidate("source text", kind, "source cue").to_dict() == {
        "content": "source text",
        "kind": wire,
        "cue": "source cue",
    }


@pytest.mark.parametrize("text", ["1.", "1)", "12.", "12)", "1", "12"])
def test_incomplete_ordered_markers_remain_literal(text: str) -> None:
    assert strip_ordered_marker(text) == text
    assert extract(text) == []
