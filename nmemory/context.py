"""Unified context substrate for nMEMORY: explicit indexing and read path."""

from __future__ import annotations

import ast
import hashlib
import logging
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import nott_database
from nott_database import DatabaseError

from . import audit_actions
from .bridge import split_markdown
from .capsule import format_rfc3339, headline_of, sha256_hex
from .context_readers import (
    MAX_FILE_BYTES,
    MAX_FILES,
    AdapterHit,
    ContextBoundError,
    ContextGitError,
    ContextIndexError,
    ContextIoError,
    ContextPathError,
    ContextProvenance,
    _format_io_error,
    normalize_chunk,
    query_adapter,
    repository_head,
    run_git,
    safe_relative,
)
from .embedding import embedding_key_variable
from .lexical import lexical_words
from .project import in_project_subtree
from .repositories.capsules import StoredCapsule, Tombstoned
from .repositories.corpus import (
    CorpusChunkInput,
    CorpusOccurrence,
    CorpusSearchHit,
    CorpusSourceRecord,
)
from .repositories.embeddings import SupersededSpace
from .retrieve import RRF_K, decay_factor, query_vector, vector_lane_note
from .space import EmbeddingError, SpaceFingerprint
from .store import Store

logger = logging.getLogger(__name__)
MAX_TOTAL_BYTES: int = 64 * 1024 * 1024
MAX_CHUNKS: int = 50_000


@dataclass
class PreparedSource:
    record: CorpusSourceRecord
    chunks: list[CorpusChunkInput]
    skipped: int = 0
    failure: str | None = None


def strip_rustdoc(raw: str) -> str | None:
    if raw.startswith(("///", "//!")):
        body = raw[3:]
        return body.removeprefix(" ")
    if raw.startswith(("/**", "/*!")):
        if not raw.endswith("*/"):
            return None
        body = raw[3:-2]
        lines: list[str] = []
        for line in body.splitlines():
            s = line.lstrip()
            if s.startswith("*"):
                s = s[1:].lstrip()
            lines.append(s)
        return "\n".join(lines)
    return None


def rustdoc_chunks(content: str) -> list[tuple[int, int, str]]:
    lines = content.splitlines()
    raw_comments: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith(("///", "//!")):
            doc = strip_rustdoc(stripped)
            if doc is not None:
                start_l = i + 1
                doc_lines = [doc]
                j = i + 1
                while j < len(lines):
                    next_stripped = lines[j].strip()
                    if next_stripped.startswith(("///", "//!")):
                        d = strip_rustdoc(next_stripped)
                        if d is not None:
                            doc_lines.append(d)
                            j += 1
                            continue
                    break
                end_l = j
                raw_comments.append((start_l, end_l, "\n".join(doc_lines)))
                i = j
                continue
        elif stripped.startswith(("/**", "/*!")):
            # find closing */
            start_l = i + 1
            j = i
            closed = False
            while j < len(lines):
                if "*/" in lines[j]:
                    closed = True
                    break
                j += 1
            if closed:
                block_raw = "\n".join(lines[i : j + 1])
                doc = strip_rustdoc(block_raw)
                if doc is not None:
                    raw_comments.append((start_l, j + 1, doc))
                i = j + 1
                continue
        i += 1

    # Group contiguous comments
    grouped: list[list[Any]] = []
    for start, end, text in raw_comments:
        if grouped and start <= grouped[-1][1] + 1:
            grouped[-1][1] = end
            grouped[-1][2] = grouped[-1][2] + "\n" + text
        else:
            grouped.append([start, end, text])

    result: list[tuple[int, int, str]] = []
    for start, end, text in grouped:
        norm = normalize_chunk(text)
        if norm:
            result.append((start, end, norm))
    return result


def code_documentation_chunks(path: str, content: str) -> list[tuple[int, int, str]]:
    """Index tracked code's explanations without flooding search with every body line."""
    chunks: list[tuple[int, int, str]] = []
    if path.endswith(".py"):
        try:
            tree = ast.parse(content, filename=path)
        except SyntaxError as error:
            raise ContextIndexError(
                f"tracked Python source {path} cannot parse"
            ) from error
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            doc = ast.get_docstring(node)
            if not doc:
                continue
            first = node.body[0]
            start = getattr(first, "lineno", 1)
            name = "module" if isinstance(node, ast.Module) else node.name
            for offset, text in split_markdown(f"{name}\n{doc}"):
                first_line = start + offset - 1
                chunks.append(
                    (first_line, first_line + len(text.splitlines()) - 1, text)
                )
        return chunks

    shell_start: int | None = None
    block: list[str] = []
    for number, line in enumerate(content.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            if shell_start is None:
                shell_start = number
            block.append(stripped.removeprefix("#").lstrip())
        elif block:
            if shell_start is not None:
                for offset, text in split_markdown("\n".join(block)):
                    first_line = shell_start + offset - 1
                    chunks.append(
                        (first_line, first_line + len(text.splitlines()) - 1, text)
                    )
            shell_start = None
            block = []
    if block and shell_start is not None:
        for offset, text in split_markdown("\n".join(block)):
            first_line = shell_start + offset - 1
            chunks.append((first_line, first_line + len(text.splitlines()) - 1, text))
    return chunks


def file_revision(head: str, files: list[tuple[str, str]]) -> str:
    h = hashlib.sha256()
    h.update(head.encode("utf-8"))
    for path, content in sorted(files, key=lambda f: f[0]):
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
        h.update(content.encode("utf-8"))
        h.update(b"\x00")
    return f"{head}:{h.hexdigest()}"


def push_chunk(
    chunks: list[CorpusChunkInput],
    source_key: str,
    project_id: str | None,
    path: str,
    start_line: int | None,
    end_line: int | None,
    revision: str,
    provenance: str,
    content: str,
) -> None:
    if not content:
        return
    if len(chunks) >= MAX_CHUNKS:
        raise ContextBoundError("chunk-count")

    content_hash = sha256_hex(content.encode("utf-8"))
    chunks.append(
        CorpusChunkInput(
            content_hash=content_hash,
            content=content,
            occurrence=CorpusOccurrence(
                source_key=source_key,
                project_id=project_id,
                path=path,
                start_line=start_line,
                end_line=end_line,
                revision=revision,
                provenance=provenance,
            ),
        )
    )


def prepare_files(
    root: Path | str,
    project_id: str | None,
    now: datetime,
    progress: dict[str, PreparedSource] | None = None,
) -> tuple[PreparedSource, PreparedSource, PreparedSource]:
    root_path = Path(root).resolve()
    head = repository_head(root_path)
    progress = {} if progress is None else progress
    for kind in ("markdown", "code", "rustdoc"):
        progress[kind] = PreparedSource(
            CorpusSourceRecord(
                f"{kind}:{root_path}",
                kind,
                str(root_path),
                head,
                format_rfc3339(now),
                "ready",
            ),
            [],
        )

    listed = run_git(
        root_path,
        [
            "ls-files",
            "-z",
            "--",
            "*.md",
            "*.markdown",
            "*.py",
            "*.sh",
            "*.bash",
            "*.rs",
        ],
        "ls-files",
    )
    raw_paths = [p.decode("utf-8") for p in listed.split(b"\x00") if p]
    if len(raw_paths) > MAX_FILES:
        raise ContextBoundError("file-count")

    markdown_raw: list[tuple[str, str]] = []
    code_raw: list[tuple[str, str]] = []
    rustdoc_raw: list[tuple[str, str]] = []
    total_bytes = 0
    markdown_skipped = 0
    code_skipped = 0
    rustdoc_skipped = 0

    for relative in raw_paths:
        try:
            rel = safe_relative(relative)
        except ContextPathError:
            continue
        p = root_path / rel
        is_md = p.suffix in (".md", ".markdown")
        is_code = p.suffix in (".py", ".sh", ".bash")
        if is_code and (
            "tests" in rel.parts
            or p.name.startswith(("test_", "test-"))
            or p.name == "conftest.py"
        ):
            code_skipped += 1
            progress["code"].skipped = code_skipped
            continue
        try:
            st = os.lstat(p)
        except OSError as e:
            raise ContextIoError(relative, _format_io_error(e)) from e

        if not p.is_file() or os.path.islink(p):
            if is_md:
                markdown_skipped += 1
                progress["markdown"].skipped = markdown_skipped
            elif is_code:
                code_skipped += 1
                progress["code"].skipped = code_skipped
            else:
                rustdoc_skipped += 1
                progress["rustdoc"].skipped = rustdoc_skipped
            continue

        try:
            canonical = p.resolve()
        except OSError as e:
            raise ContextIoError(relative, _format_io_error(e)) from e

        if not canonical.is_relative_to(root_path):
            raise ContextPathError(relative)

        if st.st_size > MAX_FILE_BYTES:
            if is_md:
                markdown_skipped += 1
                progress["markdown"].skipped = markdown_skipped
            elif is_code:
                code_skipped += 1
                progress["code"].skipped = code_skipped
            else:
                rustdoc_skipped += 1
                progress["rustdoc"].skipped = rustdoc_skipped
            continue

        total_bytes += st.st_size
        if total_bytes > MAX_TOTAL_BYTES:
            raise ContextBoundError("total-bytes")

        try:
            # newline="" disables Python's universal-newline translation. The pin reads these
            # bytes with fs::read_to_string, which translates nothing, so a CRLF file that
            # Python silently normalised to LF hashed to a different corpus revision than the
            # oracle's over the identical tree — same chunk count, different digest.
            content = p.read_text(encoding="utf-8", newline="")
        except OSError as e:
            raise ContextIoError(relative, _format_io_error(e)) from e

        if is_md:
            markdown_raw.append((relative, content))
        elif is_code:
            code_raw.append((relative, content))
        else:
            rustdoc_raw.append((relative, content))

    root_text = str(root_path)

    # Markdown
    md_source_key = f"markdown:{root_text}"
    md_revision = file_revision(head, markdown_raw)
    md_prepared = progress["markdown"]
    md_prepared.record = replace(md_prepared.record, revision=md_revision)
    md_chunks = md_prepared.chunks
    for path_str, content in markdown_raw:
        pieces = [
            (start, start + max(1, len(chunk.splitlines())) - 1, chunk)
            for start, chunk in split_markdown(content)
        ]
        for start, end, chunk in pieces:
            norm = normalize_chunk(chunk)
            push_chunk(
                md_chunks,
                md_source_key,
                project_id,
                path_str,
                start,
                end,
                md_revision,
                "tracked-markdown",
                norm,
            )

    # Docstrings and shell comments are documentation from tracked code, with
    # their own source kind so a Markdown-only query stays literal.
    code_source_key = f"code:{root_text}"
    code_revision = file_revision(head, code_raw)
    code_prepared = progress["code"]
    code_prepared.record = replace(code_prepared.record, revision=code_revision)
    for path_str, content in code_raw:
        try:
            code_chunks = code_documentation_chunks(path_str, content)
        except ContextIndexError as error:
            code_prepared.failure = str(error)
            break
        for start, end, chunk in code_chunks:
            norm = normalize_chunk(chunk)
            push_chunk(
                code_prepared.chunks,
                code_source_key,
                project_id,
                path_str,
                start,
                end,
                code_revision,
                "tracked-code",
                norm,
            )

    # Rustdoc
    rd_source_key = f"rustdoc:{root_text}"
    rd_revision = file_revision(head, rustdoc_raw)
    rd_prepared = progress["rustdoc"]
    rd_prepared.record = replace(rd_prepared.record, revision=rd_revision)
    rd_chunks = rd_prepared.chunks
    for path_str, content in rustdoc_raw:
        for start, end, doc in rustdoc_chunks(content):
            push_chunk(
                rd_chunks,
                rd_source_key,
                project_id,
                path_str,
                start,
                end,
                rd_revision,
                "tree-sitter-rustdoc",
                doc,
            )

    return md_prepared, code_prepared, rd_prepared


def prepare_git(
    root: Path | str,
    project_id: str | None,
    now: datetime,
    progress: dict[str, PreparedSource] | None = None,
) -> PreparedSource:
    root_path = Path(root).resolve()
    head = repository_head(root_path)
    partial = PreparedSource(
        CorpusSourceRecord(
            f"git:{root_path}",
            "git",
            str(root_path),
            head,
            format_rfc3339(now),
            "ready",
        ),
        [],
    )
    if progress is not None:
        progress["git"] = partial

    output = run_git(
        root_path,
        [
            "log",
            "--diff-merges=first-parent",
            "--name-only",
            "-z",
            "--format=%x1e%H%x1f%aI%x1f%an%x1f%s%x1f%b%x1f",
        ],
        "log",
    )
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ContextGitError("log", "output was not UTF-8") from e

    root_text = str(root_path)
    source_key = f"git:{root_text}"
    chunks = partial.chunks

    for record in text.split("\x1e"):
        if not record.strip():
            continue
        fields = record.split("\x1f", 5)
        if len(fields) < 6:
            raise ContextGitError("log", "commit metadata had an unexpected shape")

        c_sha, c_date, c_author, c_subject = fields[0], fields[1], fields[2], fields[3]
        content_lines = [
            f"commit {c_sha}",
            f"date {c_date}",
            f"author {c_author}",
            f"subject {c_subject}",
        ]
        body = normalize_chunk(fields[4])
        if body:
            content_lines.append(f"body {body}")

        paths_str = fields[5].lstrip("\x00\n")
        for p in paths_str.split("\x00"):
            if not p:
                continue
            safe_relative(p)
            if any(ord(c) < 32 for c in p):
                raise ContextPathError(p)
            content_lines.append(f"path {p}")

        chunk_content = normalize_chunk("\n".join(content_lines))
        push_chunk(
            chunks,
            source_key,
            project_id,
            f"git:{c_sha}",
            None,
            None,
            head,
            "git-commit-metadata",
            chunk_content,
        )

    return partial


# `RRF_K` and `decay_factor` live in `retrieve.py`: there were TWO identical constants
# and two rankers, and the one that matters is this one — of the 2613 queries recorded in
# `read_traffic` on 2026-09-17, `context` accounted for 1126 (9793 capsules served) and
# `retrieve` accounted for 15. A local copy makes divergence invisible.
DEFAULT_LIMIT: int = 12
MAX_LIMIT: int = 50
DEFAULT_TOKEN_BUDGET: int = 2000
MAX_EXCERPT_CHARS: int = 800

OBSERVATIONAL_CONTEXT_SOURCES = ("memory", "markdown", "code", "git", "rustdoc")
ALL_CONTEXT_SOURCES = [*OBSERVATIONAL_CONTEXT_SOURCES, "codegraph", "jbcontext"]
ALL_INDEX_SOURCES = ["markdown", "code", "git", "rustdoc"]


def same_context_location(left: ContextProvenance, right: ContextProvenance) -> bool:
    if (
        left.provider == "git"
        and right.provider == "git"
        and left.address == right.address
    ):
        return True
    if (
        left.address != right.address
        or left.revision is None
        or left.revision != right.revision
    ):
        return False
    if (
        left.start_line is not None
        and left.end_line is not None
        and right.start_line is not None
        and right.end_line is not None
    ):
        return left.start_line <= right.end_line and right.start_line <= left.end_line
    return False


@dataclass
class GroupedCandidate:
    id: str = ""
    handle: str | None = None
    content: str = ""
    kind: str | None = None
    providers: set[str] = field(default_factory=set)
    provenance: list[ContextProvenance] = field(default_factory=list)
    instruction_taint: bool = False
    score: float = 0.0
    score_explanation: list[str] = field(default_factory=list)
    #: Confidence and creation time of the capsule, when one exists. `None` for a
    #: corpus candidate (markdown, git, rustdoc, codegraph, jbcontext): it
    #: has no capsule, hence no confidence to weigh, and keeps factor 1.
    confidence: float | None = None
    born: datetime | None = None
    #: The factor the ranker applies to the RRF sum. Kept out of
    #: `score_explanation` ON PURPOSE: `matched_by` calls `split(":")` on
    #: these entries (`context.py`, below) and would treat `standing:x` as
    #: a lane name.
    weight: float = 1.0

    def add(
        self,
        cid: str,
        handle: str | None,
        content: str,
        provider: str,
        provenance: list[ContextProvenance],
        instruction_taint: bool,
        lane: str,
        rank: int,
        confidence: float | None = None,
        born: datetime | None = None,
    ) -> None:
        if not self.id or (provider == "memory" and "memory" not in self.providers):
            self.id = cid
            self.handle = handle
            self.content = content
            # Confidence and `born` come from the SAME capsule as the id, not from the last
            # `add`: the group is indexed by normalized content sha256,
            # so two identical capsules land here, and last-wins caused the group
            # to end up with the id of one and the weight of the other — while the verification
            # clock is read BY `cand.id`. Which weight was served was decided
            # by FTS order rather than the store (independent review finding,
            # `fugu-ultra`, 2026-09-17).
            self.confidence = confidence
            self.born = born
        self.providers.add(provider)
        for item in provenance:
            if item not in self.provenance:
                self.provenance.append(item)
        self.instruction_taint |= instruction_taint
        self.score += 1.0 / (RRF_K + float(rank))
        self.score_explanation.append(f"{lane}:rank={rank}")

    def absorb(self, other: GroupedCandidate) -> None:
        if not self.id or (
            "memory" not in self.providers and "memory" in other.providers
        ):
            self.id = other.id
            self.handle = other.handle
            self.content = other.content
            self.kind = other.kind
            # Same reason as `add`: taking the other's id without taking its weight
            # left the group with the id of one capsule and the confidence of another.
            self.confidence = other.confidence
            self.born = other.born
        elif self.kind is None:
            self.kind = other.kind
        self.providers.update(other.providers)
        for item in other.provenance:
            if item not in self.provenance:
                self.provenance.append(item)
        self.instruction_taint |= other.instruction_taint
        if self.confidence is None:
            self.confidence = other.confidence
        if self.born is None:
            self.born = other.born
        self.score += other.score
        self.score_explanation.extend(other.score_explanation)


def take_grouped_candidate(
    grouped: dict[str, GroupedCandidate],
    content_hash: str,
    provenance: list[ContextProvenance],
) -> tuple[str, GroupedCandidate]:
    matching_keys: list[str] = []
    for h, cand in grouped.items():
        if h == content_hash or any(
            same_context_location(stored, incoming)
            for stored in cand.provenance
            for incoming in provenance
        ):
            matching_keys.append(h)
    if content_hash not in matching_keys:
        matching_keys.append(content_hash)
    matching_keys.sort()

    primary_key = matching_keys[0]
    candidate = grouped.pop(primary_key, GroupedCandidate())
    for other_key in matching_keys[1:]:
        if other_key in grouped:
            candidate.absorb(grouped.pop(other_key))
    return primary_key, candidate


def index_context(
    store: Store,
    root: Path | str | None,
    project_id: str | None,
    sources: list[str] | None,
    now: datetime,
    actor: str = audit_actions.INDEX,
    retire_root: Path | str | None = None,
) -> dict[str, Any]:
    root_path = Path(root or os.getcwd()).resolve()
    selected = sorted(set(sources if sources is not None else ALL_INDEX_SOURCES))
    old_root = Path(retire_root).resolve() if retire_root is not None else None
    if old_root is not None and (
        old_root == root_path or selected != sorted(ALL_INDEX_SOURCES)
    ):
        raise ValueError("retire_root requires a distinct root and a full index")

    prepared: dict[str, PreparedSource] = {}
    progress: dict[str, PreparedSource] = {}
    failures: dict[str, str] = {}

    if any(source in selected for source in ("markdown", "code", "rustdoc")):
        try:
            md_prep, code_prep, rd_prep = prepare_files(
                root_path, project_id, now, progress
            )
            if "markdown" in selected:
                prepared["markdown"] = md_prep
            if "code" in selected:
                if code_prep.failure is None:
                    prepared["code"] = code_prep
                else:
                    failures["code"] = code_prep.failure
            if "rustdoc" in selected:
                prepared["rustdoc"] = rd_prep
        except (ContextIndexError, OSError) as e:
            if "markdown" in selected:
                failures["markdown"] = str(e)
            if "code" in selected:
                failures["code"] = str(e)
            if "rustdoc" in selected:
                failures["rustdoc"] = str(e)

    if "git" in selected:
        try:
            prepared["git"] = prepare_git(root_path, project_id, now, progress)
        except (ContextIndexError, OSError) as e:
            failures["git"] = str(e)

    rows: list[dict[str, Any]] = []
    root_text = str(root_path)

    for src in selected:
        if src in prepared:
            p = prepared[src]
            occ_count = len(p.chunks)
            unique_chunk_count = len({c.content_hash for c in p.chunks})
            changed = store.corpus.replace_corpus_source(p.record, p.chunks)
            if changed:
                store.audit.append_audit(
                    actor,
                    audit_actions.INDEX,
                    p.record.source_key,
                    "durable context source refreshed",
                    now,
                )
            rows.append(
                {
                    "source": src,
                    "status": "ready",
                    "revision": p.record.revision,
                    "chunks": unique_chunk_count,
                    "occurrences": occ_count,
                    "skipped": p.skipped,
                    "changed": changed,
                }
            )
        else:
            err_msg = failures.get(src, "provider produced no result")
            partial = progress.get(src)
            rec = CorpusSourceRecord(
                source_key=f"{src}:{root_text}",
                kind=src,
                root=root_text,
                revision=partial.record.revision if partial else "unknown",
                indexed_at=format_rfc3339(now),
                status="unavailable",
                detail=err_msg,
            )
            store.corpus.set_corpus_source_unavailable(rec)
            store.audit.append_audit(
                actor,
                audit_actions.INDEX,
                rec.source_key,
                "durable context source unavailable",
                now,
            )
            rows.append(
                {
                    "source": src,
                    "status": "unavailable",
                    "revision": partial.record.revision if partial else None,
                    "chunks": len({c.content_hash for c in partial.chunks})
                    if partial
                    else 0,
                    "occurrences": len(partial.chunks) if partial else 0,
                    "skipped": partial.skipped if partial else 0,
                    "partial": partial is not None,
                    "changed": False,
                    "detail": err_msg,
                }
            )

    retired_sources = 0
    if old_root is not None and not failures and len(prepared) == len(selected):
        retired_sources = store.corpus.retire_corpus_root(str(old_root))
        if retired_sources:
            store.audit.append_audit(
                actor,
                audit_actions.INDEX,
                str(old_root),
                "former context root retired",
                now,
            )

    rows.sort(key=lambda r: r["source"])
    return {
        "label": "ADVISORY_NOT_AUTHORITY",
        "framing": "DATA",
        "root": root_text,
        "sources": rows,
        "unique_chunks": store.corpus.corpus_chunk_count(),
        "retired_sources": retired_sources,
    }


def validate_context_request(
    query: str, project_id: str | None, project_prefix: str | None, limit: int
) -> None:
    """Refuse a request no fence can serve, with the typed ValueError."""
    if not any(c.isalnum() for c in query):
        raise ValueError("query must contain at least one alphanumeric character")
    if project_id is not None and not project_id.strip():
        raise ValueError("project_id must be non-empty when supplied")
    if project_prefix is not None and not project_prefix.strip():
        raise ValueError("project_prefix must be non-empty when supplied")
    if project_id is not None and project_prefix is not None:
        raise ValueError(
            "project_id and project_prefix are two different fences; pick one"
        )
    if limit > MAX_LIMIT or limit < 0:
        raise ValueError(f"limit must be within 0..={MAX_LIMIT}")


def _add_memory_candidate(
    store: Store,
    grouped: dict[str, GroupedCandidate],
    sc: StoredCapsule,
    lane: str,
    rank: int,
) -> bool:
    """Group one live capsule under its normalized content hash; False when not live.

    Live means tier active, not superseded and not falsified.
    """
    if (
        store.tiers.get_tier(sc.id) != "active"
        or store.relations.is_superseded(sc.id)
        or store.relations.is_falsified(sc.id)
    ):
        return False
    content = normalize_chunk(sc.capsule.content)
    h = sha256_hex(content.encode("utf-8"))
    handle = store.capsules.handle_of(sc.id) or sc.id
    cls = store.classifications.get_classification(sc.id)
    cand = grouped.setdefault(h, GroupedCandidate())
    cand.kind = cls.kind if cls else "capsule"
    cand.add(
        cid=sc.id,
        handle=handle,
        content=content,
        provider="memory",
        provenance=[
            ContextProvenance(provider="memory", address=sc.capsule.provenance.anchor)
        ],
        instruction_taint=sc.capsule.instruction_taint,
        lane=lane,
        rank=rank,
        confidence=sc.capsule.confidence,
        born=sc.capsule.freshness.valid_from,
    )
    return True


def _semantic_matches(
    store: Store,
    embeddings: Any,
    query: str,
    pool: int,
    project_id: str | None,
    project_prefix: str | None,
) -> tuple[list[tuple[str, float]], str | None]:
    """The semantic lane's cosine matches, or none and why it was skipped: the
    shared vector-lane gate, or the error that stopped it."""
    note = vector_lane_note(store, embeddings)
    if note is not None:
        return [], note
    try:
        q_vec = query_vector(store, embeddings, query)
        return store.embeddings.search_cosine(
            query_vector=q_vec,
            limit=pool,
            project_id=project_id,
            project_prefix=project_prefix,
            space_fingerprint=embeddings.space_fingerprint.as_str(),
        ), None
    except (
        EmbeddingError,
        SupersededSpace,
        DatabaseError,
        nott_database.Error,
        OSError,
    ) as e:
        logger.debug("context semantic lane skipped: %s", e)
        return [], str(e)


def outside_fence(
    capsule_project: str, project_id: str | None, project_prefix: str | None
) -> bool:
    """A semantic match outside the exact project or the project subtree."""
    if project_id is not None and capsule_project != project_id:
        return True
    return project_prefix is not None and not in_project_subtree(
        capsule_project, project_prefix
    )


def _provider_of(source_key: str) -> str:
    return source_key.split(":")[0] if ":" in source_key else "corpus"


def add_corpus_hit(
    grouped: dict[str, GroupedCandidate], hit: CorpusSearchHit, rank: int
) -> int:
    """Group one durable-corpus hit; returns the raw candidates it counts (>= 1)."""
    providers = {_provider_of(occ.source_key) for occ in hit.occurrences}
    prov_list = [
        ContextProvenance(
            provider=_provider_of(occ.source_key),
            address=occ.path,
            start_line=occ.start_line,
            end_line=occ.end_line,
            revision=occ.revision,
        )
        for occ in hit.occurrences
    ]
    primary_provider = min(providers)
    k, cand = take_grouped_candidate(grouped, hit.content_hash, prov_list)
    if cand.kind is None:
        cand.kind = primary_provider
    cand.add(
        cid=hit.id,
        handle=None,
        content=hit.content,
        provider=primary_provider,
        provenance=prov_list,
        instruction_taint=True,
        lane="corpus_lexical",
        rank=rank,
    )
    cand.providers.update(providers)
    grouped[k] = cand
    return max(1, len(hit.occurrences))


def add_adapter_hit(
    grouped: dict[str, GroupedCandidate], src: str, hit: AdapterHit, rank: int
) -> None:
    """Group one live adapter hit (codegraph, jbcontext) under its normalized hash."""
    norm_text = normalize_chunk(hit.content)
    provs = [hit.provenance]
    k, cand = take_grouped_candidate(
        grouped, sha256_hex(norm_text.encode("utf-8")), provs
    )
    if cand.kind is None:
        cand.kind = src
    cand.add(
        cid=hit.id,
        handle=None,
        content=norm_text,
        provider=src,
        provenance=provs,
        instruction_taint=True,
        lane=src,
        rank=rank,
    )
    grouped[k] = cand


def pick_corpus_sources(
    records: list[CorpusSourceRecord],
    anchor_path: Path,
    occurrence_counts: dict[str, int],
) -> dict[str, CorpusSourceRecord]:
    """The one registered source each `kind` reports.

    The rule is the anchor, not the alphabet: the root the caller is querying
    wins. Without a match, the one with occurrences wins, and the final
    tiebreaker is the same stable order as always.
    """

    def better(a: CorpusSourceRecord, b: CorpusSourceRecord) -> CorpusSourceRecord:
        for candidate in (a, b):
            if Path(candidate.root).resolve() == anchor_path:
                return candidate
        if occurrence_counts.get(b.source_key, 0) > occurrence_counts.get(
            a.source_key, 0
        ):
            return b
        return a

    picked: dict[str, CorpusSourceRecord] = {}
    for source_rec in records:
        held = picked.get(source_rec.kind)
        picked[source_rec.kind] = (
            source_rec if held is None else better(held, source_rec)
        )
    return picked


def durable_source_status(
    src: str, rec: CorpusSourceRecord | None, chunks: int
) -> dict[str, Any]:
    """The status entry of one durable source; reads the checkout's git head.

    F5: an EMPTY index has no freshness. Reporting `unknown_staleness` beside
    an `indexed_at` read as "index exists, freshness unknown" while no index
    existed (a deleted QA directory from 2026-09-05, zero chunks, read as
    presence for a week). `empty_index` comes BEFORE the freshness check: the
    freshness of zero rows is irrelevant.
    """
    if rec is None:
        return {
            "source": src,
            "status": "not_indexed",
            "detail": "run nmemory_index_build",
            "staleness_basis": "git_head",
        }
    curr_head = None
    if chunks == 0:
        stat = "empty_index"
    else:
        try:
            curr_head = repository_head(rec.root)
            indexed_head = rec.revision.split(":")[0]
            if rec.status == "unavailable":
                stat = "unavailable"
            elif curr_head != indexed_head:
                stat = "stale"
            else:
                stat = "ready"
        except ContextIndexError, OSError:
            curr_head = None
            stat = "unknown_staleness"
    entry: dict[str, Any] = {
        "source": src,
        "status": stat,
        "revision": rec.revision,
        "indexed_at": rec.indexed_at,
        # The number that makes the status auditable rather than taken on faith.
        "chunks": chunks,
        "staleness_basis": "git_head",
    }
    if curr_head:
        entry["current_revision"] = curr_head
    if rec.detail:
        entry["detail"] = rec.detail
    return entry


def semantic_status(
    error: str | None, configured: bool, provider: str | None = None
) -> dict[str, str]:
    """The semantic lane's status: the error that skipped it, ready, or unconfigured."""
    if error is not None:
        return {"status": "unavailable_lexical_only", "detail": error}
    if provider is None:
        try:
            provider = SpaceFingerprint.current().provider
        except EmbeddingError as config_error:
            return {"status": "unavailable_lexical_only", "detail": str(config_error)}
    if configured:
        return {"provider": provider, "status": "ready"}
    return {
        "detail": (
            f"{provider} embedding failed (OperatorAction): "
            f"{embedding_key_variable(provider)} is not configured"
        ),
        "status": "unavailable_lexical_only",
    }


def weigh_candidates(
    grouped: dict[str, GroupedCandidate],
    now: datetime,
    verified_at: dict[str, datetime],
    pinned_ids: set[str],
) -> None:
    """Multiply each capsule candidate's RRF sum by its decay.

    THE RANKER, unified with `retrieve.py`: the sum of lane RRF TIMES decay on
    the LAST VERIFICATION clock. Confidence does not enter the order; the
    measurement is in `decay_factor`. A pinned capsule
    does not decay (independent review, `fugu-ultra`, 2026-09-17: measured
    factor 0.0625 instead of 1.0). A corpus candidate has no capsule and keeps
    factor 1.
    """
    for cand in grouped.values():
        if cand.confidence is None or cand.born is None:
            continue
        cand.weight = decay_factor(
            now=now,
            valid_from=cand.born,
            verified_at=verified_at.get(cand.id),
            pinned=cand.id in pinned_ids,
        )
        cand.score *= cand.weight


def render_results(
    ranked: list[GroupedCandidate], limit: int, token_budget: int
) -> list[dict[str, Any]]:
    """The served results: at most `limit`, each skipped when it overflows the budget.

    The cost of one result is its excerpt's UTF-8 bytes plus 160, over 4.
    """
    spent = 0
    results: list[dict[str, Any]] = []
    for index, candidate in enumerate(ranked):
        if len(results) == limit:
            break
        excerpt = (
            candidate.content[:MAX_EXCERPT_CHARS] + "…"
            if len(candidate.content) > MAX_EXCERPT_CHARS
            else candidate.content
        )
        cost = (len(excerpt.encode("utf-8")) + 160) // 4
        if spent + cost > token_budget:
            continue
        spent += cost
        prov_list_dicts = [p.to_dict() for p in candidate.provenance]
        providers_sorted = sorted(candidate.providers)
        primary_source = (
            "memory"
            if "memory" in candidate.providers
            else (providers_sorted[0] if providers_sorted else "unknown")
        )
        evidence: dict[str, Any] = {
            "id": candidate.id,
            "source": primary_source,
            "kind": candidate.kind or primary_source,
            "headline": headline_of(candidate.content),
            "excerpt": excerpt,
            "providers": providers_sorted,
            "matched_by": sorted(
                {
                    entry.split(":")[0]
                    for entry in candidate.score_explanation
                    if ":" in entry
                }
            ),
            "rank": index + 1,
            "provenance": prov_list_dicts,
            "instruction_taint": candidate.instruction_taint,
            "score": candidate.score,
            "score_explanation": candidate.score_explanation,
        }
        if candidate.handle is not None:
            evidence["handle"] = candidate.handle
        if len(candidate.provenance) > 1:
            evidence["also_seen_in"] = prov_list_dicts[1:]
        results.append(evidence)
    return results


def context_scope(
    project_id: str | None, project_prefix: str | None, project_source: str | None
) -> dict[str, Any]:
    """The fence the answer ran under, as the envelope names it."""
    return {
        "project_id": project_id if project_id is not None else project_prefix,
        "match": (
            "exact"
            if project_id is not None
            else ("subtree" if project_prefix is not None else "prefix")
        ),
        "source": project_source
        or ("caller" if project_id is not None else "literal_default"),
    }


def execute_context(
    store: Store,
    query: str,
    project_id: str | None = None,
    sources: list[str] | None = None,
    project_prefix: str | None = None,
    limit: int = DEFAULT_LIMIT,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    anchor_root: Path | str | None = None,
    embeddings: Any = None,
    now: datetime | None = None,
    project_source: str | None = None,
) -> dict[str, Any]:
    validate_context_request(query, project_id, project_prefix, limit)
    # The ranker measures age-since-verification against `now`: a test passing
    # a fixed timestamp must see the ranker use it.
    now_dt = now if now is not None else datetime.now(UTC)
    selected = sorted(set(sources if sources is not None else ALL_CONTEXT_SOURCES))
    anchor_path = Path(anchor_root or os.getcwd()).resolve()
    pool = min(max(limit * 10, DEFAULT_LIMIT), 500)

    grouped: dict[str, GroupedCandidate] = {}
    raw_candidates = 0

    # 1. Recall memory. Each WORD is its own alternative: passing `[query]` made
    # ONE group, an AND over every word, and a twelve-word query returned ZERO
    # (measured 2026-09-21: with the semantic lane down, 80-92% of calls to
    # this surface returned nothing). BM25 ranks the disjunction.
    if "memory" in selected:
        matches = store.capsules.search_fts_scoped(
            sorted(lexical_words(query)),
            project_id=project_id,
            project_prefix=project_prefix,
            limit=pool,
        )
        for rank, (sc, _) in enumerate(matches):
            if _add_memory_candidate(store, grouped, sc, "memory_lexical", rank + 1):
                raw_candidates += 1

    excluded_tombstoned: list[str] = []
    semantic_error: str | None = None
    if (
        "memory" in selected
        and embeddings is not None
        and embeddings.is_configured()
        and store.db is not None
    ):
        v_matches, semantic_error = _semantic_matches(
            store, embeddings, query, pool, project_id, project_prefix
        )
        for rank, (cid, _sim) in enumerate(v_matches):
            try:
                sem_sc = store.capsules.get(cid)
            except Tombstoned:
                excluded_tombstoned.append(cid)
                continue
            if sem_sc is None or outside_fence(
                sem_sc.capsule.scope.project_id, project_id, project_prefix
            ):
                continue
            if _add_memory_candidate(store, grouped, sem_sc, "semantic", rank + 1):
                raw_candidates += 1

    # 2. Recall corpus
    durable_kinds = [s for s in selected if s in ("markdown", "code", "git", "rustdoc")]
    if durable_kinds:
        c_hits = store.corpus.search_corpus_fts(
            [query],
            project_id=project_id,
            kinds=durable_kinds,
            limit=pool,
            project_prefix=project_prefix,
        )
        for rank, hit in enumerate(c_hits):
            raw_candidates += add_corpus_hit(grouped, hit, rank + 1)

    # 3. Source statuses & adapters
    source_statuses: list[dict[str, Any]] = []
    if "memory" in selected:
        source_statuses.append({"source": "memory", "status": "ready"})
    occurrence_counts = store.corpus.corpus_occurrence_counts()
    corpus_records = pick_corpus_sources(
        store.corpus.list_corpus_sources(), anchor_path, occurrence_counts
    )
    for src in ("markdown", "code", "git", "rustdoc"):
        if src in selected:
            rec = corpus_records.get(src)
            chunks = occurrence_counts.get(rec.source_key, 0) if rec else 0
            source_statuses.append(durable_source_status(src, rec, chunks))

    # 4. Live Adapters
    for src in ("codegraph", "jbcontext"):
        if src in selected:
            ad_stat, ad_hits = query_adapter(src, query, pool, anchor_path)
            source_statuses.append(ad_stat)
            for rank, ad_hit in enumerate(ad_hits):
                raw_candidates += 1
                add_adapter_hit(grouped, src, ad_hit, rank + 1)

    # 5. Semantic status
    semantic = semantic_status(
        semantic_error or getattr(embeddings, "config_error", None),
        semantic_error is None and bool(embeddings and embeddings.is_configured()),
        embeddings.space_provider if embeddings is not None else None,
    )
    total_before_limit = len(grouped)

    # One batch query for the entire pool (up to 500), not one per candidate.
    weighed_ids = sorted(
        {c.id for c in grouped.values() if c.confidence is not None and c.id}
    )
    verified_at = store.anchors.last_passing_verification_among(weighed_ids)
    pinned_ids = store.pins.pinned_among(weighed_ids)
    weigh_candidates(grouped, now_dt, verified_at, pinned_ids)
    ranked = sorted(grouped.values(), key=lambda c: (-c.score, c.id))

    source_statuses.sort(key=lambda s: s["source"])
    response: dict[str, Any] = {
        "label": "ADVISORY_NOT_AUTHORITY",
        "framing": "DATA",
        "query": query,
        "scope": context_scope(project_id, project_prefix, project_source),
        "results": render_results(ranked, limit, token_budget),
        "total_before_limit": total_before_limit,
        "exact_duplicates_grouped": max(0, raw_candidates - total_before_limit),
        "sources": source_statuses,
        "semantic": semantic,
    }
    if excluded_tombstoned:
        response["excluded"] = {"tombstoned": excluded_tombstoned}
    return response
