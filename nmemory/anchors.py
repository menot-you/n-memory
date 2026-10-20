"""U2 (nmemory-standing): the anchor loop, model-free.

Measured 2026-09-16: 282 of 834 live capsules anchored in code (33.8%)
had demonstrably dead anchors — the file disappeared or the line fell
outside it — and were served with full authority. Files changed and nobody
notified memory: this is precisely the session agent dependency the owner
wants to cut.

The cheapest actor possible: a `PostToolUse` hook queues the edited path
(`plugin/hooks/nmemory-anchor-queue.sh`, only writes to a spool and exits), and this
drain judges the anchor against the repo WORKING TREE that owns the file
and writes `nmemory.verification_events`. The verdict is `pass` / `dead_anchor`
(file does not exist) / `moved_anchor` (line outside file) /
`unverifiable` (this tree cannot judge this anchor). Nothing here
wakes a model; escalation for judgment belongs to the curator (U4), and only when
there is an anchored capsule.

ponytail: `_live_code_anchored` scans the entire `store.list()` (O(n), ~12k
rows, ~1s) instead of indexing `provenance.anchor`; the drain runs off the
critical path, so the cost is acceptable until the store grows by an order of
magnitude — at which point an index by path will be created.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import NMemoryError
from .context_readers import ContextIndexError, run_git, tracked_checkout_paths
from .queries.curator import sibling_excluded_among
from .repositories.capsules import ListFilter, StoredCapsule
from .store import Store
from .subproc import GIT_QUERY_S, run_with_timeout

logger = logging.getLogger(__name__)

# A relative path with extension, optionally `:line` or `:line-line`. A
# verbatim quote, `&cap-N`, `doc-N`, `PR #N`, and paths without extension are excluded.
_CODE_ANCHOR = re.compile(r"^([A-Za-z0-9_./\-]+?\.([A-Za-z0-9]+))(?::(\d+))?(?:-\d+)?$")
# Review 2026-09-16: `x.y` is not a path. Without `/`, only a root file with a
# known extension (`README.md`); with `/`, the extension is alphabetic — `v1.2`, `0.5`, `menot.you`,
# and `e.g` became `dead_anchor` → `caducou` under `--all`.
_ROOT_FILE_EXT = frozenset(
    [
        "md",
        "txt",
        "toml",
        "json",
        "yaml",
        "yml",
        "py",
        "sh",
        "rs",
        "ts",
        "js",
        "cfg",
        "ini",
        "lock",
        "sql",
        "env",
    ]
)

#: Run output, not source code: `lanes/state/…/codex-sol.out`,
#: `.claude/battery-….output`. The file is born to be deleted, and deleting it is
#: no evidence whatsoever about the fact that cited that line.
_ARTIFACT_EXT = frozenset(["log", "out", "output", "err", "tmp", "done"])

#: Normalization of anchored content before hashing. Absorbs indentation and
#: whitespace — `ruff format` and tabs-to-spaces are noise, and escalating them
#: flooded the curator queue, which costs money. DOES NOT absorb a rewrap that
#: splits a line in two: that continues to escalate, and is the declared price of a
#: detector LOCAL to the line rather than a window (a 40-line window invalidates
#: the hash because of any neighbor).
_WHITESPACE = re.compile(r"\s+")
#: 16 hex characters (64 bits) rather than sha256's 64: `detail` is read by humans during
#: auditing, and to compare a line against itself no collision matters.
#: The byte argument does not count — 1571 capsules yield 25 KB or 100 KB.
_DIGEST_CHARS = 16

ANCHOR_KIND = "anchor"
ANCHOR_ACTOR = "anchor-check"


class AnchorVerdictError(ValueError):
    """An anchor verdict that the census does not know how to count. Falling into a silent `else`
    contaminated the dead anchor rate with the wrong category."""

    def __init__(self, verdict: str) -> None:
        super().__init__(f"unknown anchor verdict: {verdict!r}")


class AnchorScopeError(ValueError):
    """Recording a verdict requires the project fence: without it, foreign scopes are
    judged against this tree and become `caducou` — append-only, no undo."""


def parse_code_anchor(anchor: str) -> tuple[str, int | None] | None:
    a = anchor.strip()
    if not a or "\n" in a or a.startswith(("&", "/", "~")):
        return None
    m = _CODE_ANCHOR.match(a)
    if m is None:
        return None
    path, ext = m.group(1), m.group(2).lower()
    if ".." in path.split("/"):
        return None
    if "/" not in path:
        if ext not in _ROOT_FILE_EXT:
            return None
    elif not ext.isalpha() or len(ext) < 2:
        return None
    return path, (int(m.group(3)) if m.group(3) else None)


def confined_file(root_r: Path, path: str) -> Path | None:
    """The resolved file `path` names inside the resolved root `root_r`, or None.

    The anchor comes from the capsule (agent-inferred, imported, tainted): `..` and
    symlinks that leave the root, or a path that is not a file, are dead.
    """
    try:
        target = (root_r / path).resolve()
    except OSError:
        return None
    if not target.is_relative_to(root_r) or not target.is_file():
        return None
    return target


def _unjudgeable_here(root: Path, path: str) -> bool:
    """“The file disappeared” and “this anchor was never about this tree” are
    different things, and only the first is evidence about the anchored fact.

    Measured in the live store on 2026-09-17: of 162 mechanical anchor candidates, 60
    (37%) had a first segment that is not a directory of this repo
    (`scratchpad/`, `reports/`, `native369/`, `PR277/`, run names, SHAs)
    and 3 pointed to run output inside real directories. All 63
    came out as `dead_anchor`, entered the curator queue, and returned fenced as
    false — 18 fences had to be revoked manually.

    The first segment rule is deliberately mechanical: a list of
    disposable directories rots with each new repo, and what the tree has is
    precisely what the tree knows how to judge. A deleted top-level directory makes
    its subtree `unverifiable` rather than dead — the conservative direction: if
    `apps/` disappears, the tree is wrong, not memory."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in _ARTIFACT_EXT:
        return True
    top, _, rest = path.partition("/")
    return bool(rest) and not (root / top).is_dir()


_CONHECIDOS: dict[str, frozenset[str] | None] = {}


def _conhecidos(root: Path) -> frozenset[str] | None:
    """Everything this tree EVER tracked — or `None` without git.

    The index (`git ls-files`) DOES NOT answer the question: a file deleted in a
    commit leaves the index, and in a clean checkout at HEAD — which is the
    production mode of `anchor check --all` — it would be indistinguishable from “never tracked”.
    That would make `dead_anchor` unreachable and collapse `dead_rate_pct`, which is
    the program's second health metric. The answer requires HISTORY: the
    index UNION the paths that some commit removed. Measured in this tree on
    2026-09-18: 6279 commits, 1166 removed paths, 0.12s — once per root.

    `judge_anchor` and `audit_all` never depended on git (only the drain depends on it,
    `_same_repository`), and no test fixture is a repository. Without git the
    behavior remains as always: the decision falls back to `dead_anchor`."""
    key = str(root)
    if key in _CONHECIDOS:
        return _CONHECIDOS[key]
    try:
        vivos = tracked_checkout_paths(root)
        # `--no-renames` IS MANDATORY: since git 2.9, `log` detects renames by
        # default, a committed `git mv` appears as `R` and `--name-only` prints
        # ONLY the new name — the old path, which the tree in fact lost,
        # escaped the union and became `unverifiable`. Measured in this tree on
        # 2026-09-18: 1166 paths with detection enabled, 1838 without it, 672
        # hidden. Without the flag, the result still depended on the host's
        # `diff.renames`, which is configuration, not a contract.
        #
        # `-z` for the same reason as `ls-files -z`: without it `core.quotePath` (the
        # default) quotes non-ASCII paths and they would never match. With `-z` they come
        # raw, terminated by NUL.
        removidos = run_git(
            root,
            [
                "log",
                "-z",
                "--no-renames",
                "--diff-filter=D",
                "--name-only",
                "--pretty=format:",
                "HEAD",
            ],
            "log-deleted",
        )
        conhecidos = vivos | {
            caminho
            for caminho in removidos.decode("utf-8", "replace").split("\0")
            if caminho and caminho != "\n"
        }
        _CONHECIDOS[key] = frozenset(conhecidos)
    except (ContextIndexError, NMemoryError, OSError) as exc:
        # Silence here was the defect: degradation beyond the limits of
        # `context.py` (10,000 files, 8 MiB) changed the verdict without leaving
        # a trace. This is not a product failure — it is an operational fact, and stated as such.
        logger.info("anchors: no git history at %s (%s); legacy verdict", root, exc)
        _CONHECIDOS[key] = None
    return _CONHECIDOS[key]


def _ausente(root: Path, path: str) -> str:
    """The file is not there. “Died” or “never belonged to this tree”?

    A path that git never tracked is a session or run artifact
    (`docs/qa/raw/review-*.md`, gate outputs, dossiers) — its absence is not
    evidence about the anchored fact, and that is where 91 of the 95 fences of
    2026-09 came from. `dead_anchor` remains reserved for what the tree in fact LOST,
    which includes — and this was the loophole in the first version — a file removed by a
    commit, which the index alone cannot identify.

    The question is only asked when the file is ALREADY absent: a file that exists and
    git does not track (`AGENTS.md` today) continues to be judged by its content."""
    conhecidos = _conhecidos(root)
    if conhecidos is None:
        return "dead_anchor"
    return "dead_anchor" if path in conhecidos else "unverifiable"


def normalized_line_digest(root: Path, path: str, line: int | None) -> str | None:
    """What the anchored line SAYS now, normalized and digested — or `None`.

    The loophole this closes (cell D of U5, measured 6 → 6, zero movement):
    `judge_anchor` counts LINES, so an edit that preserves the line count
    answers `pass`, the capsule never enters the curator queue, and a statement
    that became false continues to be served with full authority.

    Reads the tree, never the capsule, and stops at the requested line — a 1 GB
    artifact is not read in its entirety. `None` when the anchor does not name a line
    (an entire `README.md` has no local content to digest) or the tree does not serve it.
    """
    if line is None or line < 1:
        return None
    target = confined_file(root.resolve(), path)
    if target is None:
        return None
    try:
        with target.open("rb") as fh:
            for number, raw in enumerate(fh, start=1):
                if number == line:
                    text = _WHITESPACE.sub(" ", raw.decode("utf-8", "replace")).strip()
                    return hashlib.sha256(text.encode("utf-8")).hexdigest()[
                        :_DIGEST_CHARS
                    ]
    except OSError:
        return None
    return None


def anchor_detail(path: str, line: int | None, digest: str | None) -> str:
    """The event's `detail`: `path:line#hash`. The hash travels IN the detail and not in a
    new column because `verification_events` is append-only and non-native — zero
    DDL, `CONTRACT_VERSION` remains 29 — and because the most recent event is already
    how the curator queue and `standing_of` read the past."""
    base = f"{path}:{line}" if line is not None else path
    return f"{base}#{digest}" if digest else base


def drift_verdict(structural: str, digest: str | None, previous: str | None) -> str:
    """Composes the TREE verdict with the memory of what the line said previously.

    `judge_anchor` answers about the tree and never touches the store — whoever holds
    the store knows the past. A structural `pass` splits into three:
    `baseline` (never seen this line: costs no model and does not falsely claim `vale`),
    `pass` (says the same) and `changed_anchor` (says something else).
    """
    if structural != "pass" or digest is None:
        return structural
    if previous is None:
        return "baseline"
    return "pass" if previous == digest else "changed_anchor"


def judge_anchor(root: Path, path: str, line: int | None) -> str:
    """Reads the tree, never the capsule: the capsule is what is being judged."""
    # Review 2026-09-16: the anchor comes from the capsule (agent-inferred, imported, tainted);
    # `root / path` followed `..` and symlinks outside the repo and recorded target existence
    # in `verification_events.detail`. Confined to the resolved root, or dead.
    root_r = root.resolve()
    if _unjudgeable_here(root_r, path):
        return "unverifiable"
    target = confined_file(root_r, path)
    if target is None:
        return _ausente(root_r, path)
    if line is None:
        return "pass"
    try:
        with target.open("rb") as fh:
            lines = 0
            for _ in (
                fh
            ):  # stops at requested line: a 1 GB artifact is not read in its entirety
                lines += 1
                if lines >= line:
                    return "pass"
    except OSError:
        return _ausente(root_r, path)
    return "pass" if line <= max(lines, 1) else "moved_anchor"


def effective_anchor(store: Store, sc: StoredCapsule) -> str:
    """`nmemory_capsule_reanchor` already exists and is the repair path: its anchor wins."""
    return store.anchors.get_reanchor(sc.id) or sc.capsule.provenance.anchor


def _live_code_anchored(
    store: Store, project: str | None
) -> list[tuple[StoredCapsule, str, int | None]]:
    """Anchors are repo-relative, and the scope IS the repo: without the project
    fence, `README.md` from another scope is judged against this tree and counts
    as dead (measured: 57.4% without fence vs 33.8% with it). `project` fences the
    subtree (`pynott` reaches `pynott/x`); `None` is “all”, and stated as such."""
    rows = store.capsules.list(ListFilter(limit=None, project_prefix=project))
    ids = [sc.id for sc in rows]
    dead = sibling_excluded_among(store.db, ids)
    rebound = store.anchors.reanchors_among(
        ids
    )  # one query, not one connection per capsule
    out: list[tuple[StoredCapsule, str, int | None]] = []
    for sc in rows:
        if sc.id in dead:
            continue
        parsed = parse_code_anchor(rebound.get(sc.id) or sc.capsule.provenance.anchor)
        if parsed is not None:
            out.append((sc, parsed[0], parsed[1]))
    return out


def _git_common_dir(tree: Path) -> Path | None:
    try:
        out = run_with_timeout(
            [
                "git",
                "-C",
                str(tree),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_QUERY_S,
        )
    except subprocess.TimeoutExpired, OSError:
        # A `git` stuck on a lock or a slow mount would hang the entire timer.
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return Path(out.stdout.strip()).resolve()


def _same_repository(event_root: Path, root: Path) -> bool:
    """The exact root, or a linked worktree of the same repository (measured: 251 of
    251 events from the real spool came from worktrees and were discarded)."""
    if event_root == root:
        return True
    common = _git_common_dir(event_root)
    return common is not None and common == _git_common_dir(root)


def _under(anchor_path: str, edited_path: str) -> bool:
    edited = edited_path.rstrip("/")
    return anchor_path == edited or anchor_path.startswith(edited + "/")


@dataclass(frozen=True)
class DrainReport:
    events: int
    judged: int
    foreign: int
    malformed: int


def drain_spool(
    store: Store, spool: Path, root: Path, *, now: datetime, project: str | None = None
) -> DrainReport:
    """Consumes the hook spool: one event per file, `{"path", "root", "at"}`.

    `path` is relative to `root` (the hook resolves both). An event from another repo
    is discarded, never resolved against the wrong tree: anchors are
    repo-relative, and the hook fires for any repository on the host.
    """
    if not project:
        raise AnchorScopeError("drain_spool records verdicts: pass the project scope")
    root_r = root.resolve()
    events = judged = foreign = malformed = 0
    anchored: list[tuple[StoredCapsule, str, int | None]] | None = None
    same_repo: dict[Path, bool] = {}
    if not spool.is_dir():
        return DrainReport(0, 0, 0, 0)
    for event_file in sorted(spool.glob("*.json")):
        events += 1
        try:
            event = json.loads(event_file.read_text(encoding="utf-8"))
            edited = str(event["path"])
            event_root = Path(str(event["root"])).resolve()
        except OSError, ValueError, KeyError, TypeError:
            malformed += 1
            event_file.unlink(missing_ok=True)
            continue
        if event_root not in same_repo:
            same_repo[event_root] = _same_repository(event_root, root_r)
        if not same_repo[event_root]:
            foreign += 1
            event_file.unlink(missing_ok=True)
            continue
        if anchored is None:
            anchored = _live_code_anchored(store, project)
        for sc, apath, line in anchored:
            if _under(apath, edited):
                # The event is merely the SIGNAL “this path changed in this repository”; the
                # verdict is always about `root`, the tree that the capsule
                # describes. Judging the event tree recorded the WIP of a
                # branch as a fact about the repo — and `dead_anchor`,
                # `moved_anchor`, and `changed_anchor` are `CURATOR_VERDICTS`,
                # so an abandoned branch would send true capsules to
                # the curator queue and poison the digest of `root` (blocking
                # finding of the independent review, 2026-09-18).
                digest = normalized_line_digest(root_r, apath, line)
                store.anchors.record_verification(
                    sc.id,
                    kind=ANCHOR_KIND,
                    result=drift_verdict(
                        judge_anchor(root_r, apath, line),
                        digest,
                        store.anchors.last_anchor_digests_among([sc.id]).get(sc.id),
                    ),
                    detail=anchor_detail(apath, line, digest),
                    actor=ANCHOR_ACTOR,
                    now=now,
                )
                judged += 1
        event_file.unlink(missing_ok=True)
    return DrainReport(events, judged, foreign, malformed)


@dataclass(frozen=True)
class AuditReport:
    anchored: int
    ok: int
    dead: int
    moved: int
    unverifiable: int = 0
    """Counted separately and OUTSIDE of `anchored`: the dead anchor rate is the program's
    second health metric, and an anchor that this tree cannot judge is neither
    evidence for nor against. Adding it inflated the rate by 37%."""
    baseline: int = 0
    """First time this line was digested: there is no past to compare against.
    Counts INSIDE `anchored` (the anchor exists and resolves) but outside of
    `dead_rate_pct`: this is not drift, it is ignorance, and claiming otherwise on the
    first sweep would mark 1571 capsules all at once."""
    changed: int = 0
    """The anchored line now says something else — cell D, which was
    invisible because the judge counted lines."""

    @property
    def dead_rate_pct(self) -> float:
        if not self.anchored:
            return 0.0
        return round(100.0 * (self.dead + self.moved + self.changed) / self.anchored, 1)


def audit_all(
    store: Store,
    root: Path,
    *,
    now: datetime,
    record: bool = True,
    project: str | None = None,
) -> AuditReport:
    """The full sweep: the dead anchor rate is the program's second health
    metric — mechanical, and not gameable by model opinion."""
    if record and not project:
        raise AnchorScopeError("audit_all(record=True) needs the project scope")
    root_r = root.resolve()
    ok = dead = moved = skipped = first = changed = 0
    anchored_rows = _live_code_anchored(store, project)
    # What each line SAID in the previous sweep, in a single query.
    previous = store.anchors.last_anchor_digests_among(
        [sc.id for sc, _, _ in anchored_rows]
    )
    for sc, apath, line in anchored_rows:
        digest = normalized_line_digest(root_r, apath, line)
        verdict = drift_verdict(
            judge_anchor(root_r, apath, line), digest, previous.get(sc.id)
        )
        if record:
            store.anchors.record_verification(
                sc.id,
                kind=ANCHOR_KIND,
                result=verdict,
                detail=anchor_detail(apath, line, digest),
                actor=ANCHOR_ACTOR,
                now=now,
            )
        if verdict == "pass":
            ok += 1
        elif verdict == "dead_anchor":
            dead += 1
        elif verdict == "moved_anchor":
            moved += 1
        elif verdict == "unverifiable":
            skipped += 1
        elif verdict == "baseline":
            first += 1
        elif verdict == "changed_anchor":
            changed += 1
        else:  # exhaustive dispatch: a new verdict refuses, never falls into a branch
            raise AnchorVerdictError(verdict)
    return AuditReport(
        ok + dead + moved + first + changed,
        ok,
        dead,
        moved,
        skipped,
        first,
        changed,
    )


def spool_dir() -> Path:
    """The exact pattern of `plugin/hooks/session-state.sh`: a drain launched without
    `XDG_RUNTIME_DIR` (service, nSPAWN with clean env) must read the SAME spool."""
    import os

    base = (
        os.environ.get("XDG_RUNTIME_DIR")
        or os.environ.get("XDG_STATE_HOME")
        or str(Path.home() / ".local" / "state")
    )
    return Path(base) / "nott" / "anchor-queue"
