"""Anchor resolution, liveness probes, and drift detection (u-r12).

Verifies filesystem anchors against the anchor root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .capsule import sha256_hex


@dataclass(frozen=True)
class ParsedAnchor:
    path: str | None
    sha: str | None


def _is_hex_7_40(s: str) -> bool:
    return 7 <= len(s) <= 40 and all(c in "0123456789abcdefABCDEF" for c in s)


_RESERVED_LOGICAL_PREFIXES = (
    "doc-",
    "cap-",
    "sess-",
    "gate-",
    "task-",
    "epic-",
    "plan-",
    "dod-",
    "decision-",
    "fact-",
    "out-",
    "rcpt-",
    "wf_",
    "ADR-",
)


def _is_logical_identifier(candidate: str) -> bool:
    if candidate.startswith("session:"):
        return True
    if "/" in candidate:
        return False
    if "." in candidate and not candidate.startswith("."):
        stem, ext = candidate.rsplit(".", 1)
        if stem and 1 <= len(ext) <= 10 and ext.isalnum():
            return False
    if candidate.startswith("&") and len(candidate) > 1:
        return True
    return any(
        candidate.startswith(p) and len(candidate) > len(p)
        for p in _RESERVED_LOGICAL_PREFIXES
    )


def parse_anchor(anchor: str) -> ParsedAnchor:
    # 1. Split origin (e.g. github:owner/repo//path:line)
    if "//" in anchor:
        _, rest = anchor.split("//", 1)
    else:
        rest = anchor

    # 2. Peel @sha suffix
    if "@" in rest:
        head, sha_candidate = rest.rsplit("@", 1)
        if _is_hex_7_40(sha_candidate):
            sha = sha_candidate
            head_part = head
        else:
            sha = None
            head_part = rest
    else:
        sha = None
        head_part = rest

    # 3. Path:line check (peels trailing :digits iteratively, e.g. path:line, path:line:col)
    path = None
    if ":" in head_part:
        curr = head_part
        peeled = False
        while ":" in curr:
            p, suffix = curr.rsplit(":", 1)
            if suffix.isdigit():
                curr = p
                peeled = True
            else:
                break
        if (
            peeled
            and curr.strip()
            and not any(c.isspace() for c in curr)
            and not curr.startswith("/")
            and not curr.startswith("..")
            and "://" not in curr
            and not _is_logical_identifier(curr)
        ):
            path = curr

    return ParsedAnchor(path=path, sha=sha)


def parse_anchor_path(anchor: str) -> str | None:
    """Extract filesystem path from anchor like 'PLAN.md:45' or 'src/main.rs:10:20'."""
    parsed = parse_anchor(anchor)
    return parsed.path


def _confined(root: str, path: str) -> tuple[str, str] | None:
    """`(joined, real)` of `path` under `root`, or None when it resolves outside it."""
    real_root = os.path.realpath(root)
    joined = os.path.join(root, path)
    real = os.path.realpath(joined)
    if os.path.commonpath([real_root, real]) != real_root:
        return None
    return joined, real


def anchor_file(root: str, path: str) -> str | None:
    """The real file `path` names under `root`, or None (escape, absent, link).

    The one confinement fence for an anchor path; filesystem errors propagate.
    """
    confined = _confined(root, path)
    if confined is None:
        return None
    joined, real = confined
    if not os.path.isfile(real) or os.path.islink(joined):
        return None
    return real


def anchor_path_present(repo: Path | str, rel_path: str) -> bool:
    """Whether every part of a relative `rel_path` exists under `repo`, no part a link.

    An absolute path, a `..` part or an unreadable part is not present.
    """
    if not rel_path or os.path.isabs(rel_path):
        return False
    rel = Path(rel_path)
    if ".." in rel.parts:
        return False
    probe = Path(repo)
    probed = False
    for part in rel.parts:
        if part in (".", ""):
            continue
        probe = probe / part
        probed = True
        try:
            if os.path.islink(probe) or not probe.exists():
                return False
        except OSError:
            return False
    return probed


def probe_anchor_liveness(anchor: str, root: str) -> str:
    """Probe if anchor file exists in root. Returns 'live', 'missing', or 'unknown'."""
    if not root:
        return "unknown"
    path = parse_anchor_path(anchor)
    if not path:
        return "unknown"
    try:
        if _confined(root, path) is None:
            return "unknown"
        return "live" if anchor_file(root, path) is not None else "missing"
    except OSError:
        return "unknown"


def probe_anchor_drift(
    anchor: str,
    stored_hash: str | None,
    root: str,
) -> str:
    """Probe if anchor content has drifted. Returns 'unchanged', 'drifted', or 'unknown'."""
    if not root or not stored_hash:
        return "unknown"
    path = parse_anchor_path(anchor)
    if not path:
        return "unknown"
    try:
        real = anchor_file(root, path)
        if real is None:
            return "unknown"
        with open(real, "rb") as f:
            content = f.read(2 * 1024 * 1024)
        return "unchanged" if sha256_hex(content) == stored_hash else "drifted"
    except OSError:
        return "unknown"


def quote_verdict(
    anchor: str, anchor_root: str, quote: str, *, errors: str = "strict"
) -> str:
    """Whether `quote` is in the anchored file: 'verified', 'mismatch' or 'unverifiable'.

    `errors` is the UTF-8 decoding policy; under "strict" an undecodable file is
    unverifiable.
    """
    path = parse_anchor(anchor).path
    if not path or not anchor_root:
        return "unverifiable"
    try:
        real = anchor_file(anchor_root, path)
        if real is None:
            return "unverifiable"
        with open(real, "r", encoding="utf-8", errors=errors) as f:
            content = f.read(2 * 1024 * 1024)
        return "verified" if quote in content else "mismatch"
    except OSError, UnicodeDecodeError, ValueError:
        return "unverifiable"
