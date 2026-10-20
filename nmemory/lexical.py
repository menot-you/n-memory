"""Pure lexical normalization and global-corpus BM25 scoring.

Persistence and scope eligibility belong to Store; the scorer retains global
statistics even when only a fenced subset is eligible for delivery.
"""

from __future__ import annotations

import logging
import math
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from collections.abc import Set as AbstractSet
from functools import lru_cache
from types import MappingProxyType
from typing import Final

from .capsule_ref import fold_diacritic

#: How many distinct texts the tokenizer keeps. It must fit the ENTIRE STORE
#: or the cache is useless: every search iterates through all capsules in the same
#: order, so a cache smaller than the store causes total eviction on every sweep and
#: zero hits. The live store had 12,177 capsules on 2026-09-18 and the
#: LongMemEval corpus has 18,810 documents.
#:
#: ponytail: the CEILING is memory — the key is the text itself, so each entry
#: stores the content AND its tokens. Measured on the arena corpus (median
#: 9988 B per document): ~188 MB of text and ~0.5 GB of tokens when the cache is
#: full. For the live store (~1 KB capsules) it is tens of MB. Anyone seeding
#: a large corpus adjusts `NOTT_NMEMORY_TOKEN_CACHE`. Capsule search reads the
#: term index (`database.TERM_INDEX_DDL`) once it is complete. Only the scan
#: before that and the corpus lane still load every document per search.
_TOKEN_CACHE_MAX: Final = max(
    1, int(os.environ.get("NOTT_NMEMORY_TOKEN_CACHE", "25000"))
)


#: Once per process: the store exceeded the cache and search became
#: quadratic again. Without this, the regression is SILENT — every sweep evicts everything
#: in the same order and the hit rate drops to zero without a log or metric (independent
#: review finding, 2026-09-18).
_token_cache_overflow_warned = False


def _warn_if_store_exceeds_token_cache(document_count: int) -> None:
    global _token_cache_overflow_warned
    if _token_cache_overflow_warned or document_count <= _TOKEN_CACHE_MAX:
        return
    _token_cache_overflow_warned = True
    logging.getLogger("nmemory.store").warning(
        "token cache smaller than the store: %d documents vs maxsize %d — every "
        "search evicts the whole cache and the per-item cost is quadratic again; "
        "raise NOTT_NMEMORY_TOKEN_CACHE",
        document_count,
        _TOKEN_CACHE_MAX,
    )


@lru_cache(maxsize=_TOKEN_CACHE_MAX)
def _lexical_tokens_cached(text: str) -> tuple[str, ...]:
    """Cache immutable tokens by content, with ASCII words normalized in bulk."""
    # Exclude underscores from Unicode word characters to match str.isalnum().
    return tuple(
        word.lower()
        if word.isascii()
        # Per-character lowercase preserves sigma and dotted-I behavior.
        else "".join(fold_diacritic(ch.lower()) for ch in word)
        for word in re.findall(r"[^\W_]+", text)
    )


def lexical_tokens(text: str) -> list[str]:
    """Split text into alphanumeric tokens, lowercase, and diacritic-folded."""
    return list(_lexical_tokens_cached(text))


@lru_cache(maxsize=_TOKEN_CACHE_MAX)
def _lexical_words_cached(text: str) -> Mapping[str, int]:
    """Immutable word frequencies, computed once per distinct content.

    The same bounded cache supplies O(1) document membership and term frequency;
    it replaces the former word set instead of adding a third corpus cache.
    """
    return MappingProxyType(Counter(_lexical_tokens_cached(text)))


def lexical_words(text: str) -> set[str]:
    return set(_lexical_words_cached(text))


#: The version of the tokens `lexical_tokens` makes. The term index stores these
#: tokens, so a change to the tokenizer must increase this number. The next
#: writable open then builds the index again. `test_tokenizer_version_pins_its_tokens`
#: fails until the number changes.
TOKENIZER_VERSION: Final = 1
#: The identity the term index stores: the version above and the Unicode data
#: that `str.lower`, `str.isalnum` and the word pattern read. A Python upgrade
#: with other Unicode data builds the index again.
TOKENIZER: Final = f"{TOKENIZER_VERSION}/unicode-{unicodedata.unidata_version}"


def lexical_frequencies(text: str) -> Mapping[str, int]:
    """The immutable word frequencies of `text`, as the term index stores them."""
    return _lexical_words_cached(text)


def _bm25_term(
    freq: float,
    doc_freq: float,
    document_count: float,
    length: float,
    avg_length: float,
) -> float:
    """One word's BM25 contribution to one document, shared by both scorers."""
    inv_freq = math.log(1.0 + (document_count - doc_freq + 0.5) / (doc_freq + 0.5))
    denominator = freq + 1.2 * (0.25 + 0.75 * length / avg_length)
    return inv_freq * freq * 2.2 / denominator


def bm25_scores(
    contents: Mapping[str, str],
    queries: Sequence[AbstractSet[str]],
    eligible_ids: Collection[str],
) -> dict[str, float]:
    """Score eligible matches using every live document for corpus statistics.

    Each matching document is counted once, instead of rescanning its token
    sequence once per query word. Query iteration and floating-point arithmetic
    remain unchanged so ranking and exposed scores retain their exact contract.
    """
    if not queries or not contents:
        return {}
    query_words: set[str] = set().union(*queries)
    corpus = {cid: _lexical_tokens_cached(text) for cid, text in contents.items()}
    words = {cid: _lexical_words_cached(text) for cid, text in contents.items()}
    document_count = float(len(corpus))
    _warn_if_store_exceeds_token_cache(len(corpus))
    avg_length = sum(len(tokens) for tokens in corpus.values()) / document_count
    if avg_length == 0.0:
        avg_length = 1.0
    document_frequency = {
        word: float(sum(word in document for document in words.values()))
        for word in query_words
    }
    scores: dict[str, float] = {}
    for cid in eligible_ids:
        if not any(query <= words[cid].keys() for query in queries):
            continue
        tokens = corpus[cid]
        frequencies = words[cid]
        length = float(len(tokens))
        score = 0.0
        for word in query_words:
            freq = float(frequencies.get(word, 0))
            if freq == 0.0:
                continue
            score += _bm25_term(
                freq, document_frequency[word], document_count, length, avg_length
            )
        scores[cid] = score
    return scores


def term_matches(
    postings: Mapping[str, Mapping[int, int]], queries: Sequence[AbstractSet[str]]
) -> set[int]:
    """The documents that hold every word of at least one query."""
    matches: set[int] = set()
    for query in queries:
        words = iter(query)
        found = set(postings.get(next(words), ()))
        for word in words:
            found &= postings.get(word, {}).keys()
        matches |= found
    return matches


def bm25_postings_scores(
    postings: Mapping[str, Mapping[int, int]],
    queries: Sequence[AbstractSet[str]],
    lengths: Mapping[int, int],
    document_count: int,
    token_count: int,
) -> dict[int, float]:
    """`bm25_scores` read from an inverted index, with the same floats.

    `postings` maps each query word to `{document: term frequency}` over every
    live document, so a word's document frequency is its posting count.
    `lengths` holds the token count of the eligible matches alone. Each
    document adds its words in the order `bm25_scores` adds them, so every
    score is bit-identical.
    """
    if not queries or not document_count:
        return {}
    documents = float(document_count)
    avg_length = token_count / documents
    if avg_length == 0.0:
        avg_length = 1.0
    scores = dict.fromkeys(lengths, 0.0)
    query_words: set[str] = set().union(*queries)
    for word in query_words:
        found = postings.get(word, {})
        doc_freq = float(len(found))
        # Walk the shorter side. Either side gives each document the same sum.
        pairs = (
            ((key, found.get(key)) for key in scores)
            if len(scores) < len(found)
            else ((key, freq) for key, freq in found.items() if key in scores)
        )
        for key, freq in pairs:
            if freq:
                scores[key] += _bm25_term(
                    float(freq), doc_freq, documents, float(lengths[key]), avg_length
                )
    return scores
