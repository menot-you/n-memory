"""Read-only, project-fenced pages for the private documentation portal."""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from datetime import datetime

from .capsule import headline_of
from .consolidate import Tier
from .queries.standing import standing_of
from .relation import closed_task_ids
from .repositories.capsules import ListFilter, StoredCapsule
from .store import Store

PORTAL_KINDS = ("decision", "constraint", "plan", "epic")
EXPLORER_URL = "https://viz.nott.ing/"


@dataclass(frozen=True)
class PortalExport:
    project: str
    selected: tuple[tuple[str, str, str], ...]  # id, project, kind
    pages: dict[str, str]
    digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "project": self.project,
            "selected": [
                {"id": cid, "project": project, "kind": kind}
                for cid, project, kind in self.selected
            ],
            "pages": self.pages,
            "digest": self.digest,
        }


def _page(stored: StoredCapsule, kind: str) -> str:
    cid = stored.id
    title = html.escape(headline_of(stored.capsule.content), quote=True)
    content = html.escape(stored.capsule.content, quote=True)
    return (
        f"<h1>{title}</h1>\n\n"
        f"**{cid}** · {kind} · [Open nMEMORY explorer]({EXPLORER_URL})\n\n"
        "> Generated view of stored data. The capsule in nMEMORY is the source.\n\n"
        f'<pre style="white-space:pre-wrap">{content}</pre>\n'
    )


def render_portal(store: Store, project: str, now: datetime) -> PortalExport:
    """Select the same live standing and closing edges as nMEMORY's readers.

    Exact project identity is required. A subtree would expose another project's
    capsules if a child scope were later introduced. One SQLite read transaction
    holds the selection and its staged/standing edges at one snapshot.
    """
    with store.db.tool_boundary():
        return _render_portal(store, project, now)


def _render_portal(store: Store, project: str, now: datetime) -> PortalExport:
    if not project or project.strip() != project:
        raise ValueError("portal project must be a non-empty exact project id")

    candidates: list[tuple[StoredCapsule, str]] = []
    staged = set(store.reviews.list_review_fenced())
    for stored in store.capsules.list(ListFilter(project_id=project)):
        cid = stored.id
        if cid in staged or store.tiers.get_tier(cid) != Tier.ACTIVE:
            continue
        classification = store.classifications.get_classification(cid)
        kind = classification.kind.value if classification else ""
        if kind not in PORTAL_KINDS or stored.capsule.freshness.valid_from > now:
            continue
        candidates.append((stored, kind))

    standing = standing_of(store.db, [row.id for row, _ in candidates], now=now)
    closed = closed_task_ids(store.relations.list_relations())
    selected = [
        (row, kind)
        for row, kind in candidates
        if standing.get(row.id, {}).get("state") == "vale"
        and (kind not in ("plan", "epic") or row.id not in closed)
    ]

    pages: dict[str, str] = {}
    index = [
        "# nMEMORY\n\n",
        "> Generated view. nMEMORY is the source of these decisions, constraints, plans and epics.\n\n",
        f"[Open nMEMORY explorer]({EXPLORER_URL})\n\n",
    ]
    for kind in PORTAL_KINDS:
        rows = [(row, k) for row, k in selected if k == kind]
        index.append(f"## {kind.title()}s ({len(rows)})\n\n")
        for row, _ in rows:
            title = html.escape(headline_of(row.capsule.content), quote=True)
            index.append(f'- <a href="{row.id}/">{row.id} — {title}</a>\n')
            pages[f"{row.id}.md"] = _page(row, kind)
        index.append("\n")
    pages["index.md"] = "".join(index)
    selected_rows = tuple(
        (row.id, row.capsule.scope.project_id, kind) for row, kind in selected
    )
    digest_input = json.dumps(
        {"project": project, "selected": selected_rows, "pages": pages},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return PortalExport(
        project, selected_rows, pages, hashlib.sha256(digest_input).hexdigest()
    )
