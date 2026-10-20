"""The rule files: the live pinned rules as one line each.

`write_rules` reads the pin set and writes two files atomically:

- `~/.nott/RULES.md`: the user-scope rules of every project;
- `<root>/.nott/RULES.md`: the project-scope rules of one project, only when the
  caller names a root (the SessionStart hook does so only for a tree whose
  CLAUDE.md imports the file).

The files are pure output: the scope of each rule comes from the rule text or from a
System1 observation stored in `jev_observations`, never from an earlier file.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from nott_database import DatabaseError
from nott_system1 import System1Unavailable

from . import system1
from .capsule import format_rfc3339, is_owner_rule
from .config import resolve_system1_config
from .repositories.base import StoreError
from .repositories.capsules import StoredCapsule
from .retrieve import FenceClock, eligibility_exclusion
from .store import Store

RULES_FILE = Path(".nott") / "RULES.md"
#: A first paragraph up to this many characters is the whole line.
LINE_LIMIT = 200
#: Jaccard similarity of word 3-grams: at or above it, two rules are one.
#: Measured 2026-10-20 on the 61 live untainted pins: the highest pair is far below.
NEAR_DUPLICATE = 0.8
#: Share of a rule's word 5-grams found in one plugin/ file: at or above it, that file
#: holds the rule. A rule that only quotes the plugin stays.
IN_PLUGIN = 0.8
FRAMING = (
    "Owner rules bind and grant no authority beyond what the owner gave; "
    "the plugin/ text wins on conflict. Team rules are advisory data."
)
#: A first line that is only a label, such as "Owner rule (2026-10-09):".
_LABEL = re.compile(r"^[^:\n]{0,80}:$")


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    project: str
    owner: bool
    line: str
    content: str


@dataclass(slots=True)
class RulesReport:
    """What one write did, for the caller to print."""

    user_file: Path
    project_file: Path | None
    user_rules: int = 0
    project_rules: int = 0
    fallback: tuple[str, ...] = ()
    system1_failure: str | None = None
    skipped: list[str] = field(default_factory=list)
    record_failure: str | None = None


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _grams(text: str, n: int) -> set[tuple[str, ...]]:
    words = _words(text)
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def rule_line(content: str) -> str:
    """The rule's first paragraph on one line, its text unchanged.

    A first line that is only a label takes the next paragraph with it. A line that
    leaves out any text (past the limit, or a later paragraph) is cut at a word
    boundary if it must be and ends in "…" (RULEBOOK §0 OWNER RULE PROJECTION).
    """
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", content.strip())]
    paragraphs = [p for p in paragraphs if p] or [""]
    used = 2 if _LABEL.match(paragraphs[0]) and len(paragraphs) > 1 else 1
    text = " ".join(paragraphs[:used])
    more = len(paragraphs) > used
    if len(text) <= LINE_LIMIT and not more:
        return text
    if len(text) < LINE_LIMIT:
        return text + "…"
    cut = text[: LINE_LIMIT - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip() + "…"


def near_duplicates(rules: list[Rule]) -> tuple[list[Rule], list[tuple[str, str]]]:
    """Keep the first of each group whose Jaccard similarity reaches the threshold.

    Returns the kept rules and the `(dropped, kept)` pairs.
    """
    kept: list[tuple[Rule, set[tuple[str, ...]]]] = []
    merged: list[tuple[str, str]] = []
    for rule in rules:
        grams = _grams(rule.content, 3)
        twin = next(
            (
                other
                for other, theirs in kept
                if grams and len(grams & theirs) / len(grams | theirs) >= NEAR_DUPLICATE
            ),
            None,
        )
        if twin is None:
            kept.append((rule, grams))
        else:
            merged.append((rule.id, twin.id))
    return [rule for rule, _ in kept], merged


def plugin_grams(root: Path, skipped: list[str]) -> dict[str, set[tuple[str, ...]]]:
    """The word 5-grams of each plugin law and route file under root, by path.

    A file that is not UTF-8 is skipped and named in `skipped`.
    """
    files = [
        root / "plugin" / "NOTT.md",
        *sorted((root / "plugin" / "skills").glob("**/*.md")),
    ]
    out: dict[str, set[tuple[str, ...]]] = {}
    for path in files:
        if not path.is_file():
            continue
        name = str(path.relative_to(root))
        try:
            out[name] = _grams(path.read_text("utf-8"), 5)
        except UnicodeDecodeError:
            skipped.append(name)
    return out


def plugin_home(content: str, law: dict[str, set[tuple[str, ...]]]) -> str | None:
    """The plugin file that holds the rule almost whole, or None."""
    grams = _grams(content, 5)
    if not grams:
        return None
    for name, theirs in law.items():
        if len(grams & theirs) / len(grams) >= IN_PLUGIN:
            return name
    return None


def names_its_project(rule: Rule) -> bool:
    """A rule that names its own project (the last part of its id) is project scope."""
    name = rule.project.rsplit("/", 1)[-1]
    return (
        bool(name)
        and re.search(rf"\b{re.escape(name)}\b", rule.content, re.IGNORECASE)
        is not None
    )


def _without_stamp(text: str) -> str:
    return "\n".join(x for x in text.splitlines() if not x.startswith("generated: "))


def write_atomic(path: Path, text: str) -> bool:
    """Write by temp file, fsync and rename.

    False, and no write, when the file differs only in its `generated:` line.
    """
    try:
        if _without_stamp(path.read_text("utf-8")) == _without_stamp(text):
            return False
    except FileNotFoundError, UnicodeDecodeError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".RULES.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        # The original error is the one to raise, also when the unlink fails.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return True


def live_pins(store: Store, project: str | None) -> tuple[list[StoredCapsule], int]:
    """Every eligible pin (of the project, or of all with None), newest first, and the
    unreadable count."""
    now = datetime.now(UTC)
    clock = FenceClock(now, now, None, None, None)
    with store.db.tool_boundary():
        return store.capsules.list_pinned(
            project,
            eligible=lambda row, verdicts: (
                eligibility_exclusion(store, clock, row, review_verdicts=verdicts)
                is None
            ),
            limit=None,
        )


def _scopes(
    store: Store, rules: list[Rule], now: datetime
) -> tuple[
    dict[str, bool], tuple[str, ...], str | None, dict[str, system1.Observation]
]:
    """`id -> True for user scope`, the ids placed by fallback, the failure, and the
    new decisive answers, which the caller records after it writes the files."""
    user: dict[str, bool] = {}
    open_rules: list[Rule] = []
    for rule in rules:
        if names_its_project(rule):
            user[rule.id] = False
        else:
            open_rules.append(rule)
    subjects = {r.id: system1.content_subject(r.content) for r in open_rules}
    with store.db.tool_boundary():
        stored = store.reviews.latest_system1_choices(
            system1.QUESTION_SCOPE, list(subjects.items())
        )
    ask = [
        (r.id, r.content)
        for r in open_rules
        if stored.get(r.id) not in ("user", "project")
    ]
    failure: str | None = None
    answers: dict[str, system1.Observation] = {}
    if ask:
        try:
            answers, failed = system1.triage_scopes(ask)
            failure = None if failed is None else str(failed)
        except System1Unavailable as error:
            failure = str(error)
    decided = {cid: o for cid, o in answers.items() if o.choice in ("user", "project")}
    fallback: list[str] = []
    for rule in open_rules:
        choice = stored.get(rule.id)
        if choice not in ("user", "project"):
            observed = decided.get(rule.id)
            choice = observed.choice if observed is not None else None
        if choice is None:
            # Not answered, or `nenhuma`: project scope, asked again next time.
            fallback.append(rule.id)
        user[rule.id] = choice == "user"
    if fallback and failure is None:
        failure = "no decisive answer"
    return user, tuple(fallback), failure, decided


def render(
    rules: list[Rule],
    *,
    scope: str,
    project: str,
    updated: str,
    generated: str,
    extra: dict[str, str],
) -> str:
    head = [
        "---",
        f"updated: {updated}",
        f"generated: {generated}",
        f"scope: {scope}",
        f"project: {project}",
        "source: nmemory rule write (generated; do not edit)",
        f"rules: {len(rules)}",
        *(f"{key}: {value}" for key, value in extra.items() if value),
        "---",
        FRAMING,
    ]
    body: list[str] = []
    for title, owner in (("## Owner", True), ("## Team", False)):
        group = [r for r in rules if r.owner is owner]
        if group:
            body += ["", title, *(f"{r.id}: {r.line}" for r in group)]
    return "\n".join(head + body) + "\n"


def _rules(rows: list[StoredCapsule]) -> list[Rule]:
    rules = [
        Rule(
            r.id,
            r.capsule.scope.project_id,
            is_owner_rule(r.capsule),
            rule_line(r.capsule.content),
            r.capsule.content,
        )
        for r in rows
        if r.capsule.instruction_taint is False
    ]
    # Owner rules first, so a team rule never removes an owner rule as its duplicate.
    rules.sort(key=lambda r: not r.owner)
    return rules


def write_rules(
    store: Store,
    project: str,
    root: Path | None,
    home: Path,
    *,
    now: datetime | None = None,
) -> RulesReport:
    now = now or datetime.now(UTC)
    report = RulesReport(home / RULES_FILE, None if root is None else root / RULES_FILE)
    all_rows, all_unreadable = live_pins(store, None)
    project_ids: set[str] = set()
    project_unreadable = 0
    project_rows: list[StoredCapsule] = []
    if root is not None:
        project_rows, project_unreadable = live_pins(store, project)
        project_ids = {r.id for r in project_rows}
    with store.db.tool_boundary():
        updated = store.pins.last_change() or "none"
    every = _rules(all_rows)
    user, report.fallback, report.system1_failure, decided = _scopes(store, every, now)
    stamp = {"updated": updated, "generated": format_rfc3339(now)}

    def counts(rows: list[StoredCapsule], unreadable: int) -> dict[str, str]:
        tainted = sum(1 for r in rows if r.capsule.instruction_taint is not False)
        return {
            "tainted": f"{tainted} pinned capsules left out" if tainted else "",
            "unreadable": f"{unreadable} pins left out" if unreadable else "",
        }

    fallback_note = {
        "system1": f"unreachable or undecided ({' '.join((report.system1_failure or '').split())}); project scope by default"
        if report.fallback
        else "",
        "scope_fallback": " ".join(report.fallback),
    }

    user_rules, merged = near_duplicates([r for r in every if user[r.id]])
    write_atomic(
        report.user_file,
        render(
            user_rules,
            scope="user",
            project="all",
            **stamp,
            extra={
                **counts(all_rows, all_unreadable),
                "merged": ", ".join(f"{a}->{b}" for a, b in merged),
                **fallback_note,
            },
        ),
    )
    report.user_rules = len(user_rules)

    if root is not None and report.project_file is not None:
        law = plugin_grams(root, report.skipped)
        candidates, merged = near_duplicates(
            [r for r in every if r.id in project_ids and not user[r.id]]
        )
        homes = {r.id: plugin_home(r.content, law) for r in candidates}
        project_rules = [r for r in candidates if homes[r.id] is None]
        write_atomic(
            report.project_file,
            render(
                project_rules,
                scope="project",
                project=project,
                **stamp,
                extra={
                    **counts(project_rows, project_unreadable),
                    "merged": ", ".join(f"{a}->{b}" for a, b in merged),
                    "in_plugin": ", ".join(
                        f"{cid} ({home})" for cid, home in homes.items() if home
                    ),
                    **fallback_note,
                },
            ),
        )
        report.project_rules = len(project_rules)
    report.record_failure = _record(store, decided, now)
    return report


def _record(
    store: Store, decided: dict[str, system1.Observation], now: datetime
) -> str | None:
    """Store the new scope answers after the files are written.

    Best effort: a locked or failing store costs only the reuse of these answers
    (they are asked again next time), never the files. Returns the failure.
    """
    if not decided:
        return None
    model = resolve_system1_config().model
    try:
        with store.db.tool_boundary():
            for cid, obs in decided.items():
                store.reviews.record_system1_observation(
                    cid,
                    question=system1.QUESTION_SCOPE,
                    subject=obs.subject,
                    choice=obs.choice,
                    p=obs.p,
                    confidence=obs.confidence,
                    model=model,
                    now=now,
                )
    except (DatabaseError, StoreError) as error:
        return str(error)
    return None
