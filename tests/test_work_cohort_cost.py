"""A work-list walk does not carry every capsule's payload through the cohort."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    headline_of,
    sha256_hex,
)
from nmemory.queries.inbox import short_headline
from nmemory.queries.work import _work_cohort, open_work, work_page
from nmemory.store import Store

PROJECT = "work-cohort-cost"
NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
COUNT = 3000
PADDING = "x" * 1000


def seed(store: Store) -> list[dict[str, Any]]:
    """COUNT task/dod capsules of about 1 KB; every third one is closed."""
    expected: list[dict[str, Any]] = []
    with store.db.writing() as conn, conn.cursor() as cur:
        for n in range(1, COUNT + 1):
            content = f"Cohort work item {n} must stay open.\n{PADDING}"
            tainted = n % 97 == 0
            capsule = Capsule(
                content=content,
                provenance=Provenance(
                    source="test:work-cohort",
                    anchor=f"code/item.py:{n}",
                    source_hash=sha256_hex(content.encode()),
                ),
                confidence=0.9,
                freshness=Freshness(valid_from=NOW),
                scope=Scope(project_id=PROJECT),
                authority_class=AuthorityClass.USER_STATED,
                instruction_taint=tainted,
            )
            cid = f"cap-{n}"
            kind = "dod" if n % 5 == 0 else "task"
            cur.execute(
                "INSERT INTO capsules (seq, id, canonical_json, created_at, "
                "source_hash, project_id, authority_class, valid_from) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    n,
                    cid,
                    capsule.to_canonical_json(),
                    NOW.isoformat(),
                    capsule.provenance.source_hash,
                    PROJECT,
                    "user_stated",
                    NOW.isoformat(),
                ),
            )
            cur.execute(
                "INSERT INTO classifications (capsule_id, kind, scope, at) "
                "VALUES (?, ?, 'project', ?)",
                (cid, kind, NOW.isoformat()),
            )
            if n % 2 == 0:
                cur.execute(
                    "INSERT INTO capsule_handles (capsule_id, prefix, slug) "
                    "VALUES (?, 'work', ?)",
                    (cid, f"item-{n}"),
                )
            if n % 3 == 0:
                cur.execute(
                    "INSERT INTO relations (kind, from_id, to_id, at, origin) "
                    "VALUES ('witnesses', 'cap-evidence', ?, ?, 'test')",
                    (cid, NOW.isoformat()),
                )
                continue
            expected.append(
                {
                    "id": cid,
                    "handle": cid if tainted or n % 2 else f"work-item-{n}",
                    "kind": kind,
                    "age_days": 0,
                    **(
                        {"instruction_taint": True}
                        if tainted
                        else {"headline": short_headline(headline_of(content))}
                    ),
                    **({} if tainted else {"content": content}),
                }
            )
    return expected


def walk(store: Store, **options: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        page, cursor = work_page(
            store.db,
            PROJECT,
            subtree=False,
            limit=100,
            cursor=cursor,
            now=NOW,
            **options,
        )
        rows.extend(row.to_dict() for row in page)
        if cursor is None:
            return rows


def write_bytes() -> int | None:
    io = Path("/proc/self/io")
    if not io.exists():
        return None
    for line in io.read_text().splitlines():
        if line.startswith("write_bytes:"):
            return int(line.split()[1])
    return None


def test_work_walk_keeps_payload_out_of_the_cohort() -> None:
    store = Store.open_disposable_test_store()
    expected = seed(store)
    assert len(expected) == 2000

    before = write_bytes()
    rows = walk(store, include_content=True)
    plain = walk(store)
    work = open_work(store.db, PROJECT, subtree=False, now=NOW)
    after = write_bytes()

    # The results the cure must keep, byte for byte.
    assert rows == expected
    assert plain == [
        {k: v for k, v in row.items() if k != "content"} for row in expected
    ]
    assert [r.to_dict() for r in work.rows] == plain[:5]
    assert (work.task_open, work.dod_open) == (
        sum(r["kind"] == "task" for r in expected),
        sum(r["kind"] == "dod" for r in expected),
    )
    prefixed = walk(store, headline_prefix="Cohort work item 1")
    assert [r["id"] for r in prefixed] == [
        r["id"]
        for r in expected
        if r.get("headline", "").startswith("Cohort work item 1")
    ]

    # The cohort every page and every context read rebuilds holds the ranking
    # columns only: before the cure it carried each capsule's canonical JSON
    # (about 3 MB here, 27 MB of temp B-tree spill per page on the live store).
    cohort_sql, args = _work_cohort(PROJECT, False)
    with store.db.connection() as conn, conn.cursor() as cur:
        cur.execute(cohort_sql + "SELECT * FROM cohort", args)
        held = sum(len(str(value).encode()) for row in cur for value in row)
    assert held < 100 * COUNT, f"the work cohort holds {held} bytes for {COUNT} rows"

    if before is None or after is None:
        pytest.skip("/proc/self/io is absent; the cohort byte bound above still held")
    # Measured here: 219 MB before the cure, about 1.4 MB after (the narrow
    # sort still spills about 32 KB a page).
    assert after - before < 4 << 20, (
        f"a work walk wrote {after - before} bytes of temporary sort spill"
    )
