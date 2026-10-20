"""The review verdicts on staged proposals, and the cheap model's System1 observations."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final

from ..capsule import format_rfc3339
from ..database import writes
from .base import Repository, StoreError


class ReviewRepository(Repository):
    """Owns review_events, jev_observations."""

    #: U5: the questions that the cheap typed model can OBSERVE. CLOSED
    #: set, and refusal instead of orphan row: an unknown question records
    #: an observation that no consumer reads, and eligibility below
    #: would return zero forever — reads as "never asked".
    SYSTEM1_QUESTIONS: Final = ("curator", "classify", "taint", "staged", "scope")

    @writes
    def append_review_event(
        self, capsule_id: str, verdict: str, reason: str, actor: str, now: datetime
    ) -> None:
        at_str = format_rfc3339(now)
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM review_events")
            row = cur.fetchone()
            assert row is not None
            seq = int(row[0])
            cur.execute(
                "INSERT INTO review_events (seq, capsule_id, verdict, reason, actor, at) VALUES (?, ?, ?, ?, ?, ?)",
                (seq, capsule_id, verdict, reason, actor, at_str),
            )

    def review_verdict(
        self, capsule_id: str, as_of: datetime | None = None
    ) -> str | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            if as_of is None:
                cur.execute(
                    "SELECT verdict FROM review_events WHERE capsule_id = ? ORDER BY seq DESC LIMIT 1",
                    (capsule_id,),
                )
            else:
                cur.execute(
                    "SELECT verdict FROM review_events WHERE capsule_id = ? AND at <= ? ORDER BY seq DESC LIMIT 1",
                    (capsule_id, format_rfc3339(as_of)),
                )
            row = cur.fetchone()
            return str(row[0]) if row else None

    def list_review_fenced(self) -> list[str]:
        """Ids whose current review verdict is NOT `ratified` (b2).

        A staged proposal is intent: the digest counts it in the `staged` section and
        excludes it from all truth surfaces.
        """
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT capsule_id FROM (
                    SELECT capsule_id, verdict
                    FROM review_events
                    WHERE seq IN (SELECT max(seq) FROM review_events GROUP BY capsule_id)
                ) latest
                WHERE verdict <> 'ratified'
                ORDER BY capsule_id ASC
                """
            )
            return [str(r[0]) for r in cur.fetchall()]

    def review_verdicts(self, capsule_ids: Sequence[str]) -> dict[str, str]:
        """Read the latest verdict for each candidate in one indexed query."""
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT capsule_id, verdict FROM review_events WHERE seq IN ("
                " SELECT max(seq) FROM review_events"
                " WHERE capsule_id IN (SELECT value FROM json_each(?))"
                " GROUP BY capsule_id)",
                (capsule_ids,),
            )
            return {str(row[0]): str(row[1]) for row in cur.fetchall()}

    def _system1_question(self, question: str) -> None:
        if question not in self.SYSTEM1_QUESTIONS:
            raise StoreError(
                f"jev_observations: question {question!r} is not a system1 question; "
                f"the set is {list(self.SYSTEM1_QUESTIONS)}"
            )

    def record_system1_observation(
        self,
        capsule_id: str,
        *,
        question: str,
        subject: str,
        choice: str,
        p: float,
        confidence: float | None,
        model: str,
        now: datetime,
    ) -> None:
        """U5: an observation from the cheap typed model, append-only.

        DEDICATED TABLE (`jev_observations`), never `verification_events`: the
        cheap model OBSERVES and ORDERS, never signs a verdict. The mechanical
        reason is in the DDL (`database.py`) — `curator_candidates` takes the
        latest event of ANY kind before filtering `anchor`, and
        `standing_of` classifies by `result` over a closed set.

        Bookkeeping like `record_verification`: triage running every 60 s does
        not advance the canonical generation.
        """
        self._system1_question(question)
        if not 0.0 <= p <= 1.0:
            raise StoreError(
                f"jev_observations: p={p!r} outside 0..1; not a probability"
            )
        with self.db.writing() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jev_observations "
                "(capsule_id, at, question, subject, choice, p, confidence, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    capsule_id,
                    format_rfc3339(now),
                    question,
                    subject,
                    choice,
                    p,
                    confidence,
                    model,
                ),
            )

    def system1_observed_subjects(
        self, question: str, subjects: Sequence[str]
    ) -> set[str]:
        """U5: the subset of `subjects` that ALREADY has an observation for this question.

        This is eligibility: each capsule is asked once PER CONTENT (the
        `subject` is the anchor judgment or content sha256), rather than every
        60 seconds until budget runs out.
        """
        self._system1_question(question)
        wanted = list(subjects)
        if not wanted:
            return set()
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT subject FROM jev_observations "
                "WHERE question = ? AND subject IN (SELECT value FROM json_each(?))",
                (question, wanted),
            )
            return {str(r[0]) for r in cur.fetchall()}

    def latest_system1_choices(
        self, question: str, pairs: Sequence[tuple[str, str]]
    ) -> dict[str, str]:
        """The latest choice for this question by capsule, where its subject still
        matches: `capsule_id -> choice`. Any model's answer counts."""
        self._system1_question(question)
        wanted = set(pairs)
        if not wanted:
            return {}
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT capsule_id, subject, choice FROM jev_observations"
                " WHERE seq IN (SELECT max(seq) FROM jev_observations"
                " WHERE question = ? AND capsule_id IN (SELECT value FROM json_each(?))"
                " GROUP BY capsule_id, subject)",
                (question, sorted({c for c, _ in wanted})),
            )
            return {
                str(r[0]): str(r[2])
                for r in cur.fetchall()
                if (str(r[0]), str(r[1])) in wanted
            }

    def system1_observations_for(
        self, question: str, pairs: Sequence[tuple[str, str]], *, model: str
    ) -> dict[tuple[str, str], tuple[str, float, float | None]]:
        """The LATEST observation for this question by `(capsule_id, subject)`, only
        THIS model's: `(capsule_id, subject) -> (choice, p, confidence)`.

        This is reuse: curator does not re-ask what this model already answered
        for the same capsule and same judgment — measured on 2026-09-18, the loop
        re-asked the same 33 every 60 s. The key takes the CAPSULE and not just the
        `subject` because two capsules anchored on the same line share the
        judgment (`dead_anchor at src/a.py:9`) and the question is about the claim of
        each one; keying only by subject made B inherit A's response (review of
        #660). The fence is by model because `laya-nott` changes versions and the old
        response does not suffice.
        """
        self._system1_question(question)
        wanted = set(pairs)
        if not wanted:
            return {}
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT capsule_id, subject, choice, p, confidence "
                "FROM jev_observations WHERE seq IN ("
                " SELECT max(seq) FROM jev_observations"
                " WHERE question = ? AND model = ?"
                " AND capsule_id IN (SELECT value FROM json_each(?))"
                " AND subject IN (SELECT value FROM json_each(?))"
                " GROUP BY capsule_id, subject)",
                (
                    question,
                    model,
                    sorted({c for c, _ in wanted}),
                    sorted({s for _, s in wanted}),
                ),
            )
            return {
                (str(r[0]), str(r[1])): (
                    str(r[2]),
                    float(r[3]),
                    None if r[4] is None else float(r[4]),
                )
                for r in cur.fetchall()
                if (str(r[0]), str(r[1])) in wanted
            }
