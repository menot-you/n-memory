"""nmemory capsule audit — resolvable condition obsolescence detector.

Evaluates declared stale_if conditions against machine-resolvable entities
(PRs, git SHAs, file paths, capsule IDs, branches) and reports whether any
condition has triggered. Purely advisory: never mutates, never downgrades.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .anchors import _ausente, _conhecidos
from .consolidate import Tier
from .subproc import run_with_timeout

if TYPE_CHECKING:
    from .store import Store


class StalenessClass(StrEnum):
    PR = "pr"
    SHA = "sha"
    PATH = "path"
    CAPSULE = "capsule"
    BRANCH = "branch"
    PROSE = "prose"


class StalenessVerdict(StrEnum):
    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    UNDECIDABLE = "undecidable"


@dataclass(frozen=True)
class StalenessHit:
    capsule_id: str
    stale_if: str
    class_: StalenessClass
    entity: str
    verdict: StalenessVerdict
    observed_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "capsule_id": self.capsule_id,
            "stale_if": self.stale_if,
            "class": self.class_.value,
            "entity": self.entity,
            "verdict": self.verdict.value,
        }
        if self.observed_state is not None:
            d["observed_state"] = self.observed_state
        return d


@dataclass(frozen=True)
class StalenessReport:
    total_evaluated: int
    resolvable_count: int
    prose_count: int
    triggered_count: int
    not_triggered_count: int
    undecidable_count: int
    hits: list[StalenessHit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_evaluated": self.total_evaluated,
            "resolvable_count": self.resolvable_count,
            "prose_count": self.prose_count,
            "triggered_count": self.triggered_count,
            "not_triggered_count": self.not_triggered_count,
            "undecidable_count": self.undecidable_count,
            "hits": [h.to_dict() for h in self.hits],
        }


# Fenced by WORD, not substring: `sumir` matched `assumir`/`presumir`/
# `consumir`, `drop` matched `dropdown`; and `desaparecer` did not exist. Measured in
# the live store on 2026-09-19 before this calibration.
_DISAPPEARANCE = re.compile(
    r"\b(?:"
    r"deleted?|deletes|removed?|removes|retired?|retires|is gone|goes away"
    r"|dropped|drops?|vanish(?:es|ed)?"
    r"|apagad[oa]s?|removid[oa]s?|retirad[oa]s?|deixa(?:m|r)? de existir"
    r"|sumir|sumiu|desaparec\w*|for removido"
    r")\b"
)

# Clause boundaries: the disappearance word must be in the SAME
# clause as the entity (“landing/ is removed, or tools/x.sh changes its
# scope” is not about tools/x.sh disappearing).
_CLAUSE = re.compile(r",|;|\bor\b|\bou\b")


def names_disappearance(condition: str, entity: str | None = None) -> bool:
    text = condition.lower()
    if entity is None:
        return _DISAPPEARANCE.search(text) is not None
    ent = entity.lower()
    return any(
        ent in clause and _DISAPPEARANCE.search(clause)
        for clause in _CLAUSE.split(text)
    )


_MERGE = re.compile(r"\b(?:merges?|merged|merging|lands?|landed|fundid[oa]|fundir)\b")
_CLOSE = re.compile(r"\b(?:closes?|closed|closing|fecha(?:r|do|da)?|fechad[oa])\b")
# “fechar sem merge” / “closed without merging” names merge in order to NEGATE it.
_NEGATED_MERGE = re.compile(
    r"\b(?:sem|without|not|nao|não|un)\s*(?:merge|merging|merged|fundir|fundid[oa])\b"
)
_MAIN = re.compile(r"\b(?:origin/)?main\b")


def repository_name(anchor_root: Path) -> str | None:
    """`owner/name` of this tree's `origin`, or None when there is no remote."""
    try:
        res = run_with_timeout(
            ["git", "remote", "get-url", "origin"],
            cwd=str(anchor_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if res.returncode != 0:
        return None
    url = res.stdout.strip().removesuffix(".git").replace(":", "/")
    parts = [p for p in url.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def project_is_this_repository(project_id: str, repo: str) -> bool:
    """`pynott`, `pynott/nmemory` and `menot-you/pynott` belong to this tree;
    `nott/tech` does not. The capsule's project is the only hint the store provides."""
    name = repo.rsplit("/", 1)[-1]
    return project_id == repo or project_id.split("/", 1)[0] == name


_MOVES = re.compile(
    r"\b(?:moves?|moved|advances?|advanced|past|beyond|avan[çc]ar|avan[çc]a|al[ée]m)\b"
)


def names_main_moving(condition: str, sha: str) -> bool:
    """The clause containing the SHA names `main` advancing."""
    text = condition.lower()
    return any(
        sha.lower() in clause and _MAIN.search(clause) and _MOVES.search(clause)
        for clause in _CLAUSE.split(text)
    )


def remote_refs_readable(anchor_root: Path) -> bool:
    try:
        res = run_with_timeout(
            ["git", "for-each-ref", "--count=1", "refs/remotes/origin/"],
            cwd=str(anchor_root),
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0 and bool(res.stdout.strip())
    except OSError, subprocess.SubprocessError:
        return False


class SystemRunner:
    def __init__(self, anchor_root: Path, store: Store) -> None:
        self.anchor_root = anchor_root
        self.store = store
        self.pr_cache: dict[int, str | None] = {}
        self._repo: str | None = None
        self._repo_read = False
        self.sha_cache: dict[str, tuple[StalenessVerdict, str | None]] = {}
        self.path_cache: dict[str, tuple[StalenessVerdict, str | None]] = {}
        self.branch_cache: dict[str, tuple[StalenessVerdict, str | None]] = {}

    def _pr_state(self, pr: int) -> str | None:
        if pr in self.pr_cache:
            return self.pr_cache[pr]
        state: str | None = None
        try:
            res = run_with_timeout(
                ["gh", "pr", "view", str(pr), "--json", "state"],
                cwd=str(self.anchor_root),
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                raw = json.loads(res.stdout).get("state")
                state = str(raw) if raw in ("MERGED", "CLOSED", "OPEN") else None
        except OSError, subprocess.SubprocessError, json.JSONDecodeError:
            state = None
        self.pr_cache[pr] = state
        return state

    def _repository(self) -> str | None:
        if not self._repo_read:
            self._repo = repository_name(self.anchor_root)
            self._repo_read = True
        return self._repo

    def check_pr(
        self, pr: int, condition: str, capsule_id: str
    ) -> tuple[StalenessVerdict, str | None]:
        if self.store.capsules.get_tombstone(capsule_id) is not None:
            return (StalenessVerdict.UNDECIDABLE, None)
        # `gh` runs in the tree's repository; the store has capsules from several. The
        # capsule's project is the only hint — if it doesn't link it to this tree, #N
        # belongs to another repository and cannot be decided.
        repo = self._repository()
        stored = self.store.capsules.get(capsule_id)
        if repo is None or stored is None:
            return (StalenessVerdict.UNDECIDABLE, "repositório do PR indeterminável")
        if not project_is_this_repository(stored.capsule.scope.project_id, repo):
            return (
                StalenessVerdict.UNDECIDABLE,
                f"repositório do PR indeterminável ({stored.capsule.scope.project_id} ≠ {repo})",
            )
        state = self._pr_state(pr)
        if state is None:
            return (StalenessVerdict.UNDECIDABLE, None)
        if state == "OPEN":
            return (StalenessVerdict.NOT_TRIGGERED, "OPEN")
        text = _NEGATED_MERGE.sub(" ", condition.lower())
        if state == "MERGED" and _MERGE.search(text):
            return (StalenessVerdict.TRIGGERED, "MERGED")
        if state == "CLOSED" and _CLOSE.search(text):
            return (StalenessVerdict.TRIGGERED, "CLOSED")
        return (
            StalenessVerdict.UNDECIDABLE,
            f"{state}, but the condition does not name it",
        )

    def check_sha(
        self, sha: str, condition: str
    ) -> tuple[StalenessVerdict, str | None]:
        # The only thing this check measures is “origin/main advanced past
        # the sha”. A condition that does not name main (“lane/x head moves off”,
        # “if X is reverted”) cannot be decided with that measurement.
        if not names_main_moving(condition, sha):
            return (
                StalenessVerdict.UNDECIDABLE,
                "condition is not about main advancing",
            )
        if sha in self.sha_cache:
            return self.sha_cache[sha]
        ret: tuple[StalenessVerdict, str | None]
        try:
            res = run_with_timeout(
                ["git", "cat-file", "-t", sha],
                cwd=str(self.anchor_root),
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                ret = (StalenessVerdict.UNDECIDABLE, None)
                self.sha_cache[sha] = ret
                return ret

            anc_res = run_with_timeout(
                ["git", "merge-base", "--is-ancestor", sha, "origin/main"],
                cwd=str(self.anchor_root),
                capture_output=True,
                check=False,
            )
            is_ancestor = anc_res.returncode == 0

            tip_res = run_with_timeout(
                ["git", "rev-parse", "origin/main"],
                cwd=str(self.anchor_root),
                capture_output=True,
                text=True,
                check=False,
            )
            tip = tip_res.stdout.strip() if tip_res.returncode == 0 else ""

            if not tip or not is_ancestor:
                ret = (StalenessVerdict.UNDECIDABLE, None)
            elif tip.startswith(sha):
                ret = (StalenessVerdict.NOT_TRIGGERED, "still the tip")
            else:
                ret = (
                    StalenessVerdict.TRIGGERED,
                    f"main advanced to {tip[: min(len(tip), 9)]}",
                )
        except OSError, subprocess.SubprocessError:
            ret = (StalenessVerdict.UNDECIDABLE, None)
        self.sha_cache[sha] = ret
        return ret

    def check_path(
        self, path: str, condition: str
    ) -> tuple[StalenessVerdict, str | None]:
        if not names_disappearance(condition, path):
            return (
                StalenessVerdict.UNDECIDABLE,
                "condition is not about disappearance",
            )
        if path in self.path_cache:
            return self.path_cache[path]
        if ".." in path:
            ret: tuple[StalenessVerdict, str | None] = (
                StalenessVerdict.UNDECIDABLE,
                None,
            )
            self.path_cache[path] = ret
            return ret

        full = self.anchor_root / path
        try:
            if full.is_symlink():
                ret = (StalenessVerdict.UNDECIDABLE, None)
                self.path_cache[path] = ret
                return ret
            if full.is_file():
                ret = (StalenessVerdict.NOT_TRIGGERED, "still present")
            elif (
                _conhecidos(self.anchor_root) is not None
                and _ausente(self.anchor_root, path) == "dead_anchor"
            ):
                # The tree LOST the file. A path that git never tracked
                # (session artifact, run output) is not evidence.
                ret = (StalenessVerdict.TRIGGERED, "gone")
            else:
                ret = (StalenessVerdict.UNDECIDABLE, "never tracked by this tree")
        except OSError:
            ret = (StalenessVerdict.UNDECIDABLE, None)
        self.path_cache[path] = ret
        return ret

    def check_capsule(self, capsule_id: str) -> tuple[StalenessVerdict, str | None]:
        if self.store.capsules.get_tombstone(capsule_id) is not None:
            return (StalenessVerdict.UNDECIDABLE, None)
        stored = self.store.capsules.get(capsule_id)
        if not stored:
            return (StalenessVerdict.UNDECIDABLE, None)
        if self.store.relations.is_superseded(capsule_id):
            return (StalenessVerdict.TRIGGERED, "superseded")

        if self.store.tiers.get_tier(capsule_id) == Tier.ARCHIVED:
            return (StalenessVerdict.TRIGGERED, "archived")
        return (StalenessVerdict.NOT_TRIGGERED, "active")

    def check_branch(
        self, branch: str, condition: str
    ) -> tuple[StalenessVerdict, str | None]:
        if not names_disappearance(condition, branch):
            return (
                StalenessVerdict.UNDECIDABLE,
                "condition is not about the ref disappearing",
            )
        if branch in self.branch_cache:
            return self.branch_cache[branch]
        ref_name = f"origin/{branch}"
        ret: tuple[StalenessVerdict, str | None]
        try:
            res = run_with_timeout(
                ["git", "rev-parse", "--verify", ref_name],
                cwd=str(self.anchor_root),
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                if remote_refs_readable(self.anchor_root):
                    ret = (StalenessVerdict.TRIGGERED, "gone")
                else:
                    ret = (StalenessVerdict.UNDECIDABLE, None)
                self.branch_cache[branch] = ret
                return ret
            verified = res.stdout.strip()
            if not verified:
                ret = (StalenessVerdict.UNDECIDABLE, None)
            else:
                ret = (
                    StalenessVerdict.NOT_TRIGGERED,
                    f"still at {verified[: min(len(verified), 9)]}",
                )
        except OSError, subprocess.SubprocessError:
            ret = (StalenessVerdict.UNDECIDABLE, None)
        self.branch_cache[branch] = ret
        return ret


def extract_pr_number(text: str) -> int | None:
    for i, ch in enumerate(text):
        if ch == "#":
            start = i + 1
            end = start
            while end < len(text) and text[end].isdigit():
                end += 1
            length = end - start
            if 2 <= length <= 5:
                try:
                    return int(text[start:end])
                except ValueError:
                    pass
    return None


def extract_capsule_id(text: str) -> str | None:
    prefix = "cap-"
    i = 0
    while i + len(prefix) <= len(text):
        if text[i : i + len(prefix)] == prefix:
            if i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
                i += 1
                continue
            start = i
            end = i + len(prefix)
            while end < len(text) and text[end].isdigit():
                end += 1
            if end > i + len(prefix) and (end == len(text) or not text[end].isalnum()):
                return text[start:end]
            i = end
        else:
            i += 1
    return None


def extract_file_path(text: str) -> str | None:
    delims = (" ", "\t", "\n", "(", ")", "[", "]", "`", '"', "'")
    words: list[str] = []
    curr: list[str] = []
    for ch in text:
        if ch in delims:
            if curr:
                words.append("".join(curr))
                curr = []
        else:
            curr.append(ch)
    if curr:
        words.append("".join(curr))

    extensions = (".rs", ".sh", ".md", ".toml", ".json", ".py")
    for w in words:
        clean = w.strip(",.:;")
        if (
            "/" in clean
            and any(clean.endswith(ext) for ext in extensions)
            and all(c.isalnum() or c in "/._-" for c in clean)
        ):
            return clean
    return None


def extract_branch(text: str) -> str | None:
    delims = (" ", "\t", "\n", "(", ")", "[", "]", "`", '"', "'")
    words: list[str] = []
    curr: list[str] = []
    for ch in text:
        if ch in delims:
            if curr:
                words.append("".join(curr))
                curr = []
        else:
            curr.append(ch)
    if curr:
        words.append("".join(curr))

    prefixes = ("feat/", "fix/", "chore/", "docs/")
    extensions = (".rs", ".sh", ".md", ".toml", ".json", ".py")
    for w in words:
        clean = w.strip(",.:;")
        if (
            any(clean.startswith(p) for p in prefixes)
            and not any(clean.endswith(ext) for ext in extensions)
            and all(c.isalnum() or c in "/_-" for c in clean)
        ):
            return clean
    return None


def extract_git_sha(text: str) -> str | None:
    words: list[str] = []
    curr: list[str] = []
    for ch in text:
        if ch.isalnum():
            curr.append(ch)
        else:
            if curr:
                words.append("".join(curr))
                curr = []
    if curr:
        words.append("".join(curr))

    for w in words:
        if (
            7 <= len(w) <= 40
            and all(c in "0123456789abcdef" for c in w)
            and not w.isdigit()
        ):
            return w
    return None


def evaluate_condition(
    capsule_id: str,
    stale_if: str,
    runner: SystemRunner | Any,
) -> StalenessHit:
    # “if apps/x.py is removed in #690” is about the path, not about the PR.
    path = extract_file_path(stale_if)
    if path is not None and names_disappearance(stale_if, path):
        verdict, obs = runner.check_path(path, stale_if)
        return StalenessHit(
            capsule_id=capsule_id,
            stale_if=stale_if,
            class_=StalenessClass.PATH,
            entity=path,
            verdict=verdict,
            observed_state=obs,
        )

    pr = extract_pr_number(stale_if)
    if pr is not None:
        verdict, obs = runner.check_pr(pr, stale_if, capsule_id)
        return StalenessHit(
            capsule_id=capsule_id,
            stale_if=stale_if,
            class_=StalenessClass.PR,
            entity=f"#{pr}",
            verdict=verdict,
            observed_state=obs,
        )

    cap = extract_capsule_id(stale_if)
    if cap is not None:
        verdict, obs = runner.check_capsule(cap)
        return StalenessHit(
            capsule_id=capsule_id,
            stale_if=stale_if,
            class_=StalenessClass.CAPSULE,
            entity=cap,
            verdict=verdict,
            observed_state=obs,
        )

    if path is not None:
        verdict, obs = runner.check_path(path, stale_if)
        return StalenessHit(
            capsule_id=capsule_id,
            stale_if=stale_if,
            class_=StalenessClass.PATH,
            entity=path,
            verdict=verdict,
            observed_state=obs,
        )

    branch = extract_branch(stale_if)
    if branch is not None:
        verdict, obs = runner.check_branch(branch, stale_if)
        return StalenessHit(
            capsule_id=capsule_id,
            stale_if=stale_if,
            class_=StalenessClass.BRANCH,
            entity=branch,
            verdict=verdict,
            observed_state=obs,
        )

    sha = extract_git_sha(stale_if)
    if sha is not None:
        verdict, obs = runner.check_sha(sha, stale_if)
        return StalenessHit(
            capsule_id=capsule_id,
            stale_if=stale_if,
            class_=StalenessClass.SHA,
            entity=sha,
            verdict=verdict,
            observed_state=obs,
        )

    return StalenessHit(
        capsule_id=capsule_id,
        stale_if=stale_if,
        class_=StalenessClass.PROSE,
        entity="",
        verdict=StalenessVerdict.UNDECIDABLE,
        observed_state=None,
    )


def evaluate_all(
    conditions: list[tuple[str, str]], runner: SystemRunner | Any
) -> StalenessReport:
    hits: list[StalenessHit] = []
    resolvable_count = 0
    prose_count = 0
    triggered_count = 0
    not_triggered_count = 0
    undecidable_count = 0

    for cap_id, stale_if in conditions:
        hit = evaluate_condition(cap_id, stale_if, runner)
        if hit.class_ == StalenessClass.PROSE:
            prose_count += 1
        else:
            resolvable_count += 1

        if hit.verdict == StalenessVerdict.TRIGGERED:
            triggered_count += 1
        elif hit.verdict == StalenessVerdict.NOT_TRIGGERED:
            not_triggered_count += 1
        elif hit.verdict == StalenessVerdict.UNDECIDABLE:
            undecidable_count += 1
        hits.append(hit)

    return StalenessReport(
        total_evaluated=len(conditions),
        resolvable_count=resolvable_count,
        prose_count=prose_count,
        triggered_count=triggered_count,
        not_triggered_count=not_triggered_count,
        undecidable_count=undecidable_count,
        hits=hits,
    )


#: The `result` that a triggered condition writes. Kept OUTSIDE of
#: `VERIFICATION_PASSING` and outside of `VERIFICATION_NEUTRAL` deliberately: this is
#: how `standing_of` reads it as `caducou reason=stale_condition_met` through the
#: existing branch (`store.py:4663`), without a new branch or new vocabulary.
#:
#: The path through `valid_to` does not exist — it is write-once at ingest
#: (`ingest.py:485`) and no tree `UPDATE` touches it. The verification
#: event is the only door, and it is the right one: append-only, so a subsequent
#: `pass` recovers the capsule without erasing history.
STALENESS_RESULT = "stale_condition_met"
STALENESS_KIND = "staleness"
STALENESS_ACTOR = "staleness"


def record_staleness(
    store: Store,
    report: StalenessReport,
    now: datetime,
) -> int:
    """Writes an event per TRIGGERED condition, and returns how many were written.

    This is the only function in this module that writes. Everything else above is a
    pure evaluator, and remains so: callers who only want the report do not call this.

    **Only `TRIGGERED`.** `UNDECIDABLE` generates no event at all, not even a neutral one
    — of the 2599 conditions in the live store, 1998 are undecidable, and marking one
    of them as lapsed would fabricate falsehood from absent proof,
    which is precisely the defect U1 stripped from the curator. A neutral event
    also does not serve: there is no judgment to record, and an extra row is a row
    that someone reads as a judgment.

    **Idempotent by LAST event, not by existence.** The evaluator runs on a daily
    timer; a condition that triggered remains triggered tomorrow. Without this
    fence, each run adds one row per capsule for a truth that has not
    changed, and `list_verifications` is read per capsule on each `standing_of`. The
    fence is by the LAST event and not by “one already exists”: if someone verified
    the capsule AFTER triggering — a `pass` that recovered it — and the condition
    triggered again, that is a new judgment and must be recorded.
    """
    escritos = 0
    for hit in report.hits:
        # UNDECIDABLE writes nothing on either side: inability to evaluate the
        # condition is neither triggering nor recovery; it is absence of judgment.
        if hit.verdict is StalenessVerdict.UNDECIDABLE:
            continue
        # The fence checks the LAST event of THIS axis, not the last event overall.
        # `standing_of` resolves an axis by `kind` since 2026-09-19, and the
        # anchor sweep writes an `anchor` event per capsule every
        # day: evaluated against the entire list, this fence saw the anchor `pass`,
        # decided there was a new judgment to record, and rewrote the same
        # obsolescence every 24 hours.
        anteriores = [
            v
            for v in store.anchors.list_verifications(hit.capsule_id)
            if v.kind == STALENESS_KIND
        ]
        disparado = anteriores and anteriores[-1].result == STALENESS_RESULT
        if hit.verdict is StalenessVerdict.TRIGGERED:
            if disparado:
                continue
            estado = hit.observed_state or "triggered"
            result = STALENESS_RESULT
            detail = (
                f'stale_if "{hit.stale_if}" met: {hit.class_.value} '
                f"{hit.entity} is {estado}"
            )
        else:
            # RECOVERY, and the reason it exists: `record_staleness` was the
            # ONLY writer on this axis and only knew how to condemn. The curator's `holds`
            # is mapped to the `anchor` axis in `standing_of`, so it does not reach
            # this one, and no other code wrote here. While `standing_of`
            # was just an envelope field this was ugly but inert; with the
            # `lapsed` fence removing from service whatever is `caducou`, a single trigger
            # became a perpetual condemnation — contrary to the owner's decision to
            # HIDE rather than delete. The docstring above already promised “a `pass` that
            # recovered it”; this is it.
            #
            # There is only a new judgment if the PREVIOUS state of this axis was triggered. A
            # `pass` for a capsule that never lapsed would add one row per capsule
            # per day to an append-only table that `standing_of` reads per capsule
            # on every service, for a truth nobody contested.
            if not disparado:
                continue
            estado = hit.observed_state or "not_triggered"
            result = "pass"
            detail = (
                f'stale_if "{hit.stale_if}" no longer met: {hit.class_.value} '
                f"{hit.entity} is {estado}"
            )
        store.anchors.record_verification(
            hit.capsule_id,
            kind=STALENESS_KIND,
            result=result,
            detail=detail,
            actor=STALENESS_ACTOR,
            now=now,
        )
        escritos += 1
    return escritos
