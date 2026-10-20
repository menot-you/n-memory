"""The daily hygiene meter: misses, context traffic and the pre-traffic cohort."""

from __future__ import annotations

from typing import Any

from ..database import Database

MEASURE_SQL = """
SELECT
  (SELECT count(*) FROM recall_misses
    WHERE at >= :day AND outcome = 'abstain'),
  (SELECT count(*) FROM recall_misses
    WHERE at >= :day AND outcome = 'missing_evidence'),
  (SELECT count(*) FROM read_traffic
    WHERE surface = 'context' AND at >= :week),
  (SELECT count(*) FROM read_traffic
    WHERE surface = 'context' AND at >= :week AND served_count = 0),
  (SELECT count(*) FROM read_traffic
    WHERE surface = 'context' AND at >= :prior AND at < :week),
  (SELECT count(*) FROM read_traffic
    WHERE surface = 'context' AND at >= :prior AND at < :week
      AND served_count = 0),
  (SELECT count(*) FROM capsules
    WHERE canonical_json IS NOT NULL AND created_at < :epoch)
"""


def measure_counts(db: Database, params: dict[str, str]) -> tuple[Any, ...]:
    """The seven counts of `MEASURE_SQL`, one snapshot."""
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(MEASURE_SQL, params)
        row = cur.fetchone()
        assert row is not None
        return tuple(row)
