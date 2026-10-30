"""Archiving read a recall signal blind to two of three read surfaces.

`consolidate.archive_deadline` archives a superseded capsule 30 days after
creation when `recall_count == 0`. That count came from `nmemory.usage`, which
has ONE writer -- `nmemory_capsule_retrieve` (`retrieve.py`, one call site). So a
superseded capsule that `nmemory_context_show` serves every day counted as never
recalled and fell through the trapdoor.

Measured on the live store the day this was written: 444 distinct capsules had
been served by `context`/`bootstrap`, and `usage` saw 373 of them as never
recalled -- 84% invisible to the signal the trigger consults. The overlap with
the 480 already queued for archiving was zero, so nobody had stepped on it yet.
That is a trapdoor, not a bug that waits to be reported.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from storefile import fresh_store_path

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.consolidate import (
    ARCHIVE_AFTER_DAYS,
    ConsolidationRecord,
    Tier,
    archive_deadline,
)
from nmemory.queries.sidecars import digest_sidecars
from nmemory.store import Store


def _capsule(store: Store, content: str) -> str:
    """A live capsule, so the sidecar consolidation reads carries its row."""
    cid, _created = store.append(
        Capsule(
            content=content,
            provenance=Provenance(
                source="test",
                anchor="test:1",
                source_hash=sha256_hex(content.encode("utf-8")),
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=datetime.now(UTC)),
            scope=Scope(project_id="pynott"),
            authority_class=AuthorityClass.OBSERVED_FACT,
            instruction_taint=False,
        ),
        datetime.now(UTC),
    )
    return cid


def _recall_count(store: Store, cid: str) -> int:
    """The recall count the protective tier moves hand `archive_deadline`: its sidecar."""
    return digest_sidecars(store.db)[cid].recall_count


def _record(store: Store, cid: str, created: datetime) -> ConsolidationRecord:
    return ConsolidationRecord(
        id=cid,
        seq=1,
        source_hash="h",
        content="c",
        instruction_taint=False,
        authority_class=AuthorityClass.OBSERVED_FACT,
        valid_to=None,
        created_at=created,
        recall_count=_recall_count(store, cid),
        is_superseded=True,
        tier=Tier.ACTIVE,
        pinned=False,
    )


def test_a_capsule_context_serves_is_not_archived_as_never_recalled() -> None:
    url = fresh_store_path("archsurf")
    store = Store.open(url)
    old = datetime.now(UTC) - timedelta(days=ARCHIVE_AFTER_DAYS + 1)
    unread = _capsule(store, "a capsule nothing serves")
    served = _capsule(store, "a capsule context serves")
    prefix = _capsule(store, "a capsule whose id opens a served id")
    suffix = _capsule(store, "a capsule whose id closes a served id")

    # The control: nothing served it, so the trapdoor is open under it.
    assert archive_deadline(_record(store, unread, old)) is not None, (
        "a superseded, never-served capsule past the window is still archivable "
        "-- without this the test would pass on a broken `archive_deadline`"
    )

    store.db.record_read_traffic(
        surface="context",
        session_id=None,
        terms="whatever",
        served_ids=["cap-other", served, f"{prefix}0", f"x{suffix}"],
        project_id="pynott",
        project_prefix=None,
        outcome=None,
        now=datetime.now(UTC),
    )

    assert _recall_count(store, served) == 1, "a context serving IS a recall"
    assert archive_deadline(_record(store, served, old)) is None

    assert _recall_count(store, prefix) == 0
    assert _recall_count(store, suffix) == 0


def test_orientation_does_not_count_as_a_recall() -> None:
    """The half the union must NOT swallow, and the existing suite caught it.

    The bootstrap pack wrote a `read_traffic` row naming everything it packed, and
    the store keeps those rows. Counting them would have the pack inflate its own
    recall counts, and every capsule orientation ever displayed would read as
    recalled. A first version of this change unioned every surface but `retrieve`
    and broke exactly that invariant.

    `context` is a deliberate read and counts; `bootstrap` is orientation and
    does not; `retrieve` is already in `nmemory.usage`.
    """
    url = fresh_store_path("archsurf_orient")
    store = Store.open(url)
    shown = _capsule(store, "a capsule every surface shows")

    # U1 (nmemory-curator): every new serving surface joins this loop. The
    # ranking counters are an allowlist; if one of these ever reaches the
    # sidecar's recall count, `archive_deadline` stops firing for anything the surface
    # showed -- the silent absorb a denylist causes.
    for surface in ("bootstrap", "retrieve", "get", "list", "digest", "inbox"):
        store.db.record_read_traffic(
            surface=surface,
            session_id=None,
            terms="orientation",
            served_ids=[shown],
            project_id="pynott",
            project_prefix=None,
            outcome=None,
            now=datetime.now(UTC),
        )
    assert _recall_count(store, shown) == 0, (
        "neither orientation nor a retrieve row already counted in `usage` may "
        "be added a second time"
    )

    store.db.record_read_traffic(
        surface="context",
        session_id=None,
        terms="a deliberate read",
        served_ids=[shown],
        project_id="pynott",
        project_prefix=None,
        outcome=None,
        now=datetime.now(UTC),
    )
    assert _recall_count(store, shown) == 1
