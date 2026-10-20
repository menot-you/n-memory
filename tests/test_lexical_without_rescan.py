"""Lexical search stops re-tokenizing the ENTIRE store on every call.

Measured 2026-09-18 on two independent fronts:

- profile of ONE write in the arena store at 2362 capsules: `lexical_tokens` accounts for
  25.9 s of the 26.9 s total cost; `append_capture` is 65 ms;
- replay of 180 real documents in a disposable database: the cost PER ITEM grows
  linearly with the store (33 ms + 8.22 ms × n), so the total is quadratic —
  projection of ~404 h to seed the 18,810 documents of LongMemEval.

The cause is `Store.search_fts_scoped` (store.py:2331-2337): loads the whole store and
calls `lexical_tokens` per capsule, a character-by-character loop with
`fold_diacritic` per char (7.35 million calls in a single `ingest_capture`). The
`limit` truncates the RESULT, never the work. And `ingest_capture` pays this
four times per item: `near_duplicate_hint`, `sibling_hints`, and the
re-tokenization of each candidate in both loops.

What these falsifiers pin is that the same content is never tokenized twice
and the score does not change by a float. What they DO NOT prove, and the
independent review of 2026-09-18 was right to point out: a cache keyed by `id`
inside the search loop would pass here, because `canonical_json` is never
rewritten and content-by-id is immutable in tests and in the store. Key purity
is guaranteed by the signature of `lexical_tokens`, which only receives text, not by
a test. The cache is correct by construction because the key is the CONTENT — and, in the database, the
`canonical_json` of an id is never rewritten: the only write that touches it is
purge, which sets it to null (store.py:3328), and `list()` filters `IS NOT NULL`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from nmemory import lexical as store_mod
from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.ingest import significant_tokens
from nmemory.lexical import lexical_tokens, lexical_words
from nmemory.project import in_project_subtree
from nmemory.repositories.capsules import ListFilter
from nmemory.store import Store

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
MARKER = "varrimento"


def cap(n: int) -> Capsule:
    content = (
        f"Capsule {n} about the lexical {MARKER} of the whole store, with accents "
        f"to give fold_diacritic work: façade, naïve, café, ünïcödé {n}."
    )
    return Capsule(
        content=content,
        provenance=Provenance(
            source=f"test:lexical:{n}",
            anchor=f"tests/test_lexical_without_rescan.py:{n}",
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id="pynott"),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


class _Contador:
    """Counts passes through the character-by-character loop, without altering it."""

    def __init__(self) -> None:
        self.chars = 0
        self._real = store_mod.fold_diacritic

    def __call__(self, c: str) -> str:
        self.chars += 1
        return self._real(c)


def test_a_segunda_busca_NAO_volta_a_tokenizar_a_loja(monkeypatch) -> None:
    """The campaign falsifier: two identical searches over the SAME store.

    Without cache, the second pays exactly the same as the first — this is how the
    cost per item grows with the store and the total becomes quadratic. With cache, the
    second does not touch a single character.
    """
    store = Store.open_disposable_test_store()
    for n in range(12):
        store.append(cap(n), NOW)
    # The term index tokenizes nothing per search. This pins the scan that
    # answers while the index is incomplete (tests/test_term_index.py).
    monkeypatch.setattr(store.capsules, "_indexed_search", lambda *_: None)

    # The cache is GLOBAL to the process: without this, running the file twice in the
    # same session (pytest-repeat) would find these texts already tokenized and the
    # `> 0` below failed without any regression.
    store_mod._lexical_tokens_cached.cache_clear()
    store_mod._lexical_words_cached.cache_clear()
    contador = _Contador()
    monkeypatch.setattr(store_mod, "fold_diacritic", contador)

    primeira = store.capsules.search_fts_scoped([MARKER], limit=5)
    apos_primeira = contador.chars
    assert apos_primeira > 0, "the search tokenized nothing: the test measures nothing"

    segunda = store.capsules.search_fts_scoped([MARKER], limit=5)
    apos_segunda = contador.chars

    assert apos_segunda == apos_primeira, (
        f"the second search re-tokenized {apos_segunda - apos_primeira} characters "
        "of the SAME immutable store — this repeated work is what makes the per-item "
        "cost proportional to the size of the store"
    )
    # and the result is the SAME, to the float: a cache that changes the score is not a cache
    assert [(sc.id, s) for sc, s in primeira] == [(sc.id, s) for sc, s in segunda]


def test_o_cache_e_por_CONTEUDO_logo_um_texto_novo_ainda_tokeniza(
    monkeypatch,
) -> None:
    """The other half: a cache returning stale tokens for new text would give
    the wrong score silently. The key is the content, not the id nor the position."""
    store_mod._lexical_tokens_cached.cache_clear()
    store_mod._lexical_words_cached.cache_clear()
    contador = _Contador()
    monkeypatch.setattr(store_mod, "fold_diacritic", contador)

    lexical_tokens("naïve café façade")
    primeiro = contador.chars
    assert primeiro > 0

    lexical_tokens("naïve café façade")
    assert contador.chars == primeiro, "the same text was tokenized twice"

    lexical_tokens("another entirely different sentence café")
    assert contador.chars > primeiro, (
        "a NEW text was not tokenized: the cache is serving the tokens of "
        "other content, and the score would come from a document nobody wrote"
    )


def test_o_tokenizador_devolve_lista_que_o_chamador_pode_mexer() -> None:
    """A cache returning the SAME list to two callers lets the first
    corrupt the second. Each call gets its own copy."""
    a = lexical_tokens("a sentence with tokens")
    b = lexical_tokens("a sentence with tokens")
    assert a == b
    assert a is not b, "two calls share the list: changing one changes the other"
    a.append("intruso")
    assert "intruso" not in lexical_tokens("a sentence with tokens")


def test_significant_tokens_nao_percorre_a_lista_por_token() -> None:
    """`token not in out` in a LIST makes this O(tokens²) — measured as the third
    and fourth tokenization of each ingest candidate (ingest.py:228).

    What is pinned here is the OUTPUT: dedup preserving the order of the first
    occurrence, and only tokens of 3+ characters. The complexity is unconstrained.
    """
    texto = "alfa be gama alfa delta gama alfa epsilon"
    assert significant_tokens(texto) == ["alfa", "gama", "delta", "epsilon"]

    # many repeated tokens: this is the case punished by the quadratic loop
    grande = " ".join(f"token{i % 50}" for i in range(4000))
    saida = significant_tokens(grande)
    assert saida == [f"token{i}" for i in range(50)]
    assert len(saida) == len(set(saida))


def test_a_loja_maior_que_o_cache_avisa_uma_vez(monkeypatch, caplog) -> None:
    """Regression to quadratic was SILENT: with the store above `maxsize`
    each sweep evicts everything and the hit rate drops to zero, without log or
    metric. Whoever seeds a large corpus must know why it slowed down."""
    import logging as _logging

    monkeypatch.setattr(store_mod, "_TOKEN_CACHE_MAX", 3)
    monkeypatch.setattr(store_mod, "_token_cache_overflow_warned", False)
    with caplog.at_level(_logging.WARNING, logger="nmemory.store"):
        store_mod._warn_if_store_exceeds_token_cache(4)
        store_mod._warn_if_store_exceeds_token_cache(4)
    avisos = [r for r in caplog.records if "token cache smaller" in r.getMessage()]
    assert len(avisos) == 1, "warns once per process, not per search"
    assert "4" in avisos[0].getMessage() and "3" in avisos[0].getMessage()

    monkeypatch.setattr(store_mod, "_token_cache_overflow_warned", False)
    caplog.clear()
    with caplog.at_level(_logging.WARNING, logger="nmemory.store"):
        store_mod._warn_if_store_exceeds_token_cache(3)
    assert not [r for r in caplog.records if "token cache smaller" in r.getMessage()], (
        "a store that FITS in the cache does not warn"
    )


# -- parse only what is returned ------------------------------------------------

RARE = "zephyrine"
WORDS = ("alpha beta", "beta gamma", "gamma delta alpha", f"delta {RARE}", "naïve café")
PROJECTS = ("lex", "lex/child", "lexx", "other")


def _capsule(n: int, project: str, words: str) -> Capsule:
    content = f"Capsule {n} for {project}: {words}."
    return Capsule(
        content=content,
        provenance=Provenance(
            source=f"test:lexical-parse:{n}",
            anchor=f"tests/test_lexical_without_rescan.py:parse:{n}",
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def _mixed_store() -> tuple[Store, list[str]]:
    store = Store.open_disposable_test_store()
    ids = []
    for n in range(40):
        cid, _ = store.append(
            _capsule(n, PROJECTS[n % 4], WORDS[n % 5]), NOW, session_id=f"s{n % 3}"
        )
        ids.append(cid)
    return store, ids


def _oracle(store: Store, terms: list[str], **kw: Any) -> list[tuple[str, float]]:
    """The search as it was: parse every live capsule, then filter and score."""
    queries = [lexical_words(t) for t in terms if lexical_words(t)]
    everything = store.capsules.list(ListFilter(limit=None))
    eligible = [
        sc
        for sc in everything
        if (kw.get("effort_set") is None or sc.id in kw["effort_set"])
        and (kw.get("topic_set") is None or sc.id in kw["topic_set"])
        and (
            kw.get("project_id") is None
            or sc.capsule.scope.project_id == kw["project_id"]
        )
        and (
            kw.get("project_prefix") is None
            or in_project_subtree(sc.capsule.scope.project_id, kw["project_prefix"])
        )
        and (kw.get("session_id") is None or sc.session_id == kw["session_id"])
    ]
    scores = store_mod.bm25_scores(
        {sc.id: sc.capsule.content for sc in everything},
        queries,
        [sc.id for sc in eligible],
    )
    matches = sorted(
        ((sc, scores[sc.id]) for sc in eligible if sc.id in scores),
        key=lambda item: (-item[1], item[0].seq),
    )
    if kw.get("limit") is not None:
        matches = matches[: kw["limit"]]
    return [(sc.id, s) for sc, s in matches]


def test_the_search_parses_only_the_capsules_it_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every call parsed the canonical JSON of the WHOLE store into capsules.

    Measured on the imported 13.6k-capsule store: 580 ms of a 1.27 s warm
    `nmemory_capsule_retrieve`, which returns at most 50 of them.
    """
    store, _ = _mixed_store()
    real = Capsule.from_canonical_json.__func__  # type: ignore[attr-defined]
    parsed: list[str] = []

    def counting(cls: type[Capsule], text: str) -> Capsule:
        parsed.append(text)
        return real(cls, text)  # type: ignore[no-any-return]

    monkeypatch.setattr(Capsule, "from_canonical_json", classmethod(counting))
    hits = store.capsules.search_fts_scoped([RARE])
    assert len(hits) == 8, "the fixture must match some capsules and not others"
    assert len(parsed) == len(hits)


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"limit": 3},
        {"project_id": "lex"},
        {"project_prefix": "lex"},
        {"project_prefix": "lex", "session_id": "s1"},
        {"session_id": "s2", "limit": 4},
        {"effort_set": "odd"},
        {"topic_set": "odd", "project_prefix": "lex"},
    ],
)
@pytest.mark.parametrize(
    "terms", [["alpha"], ["gamma", "delta alpha"], ["naive"], [RARE]]
)
def test_the_search_equals_parsing_everything(
    kw: dict[str, Any], terms: list[str]
) -> None:
    store, ids = _mixed_store()
    kw = {k: set(ids[1::2]) if v == "odd" else v for k, v in kw.items()}
    got = [(sc.id, s) for sc, s in store.capsules.search_fts_scoped(terms, **kw)]
    assert got == _oracle(store, terms, **kw)
    assert got, "the case matched nothing: it proves nothing"
