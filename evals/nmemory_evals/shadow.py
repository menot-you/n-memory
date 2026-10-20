"""U5 — the adversarial slice of the shadow store, with truth by CONSTRUCTION.

The fence that decides the shape: `curator_candidates` only enqueues capsules
whose most recent anchor event is `dead_anchor`/`moved_anchor`, and
`judge_anchor` (`nmemory/anchors.py:87-112`) COUNTS LINES — it never compares
content. So an in-place edit is invisible to the mechanical judgment, and that
is why cell D exists: it is the COVERAGE ceiling of (b), reported, never
hidden.

All the slice's truth reduces to one mechanical predicate — `line_text_present`:
is the ORIGINAL text of the anchored line still present in the fixture's file?

| cell   | construction                            | judge_anchor | truth   |
|--------|-----------------------------------------|--------------|---------|
| A      | file deleted                            | dead_anchor  | false   |
| B      | line moved to the top, rest cut        | moved_anchor | true    |
| C      | untouched                               | pass         | true    |
| D      | line rewritten, same count             | pass         | false   |

CELL A NO LONGER ENTERS THE TREATMENT, and this is design, not defect. Since
2026-09-18 the worker's policy (`curator.DEFAULT_VERDICTS`) excludes
`dead_anchor`: the file does not exist, the excerpt is `<file missing>`, and
`_prepare`'s guard refuses before paying. Cell A was, moreover, the artefact
that produced the false «7/7 correct» — it deleted a file that CARRIED the
fact, so «false» was certain by construction. A's delta is 0 **by
construction** and `measure` does not read it as a verdict of (b); what (b)
measures is B and D.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nmemory.anchors import audit_all, confined_file, parse_code_anchor
from nmemory.queries.curator import curator_candidates
from nmemory.repositories.capsules import ListFilter
from nmemory.store import Store
from nott_database import RefusedError, directory_bind

from nmemory_evals.driver import DriverError, validate_disposable_store_dir
from nmemory_evals.path import directory_check, directory_open, file_read, file_replace

CELLS = ("A", "B", "C", "D")

ANCHORED_KINDS = frozenset(
    {"fact", "failure_pattern", "procedure", "doc", "capability"}
)

# The text that replaces the line in cell D. It does not contain the original
# text, or the label's predicate would say «present» and D would stop measuring
# the ceiling.
CELL_D_LINE = "# nott-u5-celula-D: linha substituida, mesma contagem"


@dataclass(frozen=True)
class SliceRow:
    """A capsule of the slice and the cell assigned to it."""

    capsule_id: str
    kind: str
    anchor_path: str
    anchor_line: int | None
    cell: str


@dataclass(frozen=True)
class FixtureRow:
    """A manifest row: the label, and the text that proves it."""

    capsule_id: str
    cell: str
    anchor_path: str
    anchor_line: int
    line_text: str
    truth: bool


def line_text_present(root: Path, row: FixtureRow) -> bool:
    """The label's predicate. No model: the file exists and the line is there."""
    target = root / row.anchor_path
    if not target.is_file():
        return False
    return (
        row.line_text
        in target.read_text(encoding="utf-8", errors="replace").splitlines()
    )


def build_fixture(
    source_root: Path,
    dest_root: Path,
    rows: list[SliceRow],
    *,
    falsify: bool = True,
    _directory: int | None = None,
) -> list[FixtureRow]:
    """Create a fresh private fixture and return its labels.

    Only the files the slice anchors are copied: the fixture is the root
    `audit_all` sweeps, and a smaller tree is a cheaper sweep.

    `falsify=False` materializes the INTACT tree, with the same manifest and
    the same labels. It exists because cell D — an in-place edit, same line
    count — is only detectable by comparing the line with what it said BEFORE
    (`normalized_line_digest`, PR #570), and comparing requires a sweep prior
    to the falsification. Without it the first sweep returns `baseline` for
    everything, cell D is born invisible, and (b)'s ceiling becomes a limit of
    the INSTRUMENT instead of a measurement.
    """
    _reject_invalid(rows)
    source_root = source_root.resolve()
    manifest: list[FixtureRow] = []
    try:
        with ExitStack() as stack:
            destination = (
                stack.enter_context(_fixture_directory(dest_root))
                if _directory is None
                else _directory
            )
            source = stack.enter_context(directory_open(source_root))
            for row in rows:
                assert row.anchor_line is not None  # _reject_invalid already ensured it
                src = confined_file(source_root, row.anchor_path)
                if src is None:
                    raise ShadowRefusal(
                        f"source file is not confined: {row.anchor_path}"
                    )
                content = file_read(source, src.relative_to(source_root))
                lines = content.decode("utf-8", errors="replace").splitlines()
                if row.anchor_line > len(lines):
                    raise ValueError(
                        f"{row.capsule_id}: line {row.anchor_line} is outside "
                        f"{row.anchor_path} in the source tree ({len(lines)} lines)"
                    )
                anchor = Path(row.anchor_path)
                with directory_open(
                    anchor.parent, parent_fd=destination, create=True
                ) as parent:
                    descriptor = os.open(
                        anchor.name,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | os.O_CLOEXEC,
                        0o600,
                        dir_fd=parent,
                    )
                    with os.fdopen(descriptor, "wb") as target:
                        target.write(content)
                manifest.append(
                    FixtureRow(
                        capsule_id=row.capsule_id,
                        cell=row.cell,
                        anchor_path=row.anchor_path,
                        anchor_line=row.anchor_line,
                        line_text=lines[row.anchor_line - 1],
                        truth=row.cell in ("B", "C"),
                    )
                )
            directory_check(dest_root, destination)
            if falsify:
                _damage_fixture(destination, manifest)
    except OSError as err:
        raise ShadowRefusal(f"fixture operation refused: {err}") from err
    return manifest


@contextmanager
def _fixture_directory(path: Path) -> Iterator[int]:
    try:
        validate_disposable_store_dir(path.absolute())
        with directory_open(path, create=True, fresh=True) as descriptor:
            yield descriptor
    except (DriverError, OSError) as err:
        raise ShadowRefusal(f"fixture directory refused: {err}") from err


def _damage_fixture(root: int, manifest: list[FixtureRow]) -> None:
    """Damage only files inside the reserved fixture root."""
    for row in manifest:
        anchor = Path(row.anchor_path)
        with directory_open(anchor.parent, parent_fd=root) as parent:
            content = file_read(parent, Path(anchor.name))
            if row.cell == "A":
                os.unlink(anchor.name, dir_fd=parent)
            elif row.cell == "B":
                file_replace(parent, anchor.name, [row.line_text + "\n"])
            elif row.cell == "D":
                lines = content.decode("utf-8", errors="replace").splitlines()
                lines[row.anchor_line - 1] = CELL_D_LINE
                file_replace(parent, anchor.name, ["\n".join(lines) + "\n"])


def _reject_invalid(rows: list[SliceRow]) -> None:
    seen: dict[str, str] = {}
    for row in rows:
        path = Path(row.anchor_path)
        if path.is_absolute() or ".." in path.parts:
            raise ShadowRefusal(f"anchor path must stay inside the fixture: {path}")
        if row.kind not in ANCHORED_KINDS:
            raise ValueError(
                f"{row.capsule_id}: kind {row.kind!r} does not carry the standing's "
                f"four states; the slice accepts {sorted(ANCHORED_KINDS)}"
            )
        if row.cell not in CELLS:
            raise ValueError(f"{row.capsule_id}: cell {row.cell!r} does not exist")
        if row.anchor_line is None:
            raise ValueError(
                f"{row.capsule_id}: anchor {row.anchor_path!r} without a line — without a "
                "line there is no original text, so there is no mechanical label"
            )
        # Two cells in the same file overlap and both labels start to lie; the
        # slice is per file.
        if str(path) in seen:
            raise ValueError(
                f"{row.capsule_id}: {row.anchor_path} was already assigned to "
                f"{seen[str(path)]} — one cell per file"
            )
        seen[str(path)] = row.capsule_id


def select_slice(
    live: Store,
    *,
    source_root: Path,
    per_cell: int,
    project: str | None = None,
) -> list[SliceRow]:
    """Chooses the slice in the live store. PURE READ: nothing is written here.

    Only a capsule whose anchor is ALIVE in the source tree enters — the cell
    builds the falsehood from a `pass`, and an already dead anchor builds
    nothing. One cell per file, and the ceiling is per (kind, cell) so a slice
    dominated by one kind does not dilute (b).
    """
    quota: Counter[tuple[str, str]] = Counter()
    taken: set[str] = set()
    rows: list[SliceRow] = []
    source_root = source_root.resolve()
    for stored in live.capsules.list(ListFilter(project_id=project)):
        cls = live.classifications.get_classification(stored.id)
        if cls is None or cls.kind.value not in ANCHORED_KINDS:
            continue
        parsed = parse_code_anchor(stored.capsule.provenance.anchor)
        if parsed is None:
            continue
        path, line = parsed
        if line is None or path in taken:
            continue
        target = confined_file(source_root, path)
        if target is None:
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        if line > len(text.splitlines()):
            continue
        cell = next((c for c in CELLS if quota[(cls.kind.value, c)] < per_cell), None)
        if cell is None:
            continue
        quota[(cls.kind.value, cell)] += 1
        taken.add(path)
        rows.append(SliceRow(stored.id, cls.kind.value, path, line, cell))
    return rows


def replicate(
    live: Store, shadow: Store, rows: list[SliceRow], now: datetime
) -> dict[str, str]:
    """Copies the slice into the shadow through the PUBLIC API, and returns the
    id map.

    Edges do NOT travel — and that is deliberate. «Edges removed in both arms»: curation already done in the live store (`falsifies`,
    `supersedes`) must not leak into the control arm, or the control arrives
    already fenced and (b) measures zero. What travels is what the capsule IS:
    canonical bytes and classification.
    """
    mapping: dict[str, str] = {}
    for row in rows:
        stored = live.capsules.get(row.capsule_id)
        if stored is None:
            raise ValueError(
                f"{row.capsule_id}: vanished from the live store between the "
                "slice and the replication — the slice is stale, redo it"
            )
        new_id, _ = shadow.append(stored.capsule, now)
        cls = live.classifications.get_classification(row.capsule_id)
        if cls is not None:
            shadow.set_classification(new_id, cls.kind, cls.scope, now)
        mapping[row.capsule_id] = new_id
    return mapping


@dataclass(frozen=True)
class Observation:
    """What the question surface did with ONE capsule of the slice."""

    capsule_id: str
    kind: str
    cell: str
    truth: bool
    served: bool


def ask(
    store: Store,
    *,
    query: str,
    project: str | None = None,
    anchor_root: str | None = None,
) -> set[str]:
    """Asks the question and returns the ids the answer SERVED as grounded.

    The ruler uses no model: the question is the capsule's own content (term
    lane), and what is measured is whether the id appears in the evidence. An
    absent id is absent whatever the reason — fence, lane, or budget — and that
    is why cell C exists: it is what separates fence from noise.

    `lapsed=True` in BOTH arms, and it is not weakening any fence: the standing
    fence belongs to the product and stays intact in `retrieve.py`. It excludes
    by `caducou` — which is what `audit_all` writes for `dead_anchor`/
    `moved_anchor`/`changed_anchor` — so A, B and D left BEFORE the curator ran
    and both arms came out identical: `served_false_delta` 0 across the board,
    which reads as «(b) failed» when it means «the instrument does not
    measure». With it neutralized equally on both sides, the curator's
    `falsifies` edge goes back to being the only observable difference — which
    is what `run_shadow` promises.
    """
    from nmemory.retrieve import RetrieveParams, retrieve

    answer = retrieve(
        store,
        RetrieveParams(
            query=query,
            project_id=project,
            anchor_root=anchor_root,
            lapsed=True,
            unread_days=None,
        ),
        record_receipt=False,
    )
    return {str(e["id"]) for e in answer.get("evidence", [])}


def measure(observations: list[Observation]) -> dict[str, Counter[str]]:
    """The three metrics of (b), per kind. No judgment: only arithmetic.

    - `served_false`: the capsule is false and was served — what (b) must take
      down.
    - `true_abstain`: it is false and was not served — (b) happening.
    - `false_abstain`: it is TRUE and was not served — the ceiling, the cost of
      the fence.

    The per-kind total comes along, or a count of 0 reads as «perfect» when it
    may mean «no capsule of that kind entered the slice».
    """
    out: dict[str, Counter[str]] = {}
    for obs in observations:
        c = out.setdefault(obs.kind, Counter())
        c["total"] += 1
        c[f"cell_{obs.cell}"] += 1
        if not obs.truth and obs.served:
            outcome = "served_false"
        elif not obs.truth and not obs.served:
            outcome = "true_abstain"
        elif obs.truth and not obs.served:
            outcome = "false_abstain"
        else:
            outcome = "true_served"
        c[outcome] += 1
        # The PER-CELL outcome is what tests the prediction written before the
        # run: `false_abstain` must concentrate in cell B, where `_excerpt`
        # hands the model `<empty file>`. Without this count the prediction is
        # not falsifiable.
        c[f"{outcome}@{obs.cell}"] += 1
    return out


@dataclass(frozen=True)
class Report:
    """The pair of arms, and the delta between them. THIS object is (b)."""

    control: dict[str, Counter[str]]
    treatment: dict[str, Counter[str]]
    manifest: list[FixtureRow]

    def served_false_delta(self) -> dict[str, int]:
        """How much (b) took down, per kind. Negative is a regression."""
        return {
            kind: self.control[kind]["served_false"]
            - self.treatment.get(kind, Counter())["served_false"]
            for kind in self.control
        }


def run_shadow(
    live: Store,
    shadow: Store,
    *,
    source_root: Path,
    dest_root: Path,
    per_cell: int,
    now: datetime,
    cycle: Callable[[], None],
    project: str,
) -> Report:
    """The before/after pair in ONE shadow store: same bytes, same questions.

    `cycle` is the only point that spends a model — and it is an argument, not
    an import, for two reasons: the production run passes `CuratorWorker.cycle`,
    and the test passes a deterministic fence, which makes the whole pipeline
    provable without any provider.

    `audit_all` OVER THE FIXTURE runs before both arms, and it is not optional:
    it is what writes `dead_anchor`/`moved_anchor` and therefore what fills
    `curator_candidates`. Without it the queue is born empty, the treatment
    equals the control, and a delta 0 would read as «(b) failed» when it means
    «the pipeline did not run». It runs in BOTH arms by construction — it
    precedes both —, and only the curator's fence differs between them.

    `retrieve()` came to exclude TWO things: `falsifies` and the standing
    `caducou` (`retrieve.py:915`). The second precedes the curator and would
    fall on A, B and D in both arms — so `ask` neutralizes it equally in both
    (`lapsed=True`, see there). `falsifies` remains the only observable
    difference between the arms, and the control is honest.
    """
    rows = select_slice(
        live, source_root=source_root, per_cell=per_cell, project=project
    )
    mapping = replicate(live, shadow, rows, now)
    # TWO sweeps, and the first is over the INTACT tree. Cell D is an in-place
    # edit with the same line count, so it is only detectable by comparing the
    # line with what it said before — and there was no before: the first sweep
    # returned `baseline` for everything and cell D never entered
    # `curator_candidates`. The 6 → 6 ceiling this unit reported on 2026-09-16
    # was the INSTRUMENT's, not the product's.
    with _fixture_directory(dest_root) as directory:
        manifest = build_fixture(
            source_root, dest_root, rows, falsify=False, _directory=directory
        )
        directory_check(dest_root, directory)
        audit_all(shadow, dest_root, now=now, record=True, project=project)
        directory_check(dest_root, directory)
        _damage_fixture(directory, manifest)
        directory_check(dest_root, directory)
        audit_all(shadow, dest_root, now=now, record=True, project=project)
    truth = {m.capsule_id: m.truth for m in manifest}
    kinds = {r.capsule_id: r.kind for r in rows}
    cells = {r.capsule_id: r.cell for r in rows}

    def arm() -> dict[str, Counter[str]]:
        seen: list[Observation] = []
        for row in rows:
            stored = shadow.capsules.get(mapping[row.capsule_id])
            assert stored is not None
            served = mapping[row.capsule_id] in ask(
                shadow,
                query=stored.capsule.content,
                project=project,
                anchor_root=str(dest_root),
            )
            seen.append(
                Observation(
                    capsule_id=row.capsule_id,
                    kind=kinds[row.capsule_id],
                    cell=cells[row.capsule_id],
                    truth=truth[row.capsule_id],
                    served=served,
                )
            )
        return measure(seen)

    control = arm()
    cycle()
    return Report(control=control, treatment=arm(), manifest=manifest)


class ShadowRefusal(Exception):
    """Refuse unsafe paths or missing benchmark prerequisites with D18 exit 2."""


@contextmanager
def open_shadow(path: Path | str) -> Iterator[Store]:
    """The shadow is ALWAYS disposable: a fresh store file under /var/tmp.

    Its directory must be a /var/tmp subdirectory outside the live store.
    Existing store files and sidecars remain unchanged.
    """
    from nmemory.database import create_store

    target = Path(path)
    try:
        validate_disposable_store_dir(target.parent)
    except DriverError as err:
        raise ShadowRefusal(f"shadow store refused: {err}") from err
    try:
        with directory_open(target.parent, create=True) as descriptor:
            target = directory_bind(descriptor) / target.name
            for suffix in ("", "-wal", "-shm", "-journal", "-write.lock"):
                candidate = Path(f"{target}{suffix}")
                if candidate.exists() or candidate.is_symlink():
                    raise ShadowRefusal(
                        f"shadow store path already exists: {candidate}"
                    )
            create_store(target)
            yield Store.open(target)
    except (RefusedError, OSError) as err:
        raise ShadowRefusal(f"cannot create a fresh shadow store: {err}") from err


def curator_cycle(shadow: Store, *, root: Path, project: str, budget: int) -> int:
    """The REAL cycle, with three refusals before letting a delta through.

    The configuration question is the CONSUMER's (`CuratorClient.is_configured`),
    never just `has_provider()`. Measured on 2026-09-16: `has_provider()` said
    True with the `NOTT_NMEMORY_CURATOR` switch off, `cycle()` made zero calls,
    and the whole run came out with `served_false_delta` 0 in all kinds —
    indistinguishable from «(b) failed». Asking what the consumer asks is what
    makes the refusal true.
    """
    from nmemory.curator import DEFAULT_VERDICTS, CuratorClient, CuratorWorker

    if not CuratorClient().is_configured():
        raise ShadowRefusal(
            "the curator is not configured: it depends on NOTT_NMEMORY_CURATOR!=0 "
            "(the owner's switch), the `claude` binary on PATH and the provider "
            "of the owner's law (PROVIDER=zai with ZAI_API_KEY/ZAI_BASE_URL). Without it "
            "the treatment equals the control, and a delta of 0 reads as «(b) failed» "
            "when it means «the curator did not run»"
        )
    # The SAME fence both times. Counting the queue with the store's whole
    # vocabulary and then building the worker with the (narrower) POLICY gave
    # `queued=N` with `calls=0` on a fixture of only dead anchors, and the
    # refusal came out with the wrong message — «the child did not start» when
    # the design was what excluded the queue. Cell A was unreachable by
    # construction.
    queued = len(
        curator_candidates(
            shadow.db, project=project, now=datetime.now(UTC), verdicts=DEFAULT_VERDICTS
        )
    )
    if queued == 0:
        raise ShadowRefusal(
            "the curator's queue is EMPTY after the audit over the fixture: "
            "there is no dead or moved anchor to judge, so there is no (b) to "
            "measure in this slice. A delta 0 from here is not a verdict, it "
            "is a pipeline that did not run"
        )
    worker = CuratorWorker(
        shadow,
        root=root,
        project=project,
        judgments_per_day=budget,
        verdicts=DEFAULT_VERDICTS,
    )
    calls = worker.cycle()
    if calls == 0:
        raise ShadowRefusal(
            f"the cycle made NO calls at all with {queued} candidates queued: "
            "either the day's budget was already spent, or the child did not "
            "start. I refuse instead of reporting a treatment identical to the "
            "control"
        )
    return calls


def run_cli(args: Any) -> int:
    """`nmemory-evals shadow`: the U5 run against the live store, pure read.

    D18: 0 when the ruler judged, 2 when the store, provider, or disposable
    database was missing. The live store is opened READ-ONLY; everything
    written goes to the disposable shadow, and the fixture is a copy of the
    anchored files — the real tree is never touched.
    """
    from nmemory_evals.capture_gap import EvalRefusal, open_live_store

    source_root = Path(args.source_root).resolve()
    dest_root = Path(args.dest_root)
    judged: list[int] = []
    try:
        live = open_live_store()
        with open_shadow(args.shadow_store) as shadow:

            def cycle() -> None:
                # Record the model calls that the cycle made.
                judged.append(
                    curator_cycle(
                        shadow, root=dest_root, project=args.project, budget=args.budget
                    )
                )

            report = run_shadow(
                live,
                shadow,
                source_root=source_root,
                dest_root=dest_root,
                per_cell=args.per_cell,
                now=datetime.now(UTC),
                cycle=cycle,
                project=args.project,
            )
    except (EvalRefusal, ShadowRefusal) as err:
        print(f"shadow: REFUSED — {err}", file=sys.stderr)
        return 2

    payload = {
        "slice": len(report.manifest),
        "cells": {
            cell: sum(1 for m in report.manifest if m.cell == cell) for cell in CELLS
        },
        "control": {k: dict(v) for k, v in report.control.items()},
        "treatment": {k: dict(v) for k, v in report.treatment.items()},
        "served_false_delta": report.served_false_delta(),
        "model_calls": judged[0] if judged else 0,
        # The manifest enters the receipt: without it the number is not
        # auditable — whoever reads it does not know WHICH capsule was in
        # which cell.
        "manifest": [
            {
                "capsule_id": m.capsule_id,
                "cell": m.cell,
                "truth": m.truth,
                "anchor": f"{m.anchor_path}:{m.anchor_line}",
            }
            for m in report.manifest
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0
