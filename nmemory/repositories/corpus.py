"""The durable context corpus: sources, their chunks, and where each chunk occurs."""

from __future__ import annotations

import builtins
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ..capsule import sha256_hex
from ..database import writes
from ..lexical import bm25_scores, lexical_tokens, lexical_words
from ..project import glob_subtree_pattern
from .base import EmptyField, Repository

QUESTION_FILLER = frozenset(
    [
        "a",
        "as",
        "o",
        "os",
        "um",
        "uma",
        "de",
        "do",
        "da",
        "dos",
        "das",
        "em",
        "no",
        "na",
        "por",
        "para",
        "pra",
        "e",
        "ou",
        "que",
        "qual",
        "como",
        "porque",
        "pq",
        "isso",
        "isto",
        "voce",
        "ele",
        "ela",
        "elas",
        "eles",
        "nao",
        "tem",
        "ha",
        "pode",
        "what",
        "can",
        "does",
        "do",
        "the",
        "is",
        "are",
        "how",
        "when",
        "why",
        "where",
        "who",
        "which",
        "and",
        "or",
        "of",
        "in",
        "for",
        "to",
        "an",
        "it",
        "this",
        "that",
        "there",
    ]
)

# The repository's operating language is Portuguese and much of its source
# documentation is English. Keep these expansions small and domain-specific;
# unmatched words still take part in lexical ranking.
PT_CONTEXT_TERMS: dict[str, tuple[str, ...]] = {
    "decaem": ("decay", "decays"),
    "decaimento": ("decay",),
    "somem": ("archive", "disappear"),
    "arquivam": ("archive",),
    "mutante": ("mutant", "mutation"),
    "mutantes": ("mutants", "mutation"),
    "cenario": ("scenario",),
    "cenarios": ("scenarios", "scenario"),
    "plano": ("plan",),
    "entregar": ("deliver",),
    "roda": ("run",),
    "rodar": ("run",),
    "funciona": ("work", "run"),
    "solucao": ("solution",),
    "autenticacao": ("authentication", "auth"),
    "banco": ("database", "sqlite"),
    "bloqueio": ("lock",),
    "busca": ("search", "retrieval"),
    "codigo": ("code",),
    "configuracao": ("configuration", "config"),
    "configurar": ("configure", "configuration", "config"),
    "dados": ("data", "database"),
    "dependencia": ("dependency",),
    "documentacao": ("documentation", "docs"),
    "documento": ("document", "docs"),
    "erro": ("error",),
    "escrita": ("write", "writes", "writer"),
    "execucao": ("execution", "run"),
    "falha": ("failure",),
    "fonte": ("source",),
    "indice": ("index",),
    "integracao": ("integration",),
    "memoria": ("memory",),
    "pagina": ("page",),
    "permissao": ("permission",),
    "porta": ("port",),
    "rede": ("network",),
    "regra": ("rule",),
    "resultado": ("result",),
    "revisao": ("review",),
    "seguranca": ("security",),
    "servico": ("service",),
    "tarefa": ("task",),
    "teste": ("test",),
    "usuario": ("user",),
    "versao": ("version",),
}

PATH_GENERIC_TERMS = frozenset({"apps", "core", "libs", "mcp", "python", "tools"})


def source_role(path: str) -> float:
    """Prefer a component's README for capability questions, then source, then history."""
    if path.startswith("changelog.d/"):
        return -1.0
    if path == "README.md":
        return 9.0
    if path.endswith("/README.md"):
        return 9.0 - 0.25 * path.count("/")
    if path.endswith((".md", ".markdown")):
        return 2.0
    return 1.0


def path_matches(path: str, query_counts: Counter[str]) -> int:
    path_words = lexical_words(path)
    parts = path.split("/")
    matches = 0
    for word, count in query_counts.items():
        if word in PATH_GENERIC_TERMS:
            continue
        variants = {word}
        variants.update(PT_CONTEXT_TERMS.get(word, ()))
        if (
            len(word) > 4
            and word.startswith("n")
            and len(parts) > 1
            and parts[0] in {"apps", "libs"}
            and parts[1] == word[1:]
        ):
            variants.add(word[1:])
        if len(word) > 4 and word.endswith("s"):
            variants.add(word[:-1])
        variants.update(
            variant[:-1]
            for variant in tuple(variants)
            if len(variant) > 4 and variant.endswith("s")
        )
        if path_words & variants:
            matches += 1 + 2 * (count - 1)
    return matches


def occurrence_key(
    content_hash: str,
    source_key: str,
    path: str,
    start_line: int | None,
    end_line: int | None,
) -> str:
    def bound(line: int | None) -> str:
        return str(line) if line is not None else "-"

    fields = [
        content_hash,
        source_key,
        path,
        bound(start_line),
        bound(end_line),
    ]
    joined = "".join(f"{len(f)}:{f}" for f in fields)
    return sha256_hex(joined.encode("utf-8"))


@dataclass(frozen=True)
class CorpusSourceRecord:
    source_key: str
    kind: str
    root: str
    revision: str
    indexed_at: str
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class CorpusOccurrence:
    source_key: str
    project_id: str | None
    path: str
    start_line: int | None
    end_line: int | None
    revision: str
    provenance: str


@dataclass(frozen=True)
class CorpusChunkInput:
    content_hash: str
    content: str
    occurrence: CorpusOccurrence


@dataclass
class CorpusSearchHit:
    id: str
    content_hash: str
    content: str
    score: float
    occurrences: builtins.list[CorpusOccurrence]


def ensure_code_source_kind(cur: Any) -> None:
    """Expand the v1 source enum in place, keeping the old binary readable.

    The old binary ignores `code` searches and accepts this table's wider CHECK.
    The schema version stays 1 so a symlink rollback can reopen the live store.
    """
    cur.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'corpus_sources'"
    )
    row = cur.fetchone()
    if row is None or "'code'" in str(row[0]):
        return
    cur.execute(
        "CREATE TABLE corpus_sources_expanded ("
        "source_key TEXT PRIMARY KEY, "
        "kind TEXT NOT NULL CHECK (kind IN ('markdown', 'code', 'git', 'rustdoc')), "
        "root TEXT NOT NULL, revision TEXT NOT NULL, indexed_at TEXT NOT NULL, "
        "status TEXT NOT NULL CHECK (status IN ('ready', 'unavailable')), detail TEXT)"
    )
    cur.execute(
        "INSERT INTO corpus_sources_expanded "
        "SELECT source_key, kind, root, revision, indexed_at, status, detail FROM corpus_sources"
    )
    cur.execute("DROP TABLE corpus_sources")
    cur.execute("ALTER TABLE corpus_sources_expanded RENAME TO corpus_sources")


class CorpusRepository(Repository):
    """Owns corpus_sources, corpus_chunks, corpus_occurrences."""

    @writes
    def replace_corpus_source(
        self, source: CorpusSourceRecord, chunks: list[CorpusChunkInput]
    ) -> bool:
        if not source.source_key.strip():
            raise EmptyField("source_key")
        if source.status != "ready":
            raise EmptyField("ready corpus source")

        with self.db.connection() as conn, conn.cursor() as cur:
            if source.kind == "code":
                ensure_code_source_kind(cur)
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'corpus_chunks_fts'"
            )
            first_fts = cur.fetchone() is None
            cur.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS corpus_chunks_fts USING "
                "fts5(content, content='corpus_chunks', content_rowid='seq', "
                "tokenize='unicode61 remove_diacritics 2')"
            )
            cur.execute(
                "CREATE TRIGGER IF NOT EXISTS corpus_chunks_fts_insert "
                "AFTER INSERT ON corpus_chunks BEGIN "
                "INSERT INTO corpus_chunks_fts(rowid, content) VALUES (new.seq, new.content); END"
            )
            cur.execute(
                "CREATE TRIGGER IF NOT EXISTS corpus_chunks_fts_delete "
                "AFTER DELETE ON corpus_chunks BEGIN "
                "INSERT INTO corpus_chunks_fts(corpus_chunks_fts, rowid, content) "
                "VALUES ('delete', old.seq, old.content); END"
            )
            cur.execute(
                "CREATE TRIGGER IF NOT EXISTS corpus_chunks_fts_update "
                "AFTER UPDATE ON corpus_chunks BEGIN "
                "INSERT INTO corpus_chunks_fts(corpus_chunks_fts, rowid, content) "
                "VALUES ('delete', old.seq, old.content); "
                "INSERT INTO corpus_chunks_fts(rowid, content) "
                "VALUES (new.seq, new.content); END"
            )
            if first_fts:
                cur.execute(
                    "INSERT INTO corpus_chunks_fts(corpus_chunks_fts) VALUES ('rebuild')"
                )
            cur.execute(
                "SELECT 1 FROM corpus_sources WHERE source_key = ? AND revision = ? AND status = 'ready'",
                (source.source_key, source.revision),
            )
            if cur.fetchone() is not None:
                return False

            cur.execute(
                "DELETE FROM corpus_occurrences WHERE source_key = ?",
                (source.source_key,),
            )

            for chunk in chunks:
                expected_id = f"ctx-{chunk.content_hash}"
                cur.execute(
                    """
                    INSERT INTO corpus_chunks (seq, id, content_hash, content)
                    VALUES (
                        (SELECT COALESCE(MAX(seq), 0) + 1 FROM corpus_chunks),
                        ?, ?, ?
                    )
                    ON CONFLICT (content_hash) DO UPDATE SET content = EXCLUDED.content
                    """,
                    (expected_id, chunk.content_hash, chunk.content),
                )

                occ = chunk.occurrence
                occ_key = occurrence_key(
                    chunk.content_hash,
                    occ.source_key,
                    occ.path,
                    occ.start_line,
                    occ.end_line,
                )
                cur.execute(
                    """
                    INSERT INTO corpus_occurrences (
                        occurrence_key, content_hash, source_key, project_id, path,
                        start_line, end_line, revision, provenance
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (occurrence_key) DO UPDATE SET
                        project_id = EXCLUDED.project_id,
                        path = EXCLUDED.path,
                        start_line = EXCLUDED.start_line,
                        end_line = EXCLUDED.end_line,
                        revision = EXCLUDED.revision,
                        provenance = EXCLUDED.provenance
                    """,
                    (
                        occ_key,
                        chunk.content_hash,
                        occ.source_key,
                        occ.project_id,
                        occ.path,
                        occ.start_line,
                        occ.end_line,
                        occ.revision,
                        occ.provenance,
                    ),
                )

            cur.execute(
                """
                DELETE FROM corpus_chunks
                WHERE content_hash NOT IN (SELECT DISTINCT content_hash FROM corpus_occurrences)
                """
            )

            cur.execute(
                """
                INSERT INTO corpus_sources (source_key, kind, root, revision, indexed_at, status, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_key) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    root = EXCLUDED.root,
                    revision = EXCLUDED.revision,
                    indexed_at = EXCLUDED.indexed_at,
                    status = EXCLUDED.status,
                    detail = EXCLUDED.detail
                """,
                (
                    source.source_key,
                    source.kind,
                    source.root,
                    source.revision,
                    source.indexed_at,
                    source.status,
                    source.detail,
                ),
            )
            return True

    @writes
    def set_corpus_source_unavailable(self, source: CorpusSourceRecord) -> None:
        if not source.source_key.strip():
            raise EmptyField("source_key")
        if source.status != "unavailable":
            raise EmptyField("unavailable corpus source")

        with self.db.connection() as conn, conn.cursor() as cur:
            if source.kind == "code":
                ensure_code_source_kind(cur)
            cur.execute(
                """
                INSERT INTO corpus_sources (source_key, kind, root, revision, indexed_at, status, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_key) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    root = EXCLUDED.root,
                    revision = EXCLUDED.revision,
                    indexed_at = EXCLUDED.indexed_at,
                    status = EXCLUDED.status,
                    detail = EXCLUDED.detail
                """,
                (
                    source.source_key,
                    source.kind,
                    source.root,
                    source.revision,
                    source.indexed_at,
                    source.status,
                    source.detail,
                ),
            )

    def list_corpus_sources(self) -> list[CorpusSourceRecord]:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT source_key, kind, root, revision, indexed_at, status, detail FROM corpus_sources ORDER BY source_key ASC"
            )
            return [
                CorpusSourceRecord(
                    source_key=str(r[0]),
                    kind=str(r[1]),
                    root=str(r[2]),
                    revision=str(r[3]),
                    indexed_at=str(r[4]),
                    status=str(r[5]),
                    detail=str(r[6]) if r[6] is not None else None,
                )
                for r in cur.fetchall()
            ]

    def corpus_occurrence_counts(self) -> dict[str, int]:
        """How many occurrences each indexed source holds, keyed by `source_key`.

        F5: `corpus_chunk_count()` answers for the ENTIRE store, and a registered
        source with ZERO occurrences was indistinguishable from a full one — the
        only source this store ever registered was a deleted QA directory, and
        the status read as presence for a week. A source absent from the map
        has zero: there is no row to count.
        """
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT source_key, COUNT(*) FROM corpus_occurrences"
                " GROUP BY source_key"
            )
            return {str(r[0]): int(r[1]) for r in cur.fetchall()}

    def corpus_chunk_count(self) -> int:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM corpus_chunks")
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def search_corpus_fts(
        self,
        terms: list[str],
        project_id: str | None = None,
        kinds: list[str] | None = None,
        limit: int = 12,
        project_prefix: str | None = None,
    ) -> list[CorpusSearchHit]:
        if not kinds or limit == 0:
            return []
        all_words = set().union(*(lexical_words(term) for term in terms))
        meaning_words = all_words - QUESTION_FILLER or all_words
        meaning_counts = Counter(
            word
            for term in terms
            for word in lexical_tokens(term)
            if word in meaning_words
        )
        concepts = {
            word: {word, *PT_CONTEXT_TERMS.get(word, ())} for word in meaning_words
        }
        for word, variants in concepts.items():
            if len(word) > 4 and word.endswith("e"):
                variants.add(f"{word}s")
        query_words = set().union(*concepts.values())
        if not query_words:
            return []
        queries = [{word} for word in sorted(query_words)]

        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'corpus_chunks_fts'"
            )
            indexed = cur.fetchone() is not None

        query = """
            SELECT c.seq, c.id, c.content_hash, c.content,
                   o.source_key, o.project_id, o.path, o.start_line,
                   o.end_line, o.revision, o.provenance, s.kind,
        """
        if indexed:
            query += " -bm25(corpus_chunks_fts) FROM corpus_chunks_fts JOIN corpus_chunks c ON c.seq = corpus_chunks_fts.rowid "
            params: list[Any] = []
        else:
            query += " 0.0 FROM corpus_chunks c "
            params = []
        query += """
            JOIN corpus_occurrences o ON o.content_hash = c.content_hash
            JOIN corpus_sources s ON s.source_key = o.source_key
            WHERE s.status = 'ready' AND s.kind IN (SELECT value FROM json_each(?))
        """
        params.append(kinds)
        if indexed:
            query += " AND corpus_chunks_fts MATCH ?"
            params.append(" OR ".join(f'"{word}"' for word in sorted(query_words)))
        if project_id is not None:
            query += " AND (o.project_id IS NULL OR o.project_id = ?)"
            params.append(project_id)
        elif project_prefix is not None:
            query += (
                " AND (o.project_id IS NULL OR o.project_id = ? OR o.project_id GLOB ?)"
            )
            params.extend((project_prefix, glob_subtree_pattern(project_prefix)))
        query += " ORDER BY c.seq, o.source_key, o.path, o.start_line"

        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

        contents: dict[str, str] = {}
        eligible: dict[str, tuple[int, str, str, list[CorpusOccurrence]]] = {}
        indexed_scores: dict[str, float] = {}
        relevance_bonus: dict[str, float] = {}
        historical: dict[str, bool] = {}
        project_name = project_id or project_prefix
        for row in rows:
            seq, cid, hash_val, content = (
                int(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
            )
            contents[cid] = content
            indexed_scores[cid] = float(row[12])
            content_words = lexical_words(content)
            if not query_words.intersection(content_words):
                continue
            occurrence = CorpusOccurrence(
                source_key=str(row[4]),
                project_id=str(row[5]) if row[5] is not None else None,
                path=str(row[6]),
                start_line=int(row[7]) if row[7] is not None else None,
                end_line=int(row[8]) if row[8] is not None else None,
                revision=str(row[9]),
                provenance=str(row[10]),
            )
            if cid not in eligible:
                eligible[cid] = (seq, hash_val, content, [])
            is_history = row[11] == "git"
            historical[cid] = historical.get(cid, True) and is_history
            path = str(row[6])
            matches = path_matches(path, meaning_counts)
            role = source_role(path)
            if path == "README.md" and not (
                (project_name is not None and project_name in meaning_words)
                or meaning_words.intersection({"workspace", "monorepo", "repository"})
            ):
                role = 0.2
            elif path.endswith("/README.md") and matches == 0:
                role = 2.0
            bonus = role + 3.0 * matches
            relevance_bonus[cid] = max(relevance_bonus.get(cid, -10.0), bonus)
            eligible[cid][3].append(occurrence)

        scores = (
            indexed_scores
            if indexed
            else bm25_scores(contents, queries, list(eligible))
        )
        hits: list[tuple[int, CorpusSearchHit]] = []
        for cid, (seq, hash_val, content, occs) in eligible.items():
            hit = CorpusSearchHit(
                id=cid,
                content_hash=hash_val,
                content=content,
                score=scores[cid],
                occurrences=occs,
            )
            hits.append((seq, hit))

        # Current-file location helps disambiguate a named component, while
        # historical commit metadata remains a fallback for an absent file.
        hits.sort(
            key=lambda item: (
                historical[item[1].id],
                -(item[1].score + relevance_bonus[item[1].id]),
                item[0],
            )
        )
        return [h[1] for h in hits[:limit]]

    @writes
    def retire_corpus_root(self, root: str) -> int:
        """Remove one former checkout's sources and occurrences in one transaction."""
        if not root.strip():
            raise EmptyField("root")
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM corpus_sources WHERE root = ?", (root,))
            row = cur.fetchone()
            count = int(row[0]) if row else 0
            cur.execute(
                "DELETE FROM corpus_occurrences WHERE source_key IN "
                "(SELECT source_key FROM corpus_sources WHERE root = ?)",
                (root,),
            )
            cur.execute("DELETE FROM corpus_sources WHERE root = ?", (root,))
            cur.execute(
                "DELETE FROM corpus_chunks WHERE content_hash NOT IN "
                "(SELECT content_hash FROM corpus_occurrences)"
            )
            return count
