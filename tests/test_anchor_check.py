"""U2 (nmemory-standing): the anchor loop — model-free.

282 of 834 live capsules anchored in code (33,8 %) had demonstrably
dead anchors against HEAD and were served with full authority:
files changed and nobody told memory. This is the cheapest actor that
exists — a hook queues the edited path, a mechanical drain judges the anchor
against the tree and writes `verification_events`. No model wakes up here.

`verification_events` is NON-native (precedent `read_traffic`): append-only,
`seq` IDENTITY, outside the canonical digest, `CONTRACT_VERSION` stays 29.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from storefile import fresh_store_path

from nmemory import anchors
from nmemory.anchors import (
    AnchorScopeError,
    AnchorVerdictError,
    audit_all,
    drain_spool,
    judge_anchor,
    parse_code_anchor,
)
from nmemory.capture import CaptureContext
from nmemory.database import Database
from nmemory.queries.curator import curator_candidates
from nmemory.queries.standing import standing_of
from nmemory.server import MemoryServer
from nmemory.store import Store

T0 = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)


def _ctx() -> CaptureContext:
    return CaptureContext(
        harness="pytest",
        model="none",
        cwd="/tmp",
        session_id="sess-anchor",
        host="t",
        owner="t",
    )


def _fresh() -> tuple[Database, Store, MemoryServer]:
    url = fresh_store_path("anchor")
    pg = Database(url)
    store = Store.open(url)
    return pg, store, MemoryServer(store=store, project_id="anchor")


def _ingest(server: MemoryServer, content: str, anchor: str, now: datetime) -> str:
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": content,
            "source": "t",
            "anchor": anchor,
            "kind": "fact",
            # The capture port rejects a `fact` without retrievable proof, and this
            # file intentionally anchors to `&cap-1` — an anchor that is NOT code,
            # to prove the auditor ignores it. The `proof_hint` allows the
            # capsule to enter without modifying its anchor, which is the test target.
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
        },
        context=_ctx(),
        now=now,
    )
    assert not response["result"]["isError"], response
    return str(
        json.loads(response["result"]["content"][0]["text"])["outcomes"][0]["id"]
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("one\n")  # 1 line: `:2` falls outside
    (root / "src" / "ok.py").write_text("one\ntwo\nthree\n")
    return root


def _event(spool: Path, name: str, path: str, root: Path) -> None:
    spool.mkdir(parents=True, exist_ok=True)
    (spool / f"{name}.json").write_text(
        json.dumps({"path": path, "root": str(root), "at": "t"})
    )


def test_only_code_anchors_are_parsed() -> None:
    assert parse_code_anchor("apps/x/y.py:12-20") == ("apps/x/y.py", 12)
    assert parse_code_anchor("docs/a.md") == ("docs/a.md", None)
    assert parse_code_anchor("plugin/scripts/pr.sh:409-470") == (
        "plugin/scripts/pr.sh",
        409,
    )
    for not_code in (
        "&cap-1",
        "doc-3",
        "PR #12",
        "a verbatim quote\nspanning two lines",
        "apps/x",
    ):
        assert parse_code_anchor(not_code) is None, not_code
    # Review 2026-09-16: `x.y` is not a path. Versions, numbers, and domains became
    # `dead_anchor` → `caducou` under `--all`; a `..` segment is stripped here, not by the judge.
    assert parse_code_anchor("README.md") == ("README.md", None)
    for not_a_path in (
        "v1.2",
        "0.5",
        "3.14",
        "menot.you",
        "e.g",
        "../x.md",
        "a/../b.py",
    ):
        assert parse_code_anchor(not_a_path) is None, not_a_path


def test_the_judge_never_leaves_the_root(tmp_path: Path) -> None:
    """An anchor comes from the capsule (agent-inferred, imported, tainted): the judge stats
    and counts lines of whatever it points to. Outside root is `dead_anchor`, without opening."""
    root = _repo(tmp_path)
    (tmp_path / "outside.md").write_text("secret\n")
    assert judge_anchor(root, "../outside.md", 1) == "dead_anchor"
    (root / "src" / "link.md").symlink_to(tmp_path / "outside.md")
    assert judge_anchor(root, "src/link.md", 1) == "dead_anchor"


def test_recording_a_verdict_needs_a_scope(tmp_path: Path) -> None:
    """Without `--project`, `anchor-check --all` judged ALL scopes against this
    tree and recorded `dead_anchor` for foreign ones — append-only, without undo."""
    root = _repo(tmp_path)
    _pg, store, server = _fresh()
    _ingest(server, "fine", "src/ok.py:1", T0)
    with pytest.raises(AnchorScopeError):
        audit_all(store, root, now=T0)
    with pytest.raises(AnchorScopeError):
        drain_spool(store, tmp_path / "spool", root, now=T0)
    # Review a506eb24 #2: `project=""` passed `is None` and, in `list`, the empty
    # string means "unfiltered" — all scopes judged against this tree.
    with pytest.raises(AnchorScopeError):
        audit_all(store, root, now=T0, project="")
    with pytest.raises(AnchorScopeError):
        drain_spool(store, tmp_path / "spool", root, now=T0, project="")
    assert audit_all(store, root, now=T0, record=False).anchored == 1, (
        "reading alone is allowed without a fence"
    )


def test_the_judge_reads_the_tree_not_the_capsule(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert judge_anchor(root, "src/ok.py", 2) == "pass"
    assert judge_anchor(root, "src/ok.py", None) == "pass"
    assert judge_anchor(root, "src/a.py", 2) == "moved_anchor"
    assert judge_anchor(root, "src/nope.py", 1) == "dead_anchor"


def test_a_drain_judges_every_capsule_anchored_on_the_edited_path(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _pg, store, server = _fresh()
    moved = _ingest(server, "fact anchored past the end", "src/a.py:2", T0)
    dead = _ingest(server, "fact anchored on a vanished file", "src/nope.py:1", T0)
    fine = _ingest(server, "fact anchored where it still is", "src/ok.py:2", T0)
    _ingest(server, "not a code anchor at all", "&cap-1", T0)
    spool = tmp_path / "spool"
    for name, path in (("e1", "src/a.py"), ("e2", "src/nope.py"), ("e3", "src/ok.py")):
        _event(spool, name, path, root)

    report = drain_spool(
        store, spool, root, now=T0 + timedelta(minutes=1), project="anchor"
    )
    assert report.events == 3 and report.judged == 3
    assert {r.result for r in store.anchors.list_verifications(moved)} == {
        "moved_anchor"
    }
    assert {r.result for r in store.anchors.list_verifications(dead)} == {"dead_anchor"}
    # The first view of a LINE is `baseline`: the anchor resolves, but no one
    # had the summary of what it said, so there is no drift to assert. `pass` is the
    # subsequent sweep, which proves the line still says the same thing.
    assert {r.result for r in store.anchors.list_verifications(fine)} == {"baseline"}
    assert list(spool.iterdir()) == [], "o dreno consome os eventos"
    for name, path in (("e4", "src/ok.py"),):
        _event(spool, name, path, root)
    drain_spool(store, spool, root, now=T0 + timedelta(minutes=90), project="anchor")
    assert [r.result for r in store.anchors.list_verifications(fine)] == [
        "baseline",
        "pass",
    ]

    again = drain_spool(
        store, spool, root, now=T0 + timedelta(minutes=2), project="anchor"
    )
    assert again.events == 0 and again.judged == 0
    assert len(store.anchors.list_verifications(moved)) == 1, "one event, one verdict"


def test_an_event_from_another_repo_is_dropped_not_resolved(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _pg, store, server = _fresh()
    cid = _ingest(server, "fact", "src/a.py:2", T0)
    spool = tmp_path / "spool"
    _event(spool, "foreign", "src/a.py", tmp_path / "elsewhere")
    report = drain_spool(store, spool, root, now=T0, project="anchor")
    assert report.events == 1 and report.judged == 0 and report.foreign == 1
    assert store.anchors.list_verifications(cid) == []
    assert list(spool.iterdir()) == []


def test_all_reproduces_the_dead_anchor_rate(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _pg, store, server = _fresh()
    _ingest(server, "moved", "src/a.py:2", T0)
    _ingest(server, "dead", "src/nope.py:1", T0)
    _ingest(server, "fine", "src/ok.py:1", T0)
    _ingest(server, "not code", "&cap-1", T0)
    primeira = audit_all(store, root, now=T0, project="anchor")
    assert primeira.baseline == 1 and primeira.ok == 0, (
        "a live line debuts as `baseline`, not as `pass`"
    )
    report = audit_all(store, root, now=T0 + timedelta(days=1), project="anchor")
    assert (
        report.anchored == 3
        and report.dead == 1
        and report.moved == 1
        and report.ok == 1
        and report.changed == 0
    )
    assert report.dead_rate_pct == 66.7


def _ingest_in(
    server: MemoryServer, content: str, anchor: str, project: str, now: datetime
) -> str:
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": content,
            "source": "t",
            "anchor": anchor,
            "kind": "fact",
            "project_id": project,
        },
        context=_ctx(),
        now=now,
    )
    assert not response["result"]["isError"], response
    return str(
        json.loads(response["result"]["content"][0]["text"])["outcomes"][0]["id"]
    )


def test_anchors_are_repo_relative_so_the_scope_is_the_repo(tmp_path: Path) -> None:

    root = _repo(tmp_path)
    _pg, store, server = _fresh()
    ours = _ingest_in(server, "ours, fine", "src/ok.py:1", "anchor", T0)
    theirs = _ingest_in(
        server, "theirs, same path, other repo", "src/ok.py:99", "other-repo", T0
    )
    report = audit_all(store, root, now=T0, project="anchor")
    assert report.anchored == 1 and report.baseline == 1 and report.moved == 0
    assert store.anchors.list_verifications(theirs) == [], (
        "another scope is never judged against this tree"
    )
    assert len(store.anchors.list_verifications(ours)) == 1

    spool = tmp_path / "spool"
    _event(spool, "e1", "src/ok.py", root)
    d = drain_spool(store, spool, root, now=T0, project="anchor")
    assert d.judged == 1 and store.anchors.list_verifications(theirs) == []


def test_the_drain_judges_the_effective_anchor_not_the_birth_one(
    tmp_path: Path,
) -> None:
    """Review a506eb24 #3: `reanchors_among` replaced `effective_anchor` in
    the drain without proof. A re-anchored capsule is judged by the NEW anchor and only
    when the new path is edited."""
    root = _repo(tmp_path)
    _pg, store, server = _fresh()
    cid = _ingest(server, "born on a.py, moved to nope.py", "src/a.py:2", T0)
    store.anchors.reanchor(cid, "src/nope.py:1", "moved by hand", T0)
    spool = tmp_path / "spool"
    _event(spool, "old", "src/a.py", root)
    report = drain_spool(store, spool, root, now=T0, project="anchor")
    assert report.events == 1 and report.judged == 0, "the birth path no longer counts"
    _event(spool, "new", "src/nope.py", root)
    report = drain_spool(store, spool, root, now=T0, project="anchor")
    assert report.judged == 1
    assert [r.result for r in store.anchors.list_verifications(cid)] == ["dead_anchor"]


def test_an_anchor_that_was_never_about_this_tree_is_unverifiable(
    tmp_path: Path,
) -> None:
    """Measured on the live store 2026-09-17: of 162 anchor candidates, 60 (37 %)
    had a first segment that is NOT a directory of this repo — `scratchpad/`,
    `reports/`, `native369/`, `PR277/`, run names, shas. They were never
    about this tree, and `judge_anchor` responded `dead_anchor` to all: the
    capsule went to the queue, the model read `<file missing>` and fenced it as false.
    "The file vanished" and "this anchor is not from this repo" are different
    things, and only the former is evidence about the fact."""
    root = _repo(tmp_path)
    assert judge_anchor(root, "src/nope.py", 1) == "dead_anchor"
    for foreign in ("scratchpad/rnd/ab_final.py", "native369/report.md", "PR277/x.py"):
        assert judge_anchor(root, foreign, 1) == "unverifiable", foreign


def test_a_run_artifact_is_not_code_even_inside_the_repo(tmp_path: Path) -> None:
    """`lanes/state/review-pr17/codex-sol.out` and `.claude/battery-….output`
    live in real directories, but are run outputs: deleting them says nothing
    about the fact. Three of the 162 on the live store; two of them were fenced as
    false and I had to revoke."""
    root = _repo(tmp_path)
    for artifact in ("src/codex-sol.out", "src/ab_final.log", "src/battery.output"):
        assert judge_anchor(root, artifact, 1) == "unverifiable", artifact


def test_the_dead_rate_does_not_count_what_it_could_not_judge(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _pg, store, server = _fresh()
    _ingest(server, "moved", "src/a.py:2", T0)
    _ingest(server, "dead", "src/nope.py:1", T0)
    _ingest(server, "fine", "src/ok.py:1", T0)
    _ingest(server, "foreign", "scratchpad/rnd/x.py:1", T0)
    _ingest(server, "artifact", "src/run.log:1", T0)
    report = audit_all(store, root, now=T0, project="anchor")
    assert report.unverifiable == 2
    # The rate is over what WAS judged: 2 of 3, not 2 of 5.
    assert report.anchored == 3 and report.dead_rate_pct == 66.7


def test_a_verdict_the_census_cannot_count_refuses(monkeypatch, tmp_path: Path) -> None:
    """The census had an `else: moved += 1`: a new verdict silently entered the
    wrong category and the dead anchor rate lied. Now it rejects."""
    root = _repo(tmp_path)
    _pg, store, server = _fresh()
    _ingest(server, "fine", "src/ok.py:1", T0)
    monkeypatch.setattr(anchors, "judge_anchor", lambda *_: "quinto_veredito")
    with pytest.raises(AnchorVerdictError, match="quinto_veredito"):
        audit_all(store, root, now=T0, project="anchor")


def test_uma_edicao_no_mesmo_numero_de_linhas_deixa_de_ser_invisivel(
    tmp_path: Path,
) -> None:
    """Cell D of U5: the file changed in CONTENT and kept the line
    count. `judge_anchor` counts lines, so it answered `pass`, the capsule never
    entered the curator queue, and a claim that became false continued to
    be served with full authority. Measured in U5: 6 → 6, zero movement.

    The detector is mechanical and costs zero: the hash of the anchored, normalized
    LINE, stored in the `detail` of the event itself (`path:line#hash`) — no DDL, no
    new table, without bumping `CONTRACT_VERSION`."""
    root = _repo(tmp_path)
    _pg, store, server = _fresh()
    cid = _ingest(server, "ok() devolve two", "src/ok.py:2", T0)

    primeira = audit_all(store, root, now=T0, project="anchor")
    assert primeira.baseline == 1, "the first sweep has nothing to compare against"
    assert curator_candidates(store.db, project="anchor", now=T0) == [], (
        "a baseline does NOT cost a model call"
    )
    assert standing_of(store.db, [cid], now=T0)[cid]["state"] == "por_verificar", (
        "a baseline cannot lie `vale` either"
    )

    # Same line count, different content.
    (root / "src" / "ok.py").write_text("one\nDOIS mudou\nthree\n")
    segunda = audit_all(store, root, now=T0 + timedelta(days=1), project="anchor")
    assert segunda.changed == 1, "the line changed and the judge must say so"
    fila = [
        c
        for c, _ in curator_candidates(
            store.db, project="anchor", now=T0 + timedelta(days=1)
        )
    ]
    assert fila == [cid], f"cell D must reach the curator queue: {fila}"


def test_reformatacao_pura_nao_paga_uma_chamada_ao_modelo(tmp_path: Path) -> None:
    """Changed content is not a false fact: indentation and whitespace are
    noise from `ruff format`, and escalating them flooded the queue that COSTS MONEY.
    Normalization absorbs them. A rewrap that splits the line in two is NOT
    absorbed, and that is stated intentionally — it is the price of a local detector."""
    root = _repo(tmp_path)
    _pg, store, server = _fresh()
    _ingest(server, "ok() devolve two", "src/ok.py:2", T0)
    audit_all(store, root, now=T0, project="anchor")

    (root / "src" / "ok.py").write_text("one\n\t  two   \nthree\n")
    after = audit_all(store, root, now=T0 + timedelta(days=1), project="anchor")
    assert after.changed == 0 and after.ok == 1, (
        "indentation and whitespace are not a content change"
    )
    assert (
        curator_candidates(store.db, project="anchor", now=T0 + timedelta(days=1)) == []
    )


def test_an_event_from_a_linked_worktree_counts_but_is_judged_against_root(
    tmp_path: Path,
) -> None:
    """251 of 251 events in the real spool were "foreign": every edit in this repo
    happens in a linked worktree, and the drain only accepted the exact root. A
    worktree of the same repository is the same repo, so the event COUNTS — but the
    verdict is on `root`, the tree described by the capsule. Judging the event's
    tree recorded a branch's WIP as fact about the repo, and those verdicts
    are `CURATOR_VERDICTS`: they sent true capsules to the curator queue
    and poisoned the digest of `root`."""
    import subprocess

    root = _repo(tmp_path)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], cwd=root, check=True)
    subprocess.run([*git, "add", "."], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], cwd=root, check=True)
    wt = tmp_path / "wt"
    subprocess.run([*git, "worktree", "add", "-q", str(wt)], cwd=root, check=True)
    (wt / "src" / "ok.py").unlink()  # dead ONLY in the worktree; `root` still has it

    _pg, store, server = _fresh()
    cid = _ingest(server, "fact", "src/ok.py:2", T0)
    spool = tmp_path / "spool"
    _event(spool, "wt", "src/ok.py", wt)
    report = drain_spool(store, spool, root, now=T0, project="anchor")
    assert report.foreign == 0 and report.judged == 1, report
    assert {r.result for r in store.anchors.list_verifications(cid)} == {"baseline"}, (
        "the verdict is about `root`, where the line exists — never `dead_anchor` "
        "because of a work branch"
    )

    _event(spool, "other", "src/ok.py", tmp_path / "elsewhere")
    again = drain_spool(store, spool, root, now=T0, project="anchor")
    assert again.foreign == 1 and again.judged == 0, "outro repo continua foreign"


def test_the_digest_of_a_worktree_event_is_read_from_root(tmp_path: Path) -> None:
    """The digest is also from `root`: reading it in the event's tree caused `main`
    to compare itself against branch bytes and record `changed_anchor` in
    ping-pong, indefinitely (surviving mutant flagged by review)."""
    import subprocess

    root = _repo(tmp_path)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], cwd=root, check=True)
    subprocess.run([*git, "add", "."], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], cwd=root, check=True)
    wt = tmp_path / "wt"
    subprocess.run([*git, "worktree", "add", "-q", str(wt)], cwd=root, check=True)

    _pg, store, server = _fresh()
    cid = _ingest(server, "fact", "src/ok.py:2", T0)
    spool = tmp_path / "spool"
    _event(spool, "b1", "src/ok.py", root)
    drain_spool(store, spool, root, now=T0, project="anchor")  # baseline from `root`

    # The worktree rewrites the line; `root` does not change.
    (wt / "src" / "ok.py").write_text("one\noutra coisa\nthree\n")
    _event(spool, "b2", "src/ok.py", wt)
    drain_spool(store, spool, root, now=T0 + timedelta(minutes=1), project="anchor")
    assert [r.result for r in store.anchors.list_verifications(cid)] == [
        "baseline",
        "pass",
    ], (
        "the `root` line did not change: `pass`, never `changed_anchor` with the "
        "worktree digest"
    )


def _git_repo(tmp_path: Path) -> Path:
    import subprocess

    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("def a():\n    return 1\n")
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "src/a.py"],
        ["commit", "-qm", "seed"],
    ):
        subprocess.run(["git", *cmd], cwd=root, check=True, capture_output=True)
    return root


def test_um_artefacto_de_sessao_ausente_nao_e_ancora_morta(tmp_path: Path) -> None:
    """ROOT of the 95: `docs/qa/raw/review-366.md` was never in the tree — its
    absence is not evidence about the anchored fact, and `dead_anchor` sent it
    to the curator queue, which fenced it as false.

    A git-tracked file that DISAPPEARED remains dead: this is the case where
    the tree actually lost proof."""
    import subprocess

    root = _git_repo(tmp_path)
    (root / "docs" / "qa").mkdir(parents=True)
    anchors._CONHECIDOS.clear()
    assert judge_anchor(root, "docs/qa/nunca-seguido.md", 1) == "unverifiable"
    # Removed without commit: remains in the index.
    (root / "src" / "a.py").unlink()
    anchors._CONHECIDOS.clear()
    assert judge_anchor(root, "src/a.py", 1) == "dead_anchor"
    # Removed BY A COMMIT: leaves the index, and this is the production case — a
    # clean checkout at HEAD. Without history, it was indistinguishable from "never
    # tracked" and `dead_anchor` became unreachable.
    subprocess.run(
        ["git", "commit", "-qam", "remove a.py"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    # ONE COMMIT LATER, on purpose: with removal at HEAD, a `--max-count=1`
    # added to `git log` would pass this test and in production would lose every
    # removal older than the latest commit.
    (root / "outro.txt").write_text("x\n")
    for cmd in (["add", "outro.txt"], ["commit", "-qm", "depois"]):
        subprocess.run(["git", *cmd], cwd=root, check=True, capture_output=True)
    anchors._CONHECIDOS.clear()
    assert judge_anchor(root, "src/a.py", 1) == "dead_anchor"
    (root / "src" / "movido.py").write_text("def m():\n    return 2\n")
    for cmd in (
        ["add", "src/movido.py"],
        ["commit", "-qm", "novo"],
        ["mv", "src/movido.py", "src/destino.py"],
        ["commit", "-qm", "renomeia"],
    ):
        subprocess.run(["git", *cmd], cwd=root, check=True, capture_output=True)
    anchors._CONHECIDOS.clear()
    assert judge_anchor(root, "src/movido.py", 1) == "dead_anchor"
    # AND THE OLD REMOVAL REMAINS DEAD, now that there are TWO commits with
    # removals. `--max-count=N` limits the commits ALREADY FILTERED by
    # `--diff-filter=D`, not history depth: with only one removal,
    # `--max-count=1` would still succeed, which is why the assertion above didn't
    # catch it. With two, it loses the oldest.
    assert judge_anchor(root, "src/a.py", 1) == "dead_anchor"


def test_um_ficheiro_que_existe_e_o_git_nao_segue_julga_se_na_mesma(
    tmp_path: Path,
) -> None:
    """The rule is "missing AND untracked". An uncommitted file (`AGENTS.md`
    in this tree today) is judged by content: degrading it to `unverifiable`
    would publish `caducou` on valid capsules."""
    root = _git_repo(tmp_path)
    (root / "AGENTS.md").write_text("linha\n")
    anchors._CONHECIDOS.clear()
    assert judge_anchor(root, "AGENTS.md", 1) == "pass"


def test_sem_git_o_veredito_e_o_de_sempre(tmp_path: Path) -> None:
    """No fixture in this suite is a repository and `judge_anchor` never depended
    on git: without it the decision falls back to previous behavior, not an exception."""
    root = tmp_path / "solto"
    (root / "src").mkdir(parents=True)
    anchors._CONHECIDOS.clear()
    assert judge_anchor(root, "src/sumiu.py", 1) == "dead_anchor"


def test_de_audit_all_ate_standing_of_um_artefacto_sai_de_caducou(
    tmp_path: Path,
) -> None:
    """END TO END, which was missing: the three `standing_of` tests write
    the event manually, so the claim "30 of the 49 capsules exit `caducou`"
    rested on synthetic events. Here the real scan writes the verdict.

    The capsule anchors to a session artifact that this tree never tracked. The
    OLD rule returned `dead_anchor` and `standing_of` published `caducou`; the
    new rule returns `unverifiable` and standing reverts to `por_verificar`."""
    root = _git_repo(tmp_path)
    (root / "docs" / "qa" / "raw").mkdir(parents=True)
    _pg, store, server = _fresh()
    cid = _ingest(server, "a claim about review 366", "docs/qa/raw/review-366.md:1", T0)
    # The past, written by the WRONG rule.
    store.anchors.record_verification(
        cid,
        kind="anchor",
        result="dead_anchor",
        detail="docs/qa/raw/review-366.md:1",
        actor="anchor-check",
        now=T0,
    )
    assert standing_of(store.db, [cid], now=T0)[cid]["state"] == "caducou"

    anchors._CONHECIDOS.clear()
    relatorio = audit_all(store, root, project="anchor", now=T0 + timedelta(days=1))
    assert relatorio.unverifiable >= 1, relatorio

    st = standing_of(store.db, [cid], now=T0 + timedelta(days=1))[cid]
    assert st["state"] == "por_verificar" and st["reason"] == "unverifiable", st
