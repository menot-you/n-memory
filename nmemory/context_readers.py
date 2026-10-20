"""The context substrate's readers: git, the checkout and external adapter programs.

Every subprocess and file read the context index performs lives here, with its bounds
and its typed errors. `context` keeps indexing and ranking and calls these readers.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from .anchor import anchor_path_present
from .renamed import env
from .subproc import GIT_QUERY_S, PROBE_S, run_with_timeout

MAX_GIT_OUTPUT: int = 8 * 1024 * 1024
MAX_FILES: int = 10_000
MAX_FILE_BYTES: int = 2 * 1024 * 1024
MAX_ADAPTER_OUTPUT: int = 1024 * 1024
MAX_ADAPTER_LINES: int = 200


class ContextIndexError(Exception):
    pass


class ContextGitError(ContextIndexError):
    def __init__(self, operation: str, detail: str) -> None:
        super().__init__(f"git {operation} failed: {detail}")
        self.operation = operation
        self.detail = detail


class ContextPathError(ContextIndexError):
    def __init__(self, path: str) -> None:
        super().__init__(f"context path {path} is outside the repository root")
        self.path = path


def _format_io_error(e: OSError) -> str:
    if e.errno is not None:
        return f"{os.strerror(e.errno)} (os error {e.errno})"
    return str(e)


class ContextIoError(ContextIndexError):
    def __init__(self, path: str, detail: str) -> None:
        super().__init__(f"context file {path} could not be read: {detail}")
        self.path = path
        self.detail = detail


class ContextBoundError(ContextIndexError):
    def __init__(self, what: str) -> None:
        super().__init__(f"context index exceeded its {what} bound")
        self.what = what


def normalize_chunk(content: str) -> str:
    """Normalize exact chunk identity: LF newlines, trimmed line tails, no outer blank lines,

    and at most one blank separator.
    """
    output: list[str] = []
    blank = False
    unified = content.replace("\r\n", "\n").replace("\r", "\n")
    for line in unified.split("\n"):
        trimmed = line.rstrip()
        if not trimmed.strip():
            if output:
                blank = True
            continue
        if blank:
            output.append("")
            blank = False
        output.append(trimmed)
    return "\n".join(output).strip()


def run_git(
    root: Path | str,
    args: list[str],
    operation: str,
) -> bytes:
    root_path = Path(root)
    git_bin = "/usr/bin/git" if os.path.exists("/usr/bin/git") else "git"
    try:
        proc = run_with_timeout(
            [git_bin, "-C", str(root_path)] + args,
            capture_output=True,
            timeout=GIT_QUERY_S,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ContextGitError(operation, "timed out after 10 seconds") from e
    except OSError as e:
        raise ContextGitError(operation, str(e)) from e

    if len(proc.stdout) > MAX_GIT_OUTPUT:
        raise ContextBoundError("git-output")
    if proc.returncode != 0:
        raise ContextGitError(operation, f"exit status {proc.returncode}")
    return proc.stdout


def repository_head(root: Path | str) -> str:
    out = run_git(root, ["rev-parse", "--verify", "HEAD"], "rev-parse")
    try:
        head = out.decode("utf-8").strip()
    except UnicodeDecodeError as e:
        raise ContextGitError("rev-parse", "output was not UTF-8") from e

    if len(head) != 40 or not all(c in "0123456789abcdefABCDEF" for c in head):
        raise ContextGitError("rev-parse", "HEAD was not a full commit id")
    return head.lower()


def tracked_checkout_paths(root: Path | str) -> set[str]:
    listed = run_git(root, ["ls-files", "-z"], "ls-files")
    paths: set[str] = set()
    for raw in listed.split(b"\x00"):
        if not raw:
            continue
        try:
            p = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ContextGitError("ls-files", "a tracked path was not UTF-8") from e
        paths.add(p)
        if len(paths) > MAX_FILES:
            raise ContextBoundError("file-count")
    return paths


def safe_relative(path: str) -> Path:
    p = PurePath(path)
    if (
        p.is_absolute()
        or any(part in ("..", "") for part in p.parts)
        or path.startswith("~")
    ):
        raise ContextPathError(path)
    return Path(path)


def read_checkout_excerpt(
    root: Path | str,
    tracked: set[str],
    path: str,
    start_line: int,
    end_line: int | None = None,
) -> tuple[str, int]:
    if (
        not path
        or len(path) > 512
        or path.startswith("~")
        or "\\" in path
        or any(ord(c) < 32 for c in path)
    ):
        raise ValueError("adapter path was refused by the tracked-path fence")
    try:
        safe_relative(path)
    except ContextPathError as e:
        raise ValueError("adapter path was refused by the tracked-path fence") from e

    if path not in tracked:
        raise ValueError("adapter path was refused by the tracked-path fence")
    if start_line == 0 or (end_line is not None and end_line < start_line):
        raise ValueError("adapter line span was invalid")
    if not anchor_path_present(root, path):
        raise ValueError("adapter path was unavailable or symlinked")

    file_path = Path(root) / path
    try:
        st = os.lstat(file_path)
    except OSError as e:
        raise ValueError("adapter checkout file was unavailable") from e

    if os.path.islink(file_path) or st.st_size > MAX_FILE_BYTES:
        raise ValueError("adapter checkout file was not a bounded regular file")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError("adapter checkout file could not be read") from e

    requested_end = (
        min(end_line, start_line + MAX_ADAPTER_LINES - 1)
        if end_line is not None
        else (start_line + MAX_ADAPTER_LINES - 1)
    )

    sliced = lines[start_line - 1 : requested_end]
    if not sliced:
        raise ValueError("adapter line span was outside the checkout file")

    returned_end = start_line + len(sliced) - 1
    excerpt = normalize_chunk("".join(sliced))
    if not excerpt:
        raise ValueError("adapter line span contained no text")
    return excerpt, returned_end


def run_json_adapter(
    program: Path | str,
    args: list[str],
    cwd: Path | str,
) -> Any:
    prog_path = Path(program)
    if not prog_path.is_absolute():
        raise ValueError("configured executable path is not absolute")

    try:
        os.lstat(prog_path)
    except OSError as e:
        raise ValueError(str(e)) from e

    if not prog_path.is_file() or os.path.islink(prog_path):
        raise ValueError("configured executable is not a regular non-symlink file")

    cwd_path = Path(cwd).resolve()
    if not cwd_path.is_dir():
        raise ValueError("configured adapter cwd is not a directory")

    try:
        proc = run_with_timeout(
            [str(prog_path)] + args,
            cwd=str(cwd_path),
            capture_output=True,
            timeout=PROBE_S,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ValueError("adapter timed out after 3 seconds") from e
    except OSError as e:
        raise ValueError(str(e)) from e

    if len(proc.stdout) > MAX_ADAPTER_OUTPUT:
        raise ValueError("adapter output exceeded 1 MiB")
    if proc.returncode != 0:
        raise ValueError(f"adapter exited with exit status: {proc.returncode}")

    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except Exception as e:
        raise ValueError("adapter output was not JSON") from e


@dataclass
class ContextProvenance:
    provider: str
    address: str
    start_line: int | None = None
    end_line: int | None = None
    revision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "provider": self.provider,
            "address": self.address,
        }
        if self.start_line is not None:
            d["start_line"] = self.start_line
        if self.end_line is not None:
            d["end_line"] = self.end_line
        if self.revision is not None:
            d["revision"] = self.revision
        return d


@dataclass
class AdapterHit:
    id: str
    content: str
    provenance: ContextProvenance


def adapter_status_dict(
    source: str, status: str, detail: str | None = None
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "source": source,
        "status": status,
        "staleness_basis": "adapter_status",
    }
    if detail is not None:
        d["detail"] = detail
    return d


def query_adapter_program(
    source: str,
    query: str,
    limit: int,
    anchor_root: Path | str,
    program: Path | str,
) -> tuple[dict[str, Any], list[AdapterHit]]:
    root_path = Path(anchor_root).resolve()
    if not root_path.is_dir():
        return adapter_status_dict(
            source, "unavailable", "configured anchor root is not a directory"
        ), []

    try:
        current_rev = repository_head(root_path)
        tracked = tracked_checkout_paths(root_path)
    except (OSError, ValueError, ContextIndexError) as e:
        return adapter_status_dict(source, "unavailable", str(e)), []

    prog_path = Path(program)
    try:
        status_json = run_json_adapter(prog_path, ["status", "--json"], root_path)
    except (OSError, ValueError) as e:
        return adapter_status_dict(source, "unavailable", str(e)), []

    declared_ready = (
        status_json.get("ready") is True or status_json.get("initialized") is True
    )
    indexed = status_json.get("indexed")
    if indexed is None and isinstance(status_json.get("index"), dict):
        indexed = status_json["index"].get("state") == "complete"
    if source == "codegraph":
        ready = declared_ready and indexed is not False
    elif source == "jbcontext":
        ready = (
            declared_ready
            and indexed is True
            and status_json.get("authenticated") is True
        )
    else:
        ready = False

    if not ready:
        return adapter_status_dict(
            source, "unavailable", "adapter is not ready, indexed and authenticated"
        ), []

    if source == "codegraph":
        args = ["query", "--json", "--limit", str(min(limit, 250)), query]
    else:
        args = ["query", "--json", "--query", query, "--limit", str(min(limit, 250))]
    try:
        val = run_json_adapter(prog_path, args, root_path)
    except (OSError, ValueError) as e:
        return adapter_status_dict(source, "unavailable", str(e)), []

    revision = status_json.get("revision")
    if revision and len(revision) > 128:
        revision = None

    rows: list[Any] | None
    if isinstance(val, list):
        rows = val
    elif isinstance(val, dict):
        raw_results = val.get("results")
        rows = raw_results if isinstance(raw_results, list) else None
    else:
        rows = None
    if rows is None:
        return adapter_status_dict(
            source, "unavailable", "adapter results were not an array"
        ), []

    hits: list[AdapterHit] = []
    refused = 0
    for idx, row in enumerate(rows[: min(limit, 250)]):
        if not isinstance(row, dict):
            refused += 1
            continue
        raw_node = row.get("node")
        node: dict[str, Any] = raw_node if isinstance(raw_node, dict) else row
        path_val = node.get("path") or node.get("filePath") or node.get("uri")
        start_line = node.get("start_line") or node.get("startLine") or node.get("line")
        end_line = node.get("end_line") or node.get("endLine")
        if not path_val or start_line is None:
            refused += 1
            continue
        try:
            start_l = int(start_line)
            end_l = int(end_line) if end_line is not None else None
            excerpt, ret_end = read_checkout_excerpt(
                root_path, tracked, str(path_val), start_l, end_l
            )
        except ValueError, OSError:
            refused += 1
            continue

        hits.append(
            AdapterHit(
                id=f"{source}-{idx}",
                content=excerpt,
                provenance=ContextProvenance(
                    provider=source,
                    address=str(path_val),
                    start_line=start_l,
                    end_line=ret_end,
                    revision=current_rev,
                ),
            )
        )

    if revision == current_rev:
        stat = "ready"
        detail = None
    elif revision:
        stat = "stale"
        detail = "adapter revision does not match checkout HEAD"
    else:
        stat = "unknown_staleness"
        detail = "adapter did not report a revision"

    if refused > 0:
        ref_msg = f"{refused} adapter result(s) refused by checkout fence"
        detail = f"{detail}; {ref_msg}" if detail else ref_msg

    res_stat: dict[str, Any] = {
        "source": source,
        "status": stat,
        "current_revision": current_rev,
        "staleness_basis": "git_head",
    }
    if revision:
        res_stat["revision"] = revision
    if detail:
        res_stat["detail"] = detail

    return res_stat, hits


def query_adapter(
    source: str,
    query: str,
    limit: int,
    anchor_root: Path | str,
) -> tuple[dict[str, Any], list[AdapterHit]]:
    if source == "codegraph":
        bin_env = env("NOTT_NMEMORY_CODEGRAPH_BIN")
        if not bin_env:
            return adapter_status_dict(
                source, "unavailable", "executable is not configured"
            ), []
        return query_adapter_program(source, query, limit, anchor_root, bin_env)
    elif source == "jbcontext":
        if env("NOTT_NMEMORY_JBCONTEXT_ENABLED") != "1":
            return adapter_status_dict(
                source, "disabled", "explicit opt-in is not enabled"
            ), []
        bin_env = env("NOTT_NMEMORY_JBCONTEXT_BIN")
        if not bin_env:
            return adapter_status_dict(
                source, "unavailable", "executable is not configured"
            ), []
        return query_adapter_program(source, query, limit, anchor_root, bin_env)
    return adapter_status_dict(source, "unavailable", "unknown adapter"), []
