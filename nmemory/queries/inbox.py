"""The inbox: live capsules in scope that no surface has served yet (U6)."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from ..capsule import Capsule, headline_of
from ..database import Database
from ..project import glob_subtree_pattern
from ..relation import live_relation_sql
from .census import WORK_KINDS

# How many capsules a session receives per delivery, and how much of each. Both
# constants derive from MEASURED COST, not a call rate or budget slice — the first
# two attempts used each of those and both were wrong.
#
# RATE, discarded: the `nmemory-curator` campaign set 8 rows based on "~65
# calls/day"; the initial version here set 20 based on "~15/day". Live traffic for
# `nmemory_context_show` spans THREE days — 104 on 09-13, 21 on 09-14, 13 on 09-15 — the
# first 3.4x the sum of the others and real work (101 calls across 10 hours, median
# gap 130 s), the last ongoing. Three points, one anomalous and one partial,
# support no rate at all; and across the full window, 8 rows meant 1104 deliveries
# against 321 writes, so "the inbox was losing ground" was false.
#
# BUDGET SLICE, discarded: the inbox is appended AFTER the lanes (`tools/context.py`)
# and never touches the `spent`/`token_budget` accounting in `context.py` — it is
# ADDITIVE, not competing. Speaking of "29 % of the 2000" was fiction about a
# budget it does not draw from.
#
# ABSOLUTE COST, which is what is measured and what the session pays: measured
# with `inbox_candidates` against the live store on 2026-09-15, 20 real candidates —
# 589 tokens without headline and 1393 with the full 141-char headline. With the
# short headline the number is 976, MEASURED on the installed release serving a
# real session (20 rows, 3904 bytes, 48.8 tok/row). The 889 this line previously
# claimed was napkin math labelled as measurement — it missed the JSON overhead
# of the `"headline"` key, quotes, and escaping, ~19.4 tok/row rather than 15 —
# and an estimated number claiming "measured" next to a measured one is the defect
# this campaign corrected three times. The previous form (8 rows x full headline)
# already charged 557.
# The SHORT headline stays: 976 additive tokens deliver 2.5x the rows with the
# deciding clause, versus 589 delivering the same quantity without it. Headroom
# against the 1114 ceiling is 138 tokens (12 %), not the 21 % the estimated
# number suggested.
#
# Why the headline returns, against the first draft of this campaign: removing it
# LOSES, measured — `MAX_SLUG_LEN` is 48 chars versus 141 for the headline, and
# measured across the 20 candidates the median handle is 63 chars, of which ~15
# are the kind prefix. That leaves ~48 chars, lowercase and hyphenated, without
# casing, punctuation, or dates. And the part that causes an agent to decide to
# invoke `nmemory_capsule_show` falls precisely in the lost portion:
# `fact-observed-process-race-audit-at-candidate-26e6f5e` versus "Observed
# process-race audit at candidate 26e6f5e9…: 50 isolated real executions of
# `test_stop_then_kill…`". A delivery that fails to support the decision to read
# causes `delivered` to rise without anyone reading — moving the metric without
# moving the truth.
#
# CONSEQUENCE that no constant resolves: the inbox serves the ENTIRE population
# of the scope (2849 live, never-served knowledge capsules under `pynott` on
# 2026-09-15T17:20Z, 2895 in the subtree; 2906 was the count at 10:29Z the same
# day), not a window, and drain days depend on session rate which is unknown — the
# three days of live traffic show 104, 21, and 17 calls/day fleet-wide and 67, 20,
# and 17 IN THE `pynott` SCOPE, the first being real work and the last ongoing.
# For this reason the horizon is REPORTED by the retired census script (git history)
# (`inbox_drain_horizon`), by observed rate and with incoming writes, rather than
# asserted here.
INBOX_HEADLINE_MAX_CHARS: int = 60


# The budget splits across BOTH ENDS of the same queue, and the reason is
# measured: on 2026-09-15 the queue delivery actually drains — the `pynott`
# SUBTREE, the fence applied by the server default — held 2895 candidates, 1853
# older than 7 days and 1042 within them. Oldest-first put those 1042 — the same
# write cohort summarized to the owner by the `SessionStart` hook, not the number
# it prints — behind ~93 archive-draining calls, 1.4 to 5.5 days at scope rates.
# Half backwards keeps the tail draining (pure LIFO starves it forever); half
# forward makes "from now on" arrive immediately, which was the owner's question.
# The total does NOT increase: `INBOX_ROWS` remains the token cost measured above.
INBOX_OLDEST: int = 10


INBOX_NEWEST: int = 10


INBOX_ROWS: int = INBOX_OLDEST + INBOX_NEWEST

#: The query the inbox ran before `served` (`SERVED_DDL`) existed, for a store
#: that has no table yet: 7.7 s on the live copy. The `UNION` is what earns it
#: an automatic index; without it the plan scanned the set once per candidate
#: (19.6 s measured).
_SERVED_FROM_ROWS = """served AS (
                SELECT DISTINCT trim(sid.value) AS capsule_id, 1 AS n
                FROM read_traffic, json_each(read_traffic.served_ids) AS sid
                WHERE revoked_at IS NULL
                UNION SELECT capsule_id, 1 FROM usage
            ), """


def short_headline(headline: str) -> str:
    """The inbox headline: full headline truncated, with an ellipsis indicating it was cut."""
    if len(headline) <= INBOX_HEADLINE_MAX_CHARS:
        return headline
    return headline[:INBOX_HEADLINE_MAX_CHARS].rstrip() + "\u2026"


@dataclass(frozen=True)
class InboxRow:
    """A capsule delivered to its scope's audience (U6).

    Carries a SHORT headline (`INBOX_HEADLINE_MAX_CHARS`), not the full one: the
    full one cost 1393 tokens for 20 rows and the short one 976, and without either
    the row loses the clause by which an agent decides to read (measured: the handle
    yields ~48 hyphenated chars versus 141 for the headline).

    For an `instruction_taint` capsule, NEITHER headline NOR handle with slug is
    sent — only the BARE id. The slug is `slugify(content)`, so it IS content bytes,
    and serving them to a session that did not request them is the injection vector
    identified by the taint precondition in the `nmemory-alcance` campaign.
    """

    seq: int
    id: str
    handle: str
    kind: str | None
    headline: str | None
    instruction_taint: bool

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"id": self.id, "handle": self.handle}
        if self.kind is not None:
            row["kind"] = self.kind
        if self.headline is not None:
            row["headline"] = self.headline
        if self.instruction_taint:
            row["instruction_taint"] = True
        return row


def inbox_candidates(
    db: Database,
    appended: Collection[str],
    scope: str,
    *,
    subtree: bool,
    oldest: int = INBOX_OLDEST,
    newest: int = INBOX_NEWEST,
    exclude_ids: Sequence[str] = (),
    reader_session: str | None = None,
) -> list[InboxRow]:
    """The next capsules to deliver to the audience of `scope` (U6).

    A candidate is a LIVE capsule in scope, never served on ANY surface.
    Delivery draws from BOTH ENDS of the queue — `oldest` oldest and
    `newest` newest, both returned in ascending order of `seq` — because
    oldest-only-first placed what was written this week behind the entire
    archive (1853 candidates older than 7 days versus 1042 within them,
    measured in the `pynott` subtree on 2026-09-15), and newest-only-first
    is a LIFO that leaves the tail to starve.
    A queue shorter than `oldest + newest` returns entirely, without duplicates.

    Capsules that the session ITSELF wrote are not delivered to it, via two
    fences: what this process wrote (`appended`, which asks no one
    for identity and therefore covers Codex) and, in case the server restarted
    within the same harness session, `reader_session`, the session of WHO READS.
    The newest end picks those with highest `seq` in scope, which in a session
    that just wrote are its own. Delivering to an author what they wrote minutes ago
    records `read_traffic surface='inbox'`, and `served` is any surface — the
    capsule became PERMANENTELY ineligible for the inbox and counted as `delivered`
    in the census, while the real audience (the NEXT session) never received it.
    It was the metric moving without truth moving. Caught by independent review,
    with the number: 10 new slots × 17–67 calls/day in scope is 170–670 capsules/day
    of possible echo against 83.7/day of knowledge writes measured in `pynott` — the
    echo could cover the entire week. Identity is that of `capture_context`
    (the capsule registered by the harness), not the `capsules.session_id` mark, which
    is the `nmemory_session_start` bracket and was NULL in 9085 of 12 019 rows
    when measured on 2026-09-15. A reader without identity (`unknown`, the case
    for all of Codex) excludes NOTHING via this fence: over-excluding would strip
    reach from whoever never wrote — the process set covers that.
    A pure reader: no writes, no lock, no digest — it is called from inside
    `nmemory_context_show`, which repository policy forbids from paying the canonical
    boundary.

    There is NO cursor. The initial design had one (`nmemory.delivery_watermark`,
    one seq per scope) and independent review showed it was REDUNDANT and only
    added failure modes: `served` already excludes everything served on any surface,
    and is SELF-HEALING. With a cursor, a transaction catching seq 501 committing
    after the one catching 502 left 501 below the watermark forever; a capsule
    reclassified from `task` to `fact`, or reactivated from `archived`, with seq
    below the watermark, became invisible to the inbox and eternally "undelivered";
    and an advance that raised after the traffic row existed made the census report
    `delivered` for something no one received. Without a cursor, any of these cases
    is redelivered on the next call — the error falls on the safe side.
    """
    # Normalization lives here, not in the caller: the docstring promises that a
    # reader without identity excludes nothing, and a second caller passing the
    # string `unknown` would wipe out the inbox for the entire anonymous
    # population (independent review, 2nd pass).
    reader = reader_session if reader_session not in (None, "", "unknown") else None
    excluded = {*(str(i) for i in exclude_ids), *appended}
    pattern = glob_subtree_pattern(scope) if subtree else None
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'served'"
        )
        # A store no writable open has given `served` yet (a read-only open
        # never adds it) answers from the rows: slow, never different.
        served = "" if cur.fetchone() is not None else _SERVED_FROM_ROWS
        cur.execute(
            "WITH "
            + served
            + """retired AS (
                SELECT capsule_id FROM tiers WHERE tier <> 'active'
                UNION SELECT r.to_id FROM relations r
                       WHERE r.kind IN ('supersedes', 'falsifies') AND """
            + live_relation_sql()
            + """
                UNION SELECT capsule_id FROM tombstones
            ), cands AS (
            SELECT c.seq, c.id
            FROM capsules c
            LEFT JOIN classifications cl ON cl.capsule_id = c.id
            WHERE c.canonical_json IS NOT NULL
              -- The inbox delivers EXACTLY the population that the delivery
              -- census measures: knowledge. The work plan has its own
              -- consumer (verdict, outcome) and delivering it here would burn
              -- slots without moving the target — measured in the live store on
              -- 2026-09-15, the oldest candidates in `pynott` were `task` and
              -- `dod`. Two population definitions that disagree is the defect
              -- this campaign was caught committing twice in the previous one.
              AND coalesce(cl.kind, 'unclassified') NOT IN (SELECT value FROM json_each(?))
              AND (c.project_id = ?
                   OR (? IS NOT NULL
                       AND c.project_id GLOB ?))
              AND c.id NOT IN (SELECT value FROM json_each(?))
              AND (? IS NULL
                   OR NOT EXISTS (SELECT 1 FROM capture_context cc
                                  WHERE cc.capsule_id = c.id
                                    AND cc.session_id = ?))
              -- Served on any surface: a key lookup in the table the
              -- `read_traffic` triggers keep (`SERVED_DDL`).
              AND NOT EXISTS (SELECT 1 FROM served s
                              WHERE s.capsule_id = c.id AND s.n > 0)
              AND NOT EXISTS (SELECT 1 FROM usage u WHERE u.capsule_id = c.id)
              AND NOT EXISTS (SELECT 1 FROM retired r WHERE r.capsule_id = c.id)
            ), picked AS (
                SELECT seq, id FROM (SELECT seq, id FROM cands ORDER BY seq ASC LIMIT ?)
                UNION
                SELECT seq, id FROM (SELECT seq, id FROM cands ORDER BY seq DESC LIMIT ?)
            )
            -- Bytes (`canonical_json`) enter only for the picked ones: the
            -- candidates CTE carries `seq` and `id` (which is `text`), not the
            -- content of the ~2800 in the queue. MEASURED against the live store on
            -- 2026-09-15 (queue of 2849, `pynott` scope, subtree): 77–88 ms
            -- across 5 runs, 20 rows per call. The final `LIMIT` is a belt:
            -- both ends already deliver `oldest + newest` because
            -- `capture_context`, `capsule_handles`, and `classifications` are
            -- all PK by `capsule_id` (1:1). The belt for the day when one of them
            -- gains rows per alias is `seen` in the loop below, not a final `LIMIT`:
            -- `LIMIT` over `seq ASC` would cut from the NEW end, i.e., exactly the
            -- property that this form adds (independent review, 2nd pass). It has no
            -- falsifier as long as the three JOINs are 1:1 — removing it leaves
            -- the suite green, which is why it is a belt and not a rule.
            SELECT p.seq, p.id, h.prefix, h.slug, cl.kind, c.canonical_json
            FROM picked p
            JOIN capsules c ON c.id = p.id
            LEFT JOIN capsule_handles h ON h.capsule_id = p.id
            LEFT JOIN classifications cl ON cl.capsule_id = p.id
            ORDER BY p.seq ASC
            """,
            (
                list(WORK_KINDS),
                scope,
                pattern,
                pattern,
                sorted(excluded),
                reader,
                reader,
                oldest,
                newest,
            ),
        )
        rows = cur.fetchall()
    out: list[InboxRow] = []
    seen: set[str] = set()
    for seq, cid, prefix, slug, kind, canonical in rows:
        if str(cid) in seen:
            continue
        seen.add(str(cid))
        capsule = Capsule.from_canonical_json(str(canonical))
        tainted = capsule.instruction_taint
        # The `slug` of a handle is `slugify(content)` — the first line of the
        # CONTENT. Removing the headline and sending the handle would let the same
        # bytes arrive via another field: a tainted capsule delivered
        # "fact-ignore-all-previous-instructions-and-…" to every session, in the
        # only tool that the contract makes mandatory. Caught by
        # independent review. For tainted, the BARE id is sent, and
        # `nmemory_capsule_show` is the explicit gateway to the bytes.
        handle = (
            str(cid)
            if tainted
            else (f"{prefix}-{slug}" if prefix and slug else str(cid))
        )
        out.append(
            InboxRow(
                seq=int(seq),
                id=str(cid),
                handle=handle,
                kind=str(kind) if kind is not None else None,
                headline=(
                    None if tainted else short_headline(headline_of(capsule.content))
                ),
                instruction_taint=tainted,
            )
        )
    return out
