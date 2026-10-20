"""The term lane reads an inverted index with the scan's exact scores.

Before the index, every recall tested each query word against every live
capsule (`lexical.bm25_scores`): 66% of retrieve wall time at 64 terms and 96%
at 1,200 on the 24,460-capsule store copy of 2026-10-16. The index stores, for
each word, the capsules that hold it, so the cost follows the postings of the
query words and not the store size.
"""

from __future__ import annotations

import contextlib
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import nott_database
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from storefile import sample_capsule_id

from nmemory import database, lexical
from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.database import Database
from nmemory.lexical import bm25_scores, lexical_tokens, lexical_words
from nmemory.project import in_project_subtree
from nmemory.repositories.capsules import ListFilter
from nmemory.store import Store

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
WORDS = ("alpha", "beta", "gamma", "delta", "naïve", "café", "zeta")
PROJECTS = ("p", "p/sub", "q")
_TERM_OBJECTS = (
    ("TRIGGER", "capsules_term_index_insert"),
    ("TRIGGER", "capsules_term_index_update"),
    ("TRIGGER", "capsules_term_index_delete"),
    ("TABLE", "term_postings"),
    ("TABLE", "term_documents"),
    ("TABLE", "term_index_state"),
)


def _capsule(n: int, text: str, project: str = "p") -> Capsule:
    return Capsule(
        content=text,
        provenance=Provenance(
            source=f"test:term-index:{n}",
            anchor=str(n),
            source_hash=sha256_hex(f"{n}:{text}".encode()),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def _oracle(store: Store, terms: list[str], **kw: Any) -> list[tuple[str, float]]:
    """The scan as it was: score every live capsule with `bm25_scores`."""
    queries = [lexical_words(t) for t in terms if lexical_words(t)]
    if not queries:
        return []
    everything = store.capsules.list(ListFilter())
    eligible = [
        sc
        for sc in everything
        if (
            kw.get("project_id") is None
            or sc.capsule.scope.project_id == kw["project_id"]
        )
        and (
            kw.get("project_prefix") is None
            or in_project_subtree(sc.capsule.scope.project_id, kw["project_prefix"])
        )
        and (kw.get("session_id") is None or sc.session_id == kw["session_id"])
        and (kw.get("effort_set") is None or sc.id in kw["effort_set"])
        and (kw.get("topic_set") is None or sc.id in kw["topic_set"])
    ]
    scores = bm25_scores(
        {sc.id: sc.capsule.content for sc in everything},
        queries,
        [sc.id for sc in eligible],
    )
    ranked = sorted(
        ((sc, scores[sc.id]) for sc in eligible if sc.id in scores),
        key=lambda item: (-item[1], item[0].seq),
    )
    if kw.get("limit") is not None:
        ranked = ranked[: kw["limit"]]
    return [(sc.id, s) for sc, s in ranked]


def _search(store: Store, terms: list[str], **kw: Any) -> list[tuple[str, float]]:
    return [(sc.id, s) for sc, s in store.capsules.search_fts_scoped(terms, **kw)]


def _ready(db: Database) -> bool:
    with db.connection() as handle:
        return database.term_index(handle.conn) is not None


def _index_matches_the_live_capsules(store: Store) -> None:
    """The stored index equals the index of the live capsules, row for row."""
    live = store.capsules.list(ListFilter())
    expected = {
        (word, sc.seq, tf)
        for sc in live
        for word, tf in Counter(lexical_tokens(sc.capsule.content)).items()
    }
    with store.db.connection() as conn, conn.cursor() as cur:
        postings = set(
            cur.execute("SELECT word, seq, tf FROM term_postings").fetchall()
        )
        state = cur.execute("SELECT documents, tokens FROM term_index_state").fetchone()
        lengths = dict(cur.execute("SELECT seq, length FROM term_documents").fetchall())
    assert postings == expected
    assert lengths == {sc.seq: len(lexical_tokens(sc.capsule.content)) for sc in live}
    assert tuple(state) == (len(live), sum(lengths.values()))


def _drop_term_index(store: Store) -> None:
    """Make the file as a build before the index wrote it."""
    with store.db.writing() as handle:
        for kind, name in _TERM_OBJECTS:
            handle.conn.execute(f"DROP {kind} {name}")


# -- (a) the index scores as the scan scores ------------------------------------

_OPERATIONS = st.lists(
    st.one_of(
        st.tuples(
            st.just("ingest"),
            st.lists(st.sampled_from(WORDS), min_size=1, max_size=8).map(" ".join),
            st.tuples(st.sampled_from(PROJECTS), st.sampled_from([None, "s1", "s2"])),
        ),
        st.tuples(st.just("supersede"), st.integers(0, 15), st.integers(0, 15)),
        st.tuples(st.just("forget"), st.integers(0, 15), st.just("")),
        st.tuples(st.just("purge"), st.integers(0, 15), st.just("")),
    ),
    min_size=1,
    max_size=25,
)
_TERMS = st.lists(
    st.sampled_from(["alpha", "beta gamma", "naive", "cafe zeta", "absent", "---"]),
    min_size=1,
    max_size=4,
)
_FENCES = st.sampled_from(
    [
        {},
        {"limit": 3},
        {"project_id": "p"},
        {"project_prefix": "p"},
        {"project_id": "q"},
        {"session_id": "s1"},
        {"effort_set": "odd"},
        {"topic_set": "odd", "project_prefix": "p"},
        {"session_id": "s2", "effort_set": "odd", "limit": 2},
    ]
)


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(operations=_OPERATIONS, terms=_TERMS, fence=_FENCES)
def test_the_index_scores_and_orders_as_the_scan_after_every_writer(
    operations: list[tuple[str, Any, Any]], terms: list[str], fence: dict[str, Any]
) -> None:
    store = Store.open_disposable_test_store()
    ids: list[str] = []
    for op, a, b in operations:
        live = [cid for cid in ids if store.capsules.get_tombstone(cid) is None]
        if op == "ingest":
            project, session = b
            cid, _ = store.append(
                _capsule(len(ids), a, project), NOW, session_id=session
            )
            ids.append(cid)
        elif op == "supersede" and len(live) > 1:
            new, old = live[a % len(live)], live[b % len(live)]
            if new != old:
                store.add_relation("supersedes", new, old, now=NOW)
        elif op in ("forget", "purge") and live:
            mode = "redacted" if op == "forget" else "purged"
            store.capsules.forget_capsule(
                live[a % len(live)], mode, "term index proof", b"key", NOW
            )
        assert _ready(store.db), (
            "the index is not complete: the case compares the scan with itself"
        )
        _index_matches_the_live_capsules(store)
        kw = {k: set(ids[1::2]) if v == "odd" else v for k, v in fence.items()}
        assert _search(store, terms, **kw) == _oracle(store, terms, **kw)


def test_the_indexed_search_answers_and_parses_only_what_it_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store.open_disposable_test_store()
    for n in range(30):
        store.append(_capsule(n, f"alpha {WORDS[n % 7]} {n}", PROJECTS[n % 3]), NOW)
    real = Capsule.from_canonical_json.__func__  # type: ignore[attr-defined]
    parsed: list[str] = []

    def counting(cls: type[Capsule], text: str) -> Capsule:
        parsed.append(text)
        return real(cls, text)  # type: ignore[no-any-return]

    monkeypatch.setattr(Capsule, "from_canonical_json", classmethod(counting))
    assert store.capsules._indexed_search([{"alpha"}], lambda *_: True, 5) is not None
    parsed.clear()
    hits = _search(store, ["alpha"], limit=5)
    assert len(hits) == 5 and len(parsed) == 5
    monkeypatch.undo()
    assert hits == _oracle(store, ["alpha"], limit=5)


# -- (b) the cost does not follow the store size --------------------------------


def _work(store: Store, terms: list[str]) -> tuple[int, int, list[tuple[str, float]]]:
    """SQLite steps and per-document word lookups of one search."""
    lookups = 0
    real = lexical._lexical_words_cached

    def counted(text: str) -> Any:
        nonlocal lookups
        lookups += 1
        return real(text)

    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lexical, "_lexical_words_cached", counted)
        with store.db.tool_boundary(), store.db.connection() as handle:
            handle.conn.set_progress_handler(tick, 1)
            try:
                hits = _search(store, terms, project_prefix="p", limit=10)
            finally:
                handle.conn.set_progress_handler(None, 0)
    return steps, lookups, hits


def test_unrelated_capsules_add_no_work_to_a_search() -> None:
    store = Store.open_disposable_test_store()
    for n in range(5):
        store.append(_capsule(n, f"zephyrine alpha note {n}"), NOW)
    store.append(_capsule(5, "alpha"), NOW)
    terms = ["zephyrine", "zephyrine alpha"]
    small_steps, small_lookups, small = _work(store, terms)
    assert len(small) == 5, "the fixture must match some capsules"

    for n in range(6, 206):
        store.append(_capsule(n, f"unrelated filler text {n}"), NOW)
    large_steps, large_lookups, large = _work(store, terms)

    assert [cid for cid, _ in large] == [cid for cid, _ in small]
    assert large == _oracle(store, terms, project_prefix="p", limit=10)
    assert large_lookups == small_lookups, (
        f"200 unrelated capsules added {large_lookups - small_lookups} per-document "
        "word lookups to one search"
    )
    assert large_steps <= small_steps, (
        f"200 unrelated capsules added {large_steps - small_steps} SQLite steps "
        "to one search"
    )


# -- (c) an existing store gets the index without a long open -------------------


def test_an_existing_store_is_indexed_in_chunks_and_recall_scans_until_then(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store.open_disposable_test_store()
    ids = [
        store.append(_capsule(n, f"{WORDS[n % 7]} {WORDS[(n * 3) % 7]} {n}"), NOW)[0]
        for n in range(12)
    ]
    store.capsules.forget_capsule(ids[4], "purged", "migration proof", b"key", NOW)
    _drop_term_index(store)
    terms = ["alpha", "beta gamma", "naive"]
    expected = _oracle(store, terms)
    assert expected, "the fixture must match some capsules"

    reader = Store(Database(store.db.path, read_only=True))
    assert _search(reader, terms) == expected
    with reader.db.connection() as handle:
        assert not nott_database.query(
            handle.conn, "SELECT 1 FROM sqlite_master WHERE name = 'term_postings'"
        ), "a read-only open wrote the index"

    # An open killed after its first slice leaves a partial build behind.
    monkeypatch.setattr(database, "TERM_INDEX_SLICE", 4)
    real = nott_database.Connection.executemany
    inserts = 0

    def killed(conn: Any, sql: str, rows: Any) -> Any:
        nonlocal inserts
        if sql.startswith("INSERT INTO term_postings"):
            inserts += 1
            if inserts == 2:
                raise KeyboardInterrupt
        return real(conn, sql, rows)

    with monkeypatch.context() as patch:
        patch.setattr(nott_database.Connection, "executemany", killed)
        with pytest.raises(KeyboardInterrupt):
            Database(store.db.path)
    opened = Store(Database(store.db.path, read_only=True))
    with opened.db.connection() as handle:
        state = database._term_index_state(handle.conn)
    assert state is not None and state.valid == 2 and state.cursor, state
    assert opened.capsules._indexed_search([{"alpha"}], lambda *_: True, None) is None
    assert _search(opened, terms) == expected

    # Writers during the build: a forget, and an insert of a build before the index.
    store.capsules.forget_capsule(ids[7], "redacted", "migration proof", b"key", NOW)
    with store.db.writing() as handle:
        handle.conn.execute(
            (
                "INSERT INTO capsules (seq, id, canonical_json, created_at, source_hash, project_id, authority_class, valid_from) SELECT 99, '"
                + sample_capsule_id(99)
                + "', canonical_json, created_at, 'older-build', project_id, authority_class, valid_from FROM capsules WHERE id = ?"
            ),
            (ids[0],),
        )
    assert not _ready(store.db)
    expected = _oracle(store, terms)

    opened = Store(Database(store.db.path))
    assert _ready(opened.db)
    _index_matches_the_live_capsules(opened)
    assert opened.capsules._indexed_search([{"alpha"}], lambda *_: True, None)
    assert _search(opened, terms) == expected


@pytest.mark.parametrize(
    "change",
    [
        (
            "UPDATE capsules SET canonical_json = NULL WHERE id = '"
            + sample_capsule_id(2)
            + "'"
        ),
        (
            "UPDATE capsules SET canonical_json = replace(canonical_json, 'alpha', 'omega') WHERE id = '"
            + sample_capsule_id(2)
            + "'"
        ),
        ("DELETE FROM capsules WHERE id = '" + sample_capsule_id(2) + "'"),
        (
            "INSERT INTO capsules (seq, id, canonical_json, created_at, source_hash, project_id, authority_class, valid_from) SELECT 99, '"
            + sample_capsule_id(99)
            + "', canonical_json, created_at, 'other', project_id, authority_class, valid_from FROM capsules WHERE id = '"
            + sample_capsule_id(2)
            + "'"
        ),
    ],
)
def test_a_writer_outside_the_index_stops_its_use_until_a_writable_open(
    change: str,
) -> None:
    store = Store.open_disposable_test_store()
    for n in range(5):
        store.append(_capsule(n, f"alpha {WORDS[n]} {n}"), NOW)
    assert _ready(store.db)
    with store.db.writing() as handle:
        handle.conn.execute(change)
    assert not _ready(store.db)
    assert _search(store, ["alpha"]) == _oracle(store, ["alpha"])

    reopened = Store(Database(store.db.path))
    assert _ready(reopened.db)
    _index_matches_the_live_capsules(reopened)
    assert _search(reopened, ["alpha"]) == _oracle(reopened, ["alpha"])


def test_another_tokenizer_version_builds_the_index_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store.open_disposable_test_store()
    store.append(_capsule(1, "alpha beta"), NOW)
    monkeypatch.setattr(database, "TOKENIZER", "a newer tokenizer")
    assert not _ready(store.db)
    reopened = Store(Database(store.db.path))
    assert _ready(reopened.db)
    _index_matches_the_live_capsules(reopened)


def test_tokenizer_version_pins_its_tokens() -> None:
    """The index stores tokens. A tokenizer change without a new version is stale."""
    probe = "ΟΣ AΣ İI naïve Café a_b ½² 中文 Hello-World 42x"
    assert (lexical.TOKENIZER_VERSION, lexical_tokens(probe)) == (
        1,
        [
            "οσ",
            "aσ",
            "i̇i",
            "naive",
            "cafe",
            "a",
            "b",
            "½²",
            "中文",
            "hello",
            "world",
            "42x",
        ],
    ), "the tokens changed: increase lexical.TOKENIZER_VERSION and update this probe"


# -- builds that race other writers --------------------------------------------


def _store_without_index(texts: list[str]) -> tuple[Store, list[str]]:
    store = Store.open_disposable_test_store()
    ids = [store.append(_capsule(n, text), NOW)[0] for n, text in enumerate(texts)]
    _drop_term_index(store)
    return store, ids


def _during_the_token_read(monkeypatch: pytest.MonkeyPatch, action: Any) -> None:
    """Run `action` once, while the builder reads tokens outside the write lock."""
    real = database.lexical_frequencies
    done = False

    def hooked(text: str) -> Any:
        nonlocal done
        if not done:
            done = True
            action()
        return real(text)

    monkeypatch.setattr(database, "lexical_frequencies", hooked)


def _check_after_build(store: Store, terms: list[list[str]]) -> None:
    reopened = Store(Database(store.db.path))
    for query in terms:
        assert _search(reopened, query) == _oracle(reopened, query), query
    if _ready(reopened.db):
        _index_matches_the_live_capsules(reopened)


def test_a_text_change_during_the_token_read_never_becomes_a_valid_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ids = _store_without_index(["alpha", "gamma delta", "gamma"])
    changed = _capsule(0, "beta beta").to_canonical_json()

    def change() -> None:
        other = Store(Database(store.db.path, read_only=True))
        with Database(store.db.path).writing() as handle:
            handle.conn.execute(
                "UPDATE capsules SET canonical_json = ? WHERE id = ?", (changed, ids[0])
            )
        assert other.capsules.get(ids[0]) is not None

    _during_the_token_read(monkeypatch, change)
    opened = Store(Database(store.db.path))
    monkeypatch.undo()
    assert [cid for cid, _ in _search(opened, ["beta"])] == [ids[0]]
    assert _search(opened, ["alpha"]) == []
    _check_after_build(store, [["alpha"], ["beta"], ["gamma"]])


def test_a_second_opener_can_forget_while_the_first_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ids = _store_without_index(["alpha one", "alpha two", "beta three"])

    def forget() -> None:
        second = Store(Database(store.db.path))
        second.capsules.forget_capsule(ids[1], "purged", "two openers", b"key", NOW)
        second.capsules.forget_capsule(ids[2], "redacted", "two openers", b"key", NOW)

    _during_the_token_read(monkeypatch, forget)
    opened = Store(Database(store.db.path))
    monkeypatch.undo()
    assert _ready(opened.db)
    _index_matches_the_live_capsules(opened)
    _check_after_build(store, [["alpha"], ["beta"]])


@pytest.mark.parametrize(
    "change",
    [
        (
            "INSERT INTO capsules (seq, id, canonical_json, created_at, source_hash,"
            " project_id, authority_class, valid_from) SELECT 3, 'cap-gap',"
            " canonical_json, created_at, 'gap', project_id, authority_class,"
            " valid_from FROM capsules WHERE seq = 1"
        ),
        "UPDATE capsules SET seq = 3 WHERE seq = 5",
        "UPDATE capsules SET rowid = 3 WHERE seq = 5",
        "UPDATE capsules SET _rowid_ = 3 WHERE seq = 5",
        "UPDATE capsules SET oid = 3 WHERE seq = 5",
    ],
)
def test_a_capsule_in_a_sequence_gap_stops_the_index_use(change: str) -> None:
    store = Store.open_disposable_test_store()
    ids = [store.append(_capsule(n, f"alpha {n}"), NOW)[0] for n in range(5)]
    store.capsules.forget_capsule(ids[2], "purged", "gap", b"key", NOW)
    with store.db.writing() as handle:
        handle.conn.execute("DELETE FROM capsules WHERE seq = 3")
        handle.conn.execute(change)
    assert not _ready(store.db), "the index claims a capsule it does not hold"
    assert _search(store, ["alpha"]) == _oracle(store, ["alpha"])
    _check_after_build(store, [["alpha"]])


def test_each_build_transaction_writes_at_most_one_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = _store_without_index([f"common word {n}" for n in range(9)])
    monkeypatch.setattr(database, "TERM_INDEX_SLICE", 3)
    real = nott_database.Connection.executemany
    batches: list[int] = []

    def counted(conn: Any, sql: str, rows: Any) -> Any:
        rows = list(rows)
        if sql.startswith(("INSERT INTO term_postings", "INSERT INTO term_documents")):
            batches.append(len(rows))
        return real(conn, sql, rows)

    monkeypatch.setattr(nott_database.Connection, "executemany", counted)
    opened = Store(Database(store.db.path))
    monkeypatch.undo()
    assert _ready(opened.db)
    _index_matches_the_live_capsules(opened)
    assert batches and max(batches) <= 3, batches


def test_a_forget_between_build_slices_leaves_no_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ids = _store_without_index(
        ["alpha beta", "alpha zulu yankee", "gamma zulu", "delta yankee"]
    )
    monkeypatch.setattr(database, "TERM_INDEX_SLICE", 2)
    real = nott_database.write
    done = False

    @contextlib.contextmanager
    def between_slices(conn: Any) -> Any:
        nonlocal done
        if not done:
            with store.db.connection() as handle:
                state = database._term_index_state(handle.conn)
            if state is not None and state.valid == 2 and state.cursor:
                done = True
                Store(Database(store.db.path)).capsules.forget_capsule(
                    ids[1], "purged", "between slices", b"key", NOW
                )
        with real(conn) as handle:
            yield handle

    monkeypatch.setattr(database.nott_database, "write", between_slices)
    opened = Store(Database(store.db.path))
    monkeypatch.undo()
    assert done, "the forget never ran between two slices"
    assert _ready(opened.db)
    _index_matches_the_live_capsules(opened)
    _check_after_build(store, [["zulu"], ["yankee"], ["alpha"]])


def test_a_forget_by_another_tokenizer_invalidates_the_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store.open_disposable_test_store()
    ids = [store.append(_capsule(n, f"alpha {n}"), NOW)[0] for n in range(3)]
    assert _ready(store.db)
    with monkeypatch.context() as older:
        older.setattr(database, "TOKENIZER", "an older tokenizer")
        store.capsules.forget_capsule(ids[1], "purged", "older writer", b"key", NOW)
    assert not _ready(store.db), "a forget by another tokenizer left the index valid"
    _check_after_build(store, [["alpha"]])


def test_the_tokenizer_identity_names_the_unicode_data() -> None:
    import unicodedata

    assert unicodedata.unidata_version in database.TOKENIZER
