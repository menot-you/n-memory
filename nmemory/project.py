"""Project identity: the subtree rule, the identity folds, and the SQL fence.

Pure domain helpers with no store behind them. They lived in the PostgreSQL module
until that module was retired; every reader and writer imports them from here.
"""

from __future__ import annotations

#: The project a capsule belongs to when neither the caller nor the environment
#: names one.
DEFAULT_PROJECT_ID: str = "default"

#: Roots whose default does NOT expand to subtree, and why.
#:
#: `in_project_subtree` matches `nott/monorepo` under `nott`, and `nott` is a
#: LIVE default — arrives via `--project nott` and `NOTT_NMEMORY_PROJECT`. But `nott` is a
#: MIXED bucket: its children `nott/monorepo*` are the owner's other system,
#: which `CLAUDE.md` mandates preserving and which this map deliberately refuses
#: to attribute (see note on `nott/monorepo` below). Without this list, the default
#: audience of a reader in `nott` would include memory from a system
#: they never asked for.
#:
#: Not a security fence: whoever passes EXPLICIT `project_prefix="nott"`
#: continues to reach the subtree, because they chose it. This governs only what
#: OMISSION means.
#:
#: `pynott` is not here, and that is the difference that matters: its subtree
#: belongs entirely to this system.
SUBTREE_DEFAULT_DENY: frozenset[str] = frozenset({"nott"})


def expands_to_subtree_by_default(project_id: str | None) -> bool:
    """Is this project's default the tree, or only the node?

    A single rule for BOTH surfaces. `nmemory_context_show` has had this
    exposure since 2026-09-15 and `nmemory_capsule_retrieve` was going to inherit it: propagating
    a loophole to a second surface instead of closing it was the defect named by
    independent review.
    """
    return project_id is not None and project_id not in SUBTREE_DEFAULT_DENY


def glob_subtree_pattern(prefix: str) -> str:
    """The SQL `GLOB` pattern mirroring `in_project_subtree` — and ONLY it.

    `GLOB` compares case-sensitively (BINARY), which `LIKE` in SQLite does not:
    `LIKE` would fence `Raven` and `raven` together. `*`, `?` and `[` are GLOB
    metacharacters and NOT subtree metacharacters, so each is escaped as a
    one-character class: passing the raw prefix would make `"*"` match EVERYTHING
    and `"tre?"` match `tree/child`, which the literal predicate rejects.
    """
    escaped = "".join(f"[{c}]" if c in "*?[" else c for c in prefix)
    return f"{escaped}/*"


PROJECT_ALIASES: dict[str, str] = {}


# PITFALL, measured on 2026-09-17 and documented here because it cost eight tests:
# `nmemory` (655 live capsules) and `nott` (1408) are real store spellings AND were
# project names in test fixtures. In PRODUCTION there is no asymmetry — write
# canonicalizes in `ingest.py` and read canonicalizes at server boundary —,
# but a test writing DIRECTLY via `Store.append` stores the raw spelling and, if it
# later reads via the server, the request comes folded and does not match. Whoever
# writes a test crossing that boundary uses a name this map does NOT claim.
#
# Canonicalizing in `Store.append` would be worse, and the reason is structural:
# `capsule.scope.project_id` enters simultaneously into the `project_id` column and
# `canonical_json`, and desynchronizing them alters the canonical digest.
def canonical_project(project_id: str) -> str:
    """Fold a project spelling onto its canonical key. Unknown keys pass."""
    return PROJECT_ALIASES.get(project_id, project_id)


def in_project_subtree(project_id: str, prefix: str) -> bool:
    """The prefix half of `project_matches` (pin `store.rs:1802`).

    The prefix matches the EXACT id or the id followed by the subtree separator:
    `"nott"` covers `"nott"` and `"nott/x"`, never `"nottx"`. A bare `startswith`
    is a pseudo-fence that catches the wrong neighbor.
    """
    return project_id == prefix or project_id.startswith(f"{prefix}/")
