"""Independent BM25 oracle and bounded work for repeated document terms."""

import inspect
import math
import re
from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    format_rfc3339,
    sha256_hex,
)
from nmemory.lexical import lexical_tokens
from nmemory.repositories.capsules import CapsuleRepository, StoredCapsule
from nmemory.store import Store

NOW = datetime(2026, 9, 19, tzinfo=UTC)


def capsule(text, seq, project="p", session=None):
    value = Capsule(
        content=text,
        provenance=Provenance(
            source=f"test:{seq}", anchor=str(seq), source_hash=sha256_hex(text.encode())
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )
    return StoredCapsule(str(seq), seq, value, NOW, session)


def oracle(rows, terms, eligible=None, limit=None):
    # Deliberately independent tokenizer for the generated ASCII word domain.
    tokens = {r.id: re.findall(r"[a-z0-9]+", r.capsule.content.lower()) for r in rows}
    groups = [set(re.findall(r"[a-z0-9]+", t.lower())) for t in terms]
    groups = [g for g in groups if g]
    if not groups or not rows:
        return []
    words = set().union(*groups)
    mean = sum(map(len, tokens.values())) / len(rows) or 1.0
    out = []
    for row in rows:
        if eligible is not None and row.id not in eligible:
            continue
        text = tokens[row.id]
        if not any(group <= set(text) for group in groups):
            continue
        score = 0.0
        for word in words:
            frequency = text.count(word)
            if frequency:
                documents = sum(word in document for document in tokens.values())
                score += (
                    math.log(1 + (len(rows) - documents + 0.5) / (documents + 0.5))
                    * frequency
                    * 2.2
                    / (frequency + 1.2 * (0.25 + 0.75 * len(text) / mean))
                )
        out.append((row.id, score))
    out.sort(key=lambda pair: (-pair[1], next(r.seq for r in rows if r.id == pair[0])))
    return out if limit is None else out[:limit]


def memory_search(monkeypatch, rows, terms, **kwargs):
    capsules = object.__new__(CapsuleRepository)
    lexical_rows = [
        (
            r.id,
            r.seq,
            r.capsule.to_canonical_json(),
            format_rfc3339(r.created_at),
            r.session_id,
            r.capsule.content,
            r.capsule.scope.project_id,
        )
        for r in rows
    ]
    monkeypatch.setattr(capsules, "_lexical_rows", lambda: lexical_rows)
    # These rows have no store, so no term index: the scan answers.
    monkeypatch.setattr(capsules, "_indexed_search", lambda *_: None)
    return [(r.id, score) for r, score in capsules.search_fts_scoped(terms, **kwargs)]


def test_query_does_not_rescan_document_for_each_query_word(monkeypatch):
    owner = inspect.getmodule(lexical_tokens)
    real = owner._lexical_tokens_cached
    examined = 0

    class Counted(tuple):
        def count(self, word):
            nonlocal examined
            examined += len(self)
            return super().count(word)

    monkeypatch.setattr(
        owner, "_lexical_tokens_cached", lambda text: Counted(real(text))
    )
    rows = [capsule(" ".join(["alpha", "beta", "gamma", "delta"] * 500), 1)]
    terms = ["alpha beta gamma delta"]
    assert memory_search(monkeypatch, rows, terms) == oracle(rows, terms)
    assert examined <= 2 * 2000, (
        f"ranking scanned {examined} tokens for one 2000-token document"
    )


@settings(max_examples=100)
@given(
    documents=st.lists(
        st.lists(
            st.sampled_from(["alpha", "beta", "gamma", "delta"]),
            min_size=1,
            max_size=15,
        ),
        max_size=12,
    ),
    terms=st.lists(
        st.sampled_from(["alpha", "beta gamma", "absent", "---", "alpha alpha"]),
        max_size=5,
    ),
    limit=st.one_of(st.none(), st.integers(min_value=0, max_value=10)),
)
def test_ranker_matches_independent_corpus_oracle(documents, terms, limit):
    rows = [capsule(" ".join(words), i) for i, words in enumerate(documents)]
    with pytest.MonkeyPatch.context() as patch:
        assert memory_search(patch, rows, terms, limit=limit) == oracle(
            rows, terms, limit=limit
        )


def test_scope_fences_preserve_global_document_statistics(monkeypatch):
    rows = [
        capsule("alpha", 1, "p", "s"),
        capsule("alpha beta " * 20, 2, "other"),
        capsule("alpha gamma", 3, "p/sub", "s"),
    ]
    assert memory_search(
        monkeypatch,
        rows,
        ["alpha"],
        project_id="p",
        session_id="s",
        effort_set={"1", "2"},
        topic_set={"1", "3"},
    ) == oracle(rows, ["alpha"], {"1"})
    assert memory_search(monkeypatch, rows, ["alpha"], project_prefix="p") == oracle(
        rows, ["alpha"], {"1", "3"}
    )


def test_repeated_query_reuses_the_immutable_document_histogram(monkeypatch):
    from nmemory import lexical

    lexical._lexical_tokens_cached.cache_clear()
    lexical._lexical_words_cached.cache_clear()
    actual = lexical.Counter
    document_counts = 0

    def count_once(tokens):
        nonlocal document_counts
        if len(tokens) > 100:
            document_counts += 1
        return actual(tokens)

    monkeypatch.setattr(lexical, "Counter", count_once)
    rows = [capsule("alpha beta " * 1000, 1)]
    first = memory_search(monkeypatch, rows, ["alpha"])
    assert memory_search(monkeypatch, rows, ["alpha"]) == first
    assert document_counts == 1, "immutable content histogram was recomputed"


def test_native_ranker_observes_other_writer_and_purge():
    """A cached histogram must never cache the live corpus or its global counts."""
    store = Store.open_disposable_test_store()
    first, _ = store.append(capsule("alpha beta", 1).capsule, NOW)
    store.append(capsule("gamma " * 20, 2, "outside").capsule, NOW)

    def check():
        rows = store.capsules.list()
        eligible = {r.id for r in rows if r.capsule.scope.project_id == "p"}
        actual = [
            (r.id, score)
            for r, score in store.capsules.search_fts_scoped(["alpha"], project_id="p")
        ]
        assert actual == oracle(rows, ["alpha"], eligible)
        return actual

    before = check()
    other = Store(store.db)
    extra, _ = other.append(capsule("alpha " * 50, 3, "outside").capsule, NOW)
    after = check()
    assert after[0][0] == first
    assert after[0][1] != before[0][1]
    other.capsules.forget_capsule(
        extra, "purged", "disposable ranking proof", b"test-key", NOW
    )
    assert check() == before


def test_cached_frequencies_are_immutable_and_word_copies_are_independent():
    from nmemory.lexical import _lexical_words_cached, lexical_words

    counts = _lexical_words_cached("alpha alpha beta")
    assert counts == {"alpha": 2, "beta": 1}
    with pytest.raises(TypeError):
        counts["alpha"] = 99
    copy = lexical_words("alpha alpha beta")
    copy.clear()
    assert lexical_words("alpha alpha beta") == {"alpha", "beta"}


def test_empty_token_documents_still_contribute_to_global_statistics(monkeypatch):
    rows = [capsule("---", 1), capsule("alpha", 2)]
    assert memory_search(monkeypatch, rows, ["alpha", "absent"]) == oracle(
        rows, ["alpha", "absent"]
    )
    assert memory_search(monkeypatch, rows[:1], ["alpha"]) == []
