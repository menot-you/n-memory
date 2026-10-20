"""Cold lexical search must not normalize ASCII one character at a time."""

from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from nmemory import lexical
from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.capsule_ref import fold_diacritic
from nmemory.store import Store


def original_tokens(text):
    tokens = []
    current = []
    for char in text:
        if char.isalnum():
            current.append(fold_diacritic(char.lower()))
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def test_first_search_does_not_fold_each_ascii_character(monkeypatch):
    now = datetime(2026, 10, 16, tzinfo=UTC)
    store = Store.open_disposable_test_store()
    for number in range(10):
        content = f"{number} " + "Alpha beta " * 100 + "café"
        store.append(
            Capsule(
                content=content,
                provenance=Provenance(
                    source=f"test:cold:{number}",
                    anchor=str(number),
                    source_hash=sha256_hex(content.encode()),
                ),
                confidence=0.9,
                freshness=Freshness(valid_from=now),
                scope=Scope(project_id="cold"),
                authority_class=AuthorityClass.USER_STATED,
                instruction_taint=False,
            ),
            now,
        )
    lexical._lexical_tokens_cached.cache_clear()
    lexical._lexical_words_cached.cache_clear()
    calls = 0

    def counted(char):
        nonlocal calls
        calls += 1
        return fold_diacritic(char)

    monkeypatch.setattr(lexical, "fold_diacritic", counted)
    assert len(store.capsules.search_fts_scoped(["alpha"])) == 10
    assert calls <= 40, f"cold search used {calls} character folds, expected at most 40"


@given(st.text())
def test_tokenizer_preserves_character_normalization(text):
    assert lexical.lexical_tokens(text) == original_tokens(text)


def test_tokenizer_preserves_unicode_boundaries_and_lowercase():
    text = "ΟΣ AΣ ΣΣ İI ıI naïve cafe\u0301 café 𐐀中文 ½² a_b \ud800\x00"
    assert lexical.lexical_tokens(text) == original_tokens(text)


def test_tokenizer_preserves_all_unicode_characters():
    text = "_".join(chr(codepoint) for codepoint in range(0x110000))
    assert lexical.lexical_tokens(text) == original_tokens(text)
