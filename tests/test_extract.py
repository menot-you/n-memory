"""Independent examples for extraction precedence and source preservation."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nmemory.extract import CandidateKind, extract


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("TODO: ship the release", CandidateKind.TASK),
        ("[ ] choose a database?", CandidateKind.TASK),
        ("Epic: migrate all services", CandidateKind.EPIC),
        ("Plan: migrate in phases", CandidateKind.PLAN),
        ("Idea: cache the responses", CandidateKind.BRAINSTORM),
        ("Could we cache responses?", CandidateKind.BRAINSTORM),
        ("Docs: database migration guide", CandidateKind.DOC),
        ("Procedure: rotate the credentials", CandidateKind.PROCEDURE),
        ("Constraint: preserve all receipts", CandidateKind.CONSTRAINT),
        ("Capability: browse local files", CandidateKind.CAPABILITY),
        ("Use when investigating latency", CandidateKind.CAPABILITY),
        ("Failure: missing provider credentials", CandidateKind.FAILURE_PATTERN),
        ("We decided to retain receipts", CandidateKind.DECISION),
        ("We will use PostgreSQL", CandidateKind.DECISION),
        ("Agents must not publish secrets", CandidateKind.CONSTRAINT),
        ("The command fails with invalid credentials", CandidateKind.FAILURE_PATTERN),
        ("Step 1: rotate credentials", CandidateKind.PROCEDURE),
        ("To deploy: run the migration", CandidateKind.PROCEDURE),
        ("PostgreSQL18 is installed locally", CandidateKind.FACT),
    ],
)
def test_explicit_examples(text: str, kind: CandidateKind) -> None:
    result = extract(text)
    assert len(result) == 1
    assert result[0].kind is kind
    assert result[0].content in text
    assert result[0].to_dict()["kind"] == kind.value


@pytest.mark.parametrize(
    "text",
    [
        "",
        "one",
        "ordinary neutral prose",
        "Is this complete?",
        "    TODO: code example",
        "\tTODO: code example",
    ],
)
def test_non_candidates(text: str) -> None:
    assert extract(text) == []


@pytest.mark.parametrize("fence", ["```", "~~~"])
@settings(max_examples=100)
@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=1, max_size=60))
def test_code_fences_do_not_create_candidates(fence: str, body: str) -> None:
    assert extract(f"{fence}\nTODO: {body}\n{fence}") == []


@settings(max_examples=100)
@given(
    st.lists(
        st.sampled_from(["alpha", "beta", "gamma", "delta"]), min_size=1, max_size=15
    )
)
def test_markdown_decoration_preserves_exact_candidate(words: list[str]) -> None:
    body = "TODO: " + " ".join(words)
    expected = extract(body)
    for prefix in ["- ", "* ", "+ ", "> ", "## ", "1. ", "2) ", "[2026-09-20] alice: "]:
        assert extract(prefix + body) == expected


def test_precedence_and_sentence_order() -> None:
    result = extract("TODO: document what we decided; Plan: migrate the database")
    assert [(item.content, item.kind) for item in result] == [
        ("TODO: document what we decided", CandidateKind.TASK),
        ("Plan: migrate the database", CandidateKind.PLAN),
    ]


def test_step_cue_uses_step_number_not_numbers_in_instruction() -> None:
    result = extract("Step 1: install Python 3 and PostgreSQL 18")
    assert len(result) == 1
    assert result[0].cue == "step-prefix:'step 1'"


@pytest.mark.parametrize("raw", [" FACT ", "pRoCeDuRe", "dod"])
def test_kind_normalization(raw: str) -> None:
    assert CandidateKind.from_str(raw).value == raw.strip().lower()


def test_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown candidate kind"):
        CandidateKind.from_str("unknown")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("; ", [";"]),
        ("Alpha. Beta!", ["Alpha.", "Beta!"]),
        ("Alpha. lower", ["Alpha. lower"]),
        ("Alpha. ", ["Alpha."]),
        ("U.S. Navy", ["U.S. Navy"]),
        ("A. Smith", ["A. Smith"]),
    ],
)
def test_sentence_boundaries(text: str, expected: list[str]) -> None:
    from nmemory.extract import split_sentences

    assert split_sentences(text) == expected


@pytest.mark.parametrize(
    "stamp",
    ["[missing", "[" + "2" * 33 + "] text", "[[2]] text", "[abc] text", "[2]text"],
)
def test_malformed_stamp_is_preserved(stamp: str) -> None:
    from nmemory.extract import strip_markers

    assert strip_markers(stamp) == (stamp, False)


@pytest.mark.parametrize(
    "tag",
    [
        "withoutcolon",
        ": text",
        "a" * 25 + ": text",
        "a b: text",
        "TODO: text",
        "alice:text",
    ],
)
def test_non_speaker_labels_are_preserved(tag: str) -> None:
    from nmemory.extract import strip_markers

    assert strip_markers("[12] " + tag) == (tag, False)


def test_whitespace_and_nonmarkers_are_preserved() -> None:
    from nmemory.extract import is_indented_code, strip_ordered_marker

    assert not is_indented_code("    ")
    for text in ["12", "12.foo", "12x value"]:
        assert strip_ordered_marker(text) == text


@pytest.mark.parametrize("separator", [": ", " : ", " — ", " - "])
def test_label_separators(separator: str) -> None:
    result = extract("Plan" + separator + "migrate services")
    assert result[0].kind is CandidateKind.PLAN


def test_label_and_opener_word_boundaries() -> None:
    from nmemory.extract import labeled, opener_prefix

    assert labeled("plan extra words", ["plan"]) is None
    assert labeled("planet: extra words", ["plan"]) is None
    assert opener_prefix("use whenever needed", ["use when"]) is None
    assert opener_prefix("use when", ["use when"]) == "use when"


@pytest.mark.parametrize(
    "text",
    [
        "Install the package",
        "Para instalar: execute comando",
        "The service is ordinary",
        "We always retain receipts",
        "`db` is available",
        "A x foo(bar) neutral is ordinary",
    ],
)
def test_additional_candidate_paths(text: str) -> None:
    result = extract(text)
    assert all(item.content in text for item in result)
    if text in ("The service is ordinary", "A x foo(bar) neutral is ordinary"):
        assert result == []
    else:
        assert len(result) == 1


def test_step_without_number_is_not_a_procedure() -> None:
    assert extract("Step toward the garden") == []


def test_portuguese_task_label() -> None:
    assert extract("A fazer: publicar pacote")[0].kind is CandidateKind.TASK


@settings(max_examples=500)
@given(st.text(max_size=500))
def test_arbitrary_source_never_fabricates_candidate_bytes(text: str) -> None:
    result = extract(text)
    assert extract(text) == result
    for candidate in result:
        assert candidate.content in text
        assert len(candidate.content.split()) >= 2
        assert candidate.cue
        assert isinstance(candidate.kind, CandidateKind)


@settings(max_examples=100)
@given(
    st.integers(min_value=0, max_value=10000), st.integers(min_value=0, max_value=10000)
)
def test_step_identity_is_independent_of_version_numbers(
    step: int, version: int
) -> None:
    result = extract(f"Step {step}: install package version {version}")
    assert result[0].cue == f"step-prefix:'step {step}'"


def test_unchecked_action_overrides_other_kind_cues() -> None:
    result = extract("[ ] Plan: document what we decided")
    assert len(result) == 1
    assert result[0].kind is CandidateKind.TASK
    assert result[0].content == "Plan: document what we decided"
    assert result[0].cue == "todo-checkbox:'[ ]'"
