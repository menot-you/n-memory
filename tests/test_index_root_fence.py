"""`nmemory_index_build` will index ANY git repository the caller names. That is the fence.

The server carries an `anchor_root` — the tree its provenance anchors are resolved
against, and in production it is never passed: `cli.py` builds its stdio `MemoryServer` without it,
so it falls back to the daemon's own `os.getcwd()`. The `root`
parameter of `nmemory_index_build`, by contrast, is a free string from the caller. Its schema
declares no enum, no pattern, no required prefix, and `index_context` resolves it with
`Path(root or os.getcwd()).resolve()` without ever comparing it to `anchor_root`.

There IS a path check inside `prepare_files`, and it is easy to mistake for this one: it
refuses a tracked symlink that escapes — escapes the root THE CALLER CHOSE. It bounds the
walk inside the named tree; it does not bound which tree can be named.

So an `nmemory_index_build` call reads a repository the owner never pointed it at and writes its
chunks into the owner's store. This is live in the release serving that store now.

The fence: `root` must resolve inside `anchor_root`. Nothing else changes — the ordinary
call, where `root` IS the anchor or is omitted entirely, keeps working untouched.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nmemory.server import MemoryServer
from nmemory.store import Store
from nmemory.tools import index as index_tool

NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)


def _git(repo: Path, args: list[str]) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(path: Path, marker: str) -> Path:
    """A git repository with one tracked markdown file carrying a unique marker."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "NOTES.md").write_text(f"{marker} lives here\n", encoding="utf-8")
    _git(path, ["init", "-q"])
    _git(path, ["config", "user.email", "t@t"])
    _git(path, ["config", "user.name", "t"])
    _git(path, ["add", "NOTES.md"])
    _git(path, ["commit", "-qm", marker])
    return path


def test_index_refuses_a_root_outside_the_anchor(tmp_path: Path) -> None:
    """The reproduction: two repositories, and the caller names the one the owner did not."""
    anchored = _repo(tmp_path / "anchored", "ANCHORED-MARKER")
    elsewhere = _repo(tmp_path / "elsewhere", "ELSEWHERE-MARKER")

    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store, anchor_root=str(anchored))

    res = index_tool.handle(
        server.handler_state(),
        {"root": str(elsewhere), "project_id": "nmemory"},
        None,
        NOW,
        "test",
    )
    body = res["content"][0]["text"]

    assert res.get("isError") or "outside" in body, (
        "nmemory_index_build indexed a repository OUTSIDE the anchor root and said nothing.\n"
        f"  anchor_root: {anchored}\n"
        f"  root asked for: {elsewhere}\n"
        f"  answer: {body[:400]}"
    )


def test_index_still_reads_the_anchored_tree(tmp_path: Path) -> None:
    """The fence must not cost the ordinary call. An explicit root EQUAL to the anchor,
    and an omitted root, both keep working — otherwise the fence is a regression wearing
    a security badge."""
    anchored = _repo(tmp_path / "anchored", "ANCHORED-MARKER")
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store, anchor_root=str(anchored))

    explicit = index_tool.handle(
        server.handler_state(),
        {"root": str(anchored), "project_id": "nmemory"},
        None,
        NOW,
        "test",
    )
    assert not explicit.get("isError"), explicit["content"][0]["text"][:300]
    assert json.loads(explicit["content"][0]["text"])["unique_chunks"] >= 1

    omitted = index_tool.handle(
        server.handler_state(), {"project_id": "nmemory"}, None, NOW, "test"
    )
    assert not omitted.get("isError"), omitted["content"][0]["text"][:300]


def test_index_refuses_a_symlinked_escape_from_the_anchor(tmp_path: Path) -> None:
    """A resolved-path fence, not a string-prefix one: `..` and a symlink both escape a
    naive `startswith`, and a fence that compares unresolved strings is theatre."""
    anchored = _repo(tmp_path / "anchored", "ANCHORED-MARKER")
    elsewhere = _repo(tmp_path / "elsewhere", "ELSEWHERE-MARKER")
    bridge = anchored / "bridge"
    bridge.symlink_to(elsewhere, target_is_directory=True)

    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store, anchor_root=str(anchored))

    res = index_tool.handle(
        server.handler_state(),
        {"root": str(bridge), "project_id": "nmemory"},
        None,
        NOW,
        "test",
    )
    body = res["content"][0]["text"]
    assert res.get("isError") or "outside" in body, (
        "a symlink inside the anchor walked the fence out to another repository; the "
        f"fence must compare RESOLVED paths. answer: {body[:400]}"
    )


def test_index_hands_the_judged_path_to_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path the fence JUDGED must be the path the reader USES. One resolution.

    This is the hole the first version of the fence left, and it was exploited against a
    live store before it was closed: resolve once to decide, hand the RAW string on, and
    the reader resolves a second time. Between those two resolutions there is a window,
    and a symlink swapped inside it is judged as the path it was and read as the path it
    became — 300 racing calls put chunks from a repository outside the anchor into
    `corpus_chunks` with the fence switched on.

    The assertion is deliberately NOT a race. A racing reproduction proves the bug exists
    but goes quiet the day the timing shifts, and a falsifier that only sometimes fires is
    not a falsifier. The invariant behind the race is exact and always checkable: whatever
    the caller wrote, the reader is handed the RESOLVED path the fence approved.
    """
    anchored = _repo(tmp_path / "anchored", "ANCHORED-MARKER")
    inside = _repo(anchored / "inside", "INSIDE-MARKER")
    bridge = anchored / "bridge"
    bridge.symlink_to(inside, target_is_directory=True)

    seen: dict[str, object] = {}

    def _spy(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"sources": [], "unique_chunks": 0}

    import nmemory.context

    monkeypatch.setattr(nmemory.context, "index_context", _spy)

    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store, anchor_root=str(anchored))
    index_tool.handle(server.handler_state(), {"root": str(bridge)}, None, NOW, "test")

    handed = seen.get("root")
    assert handed is not None, "index_context was never reached"
    assert Path(str(handed)) == bridge.resolve(), (
        "the fence judged the RESOLVED path and handed the reader something else.\n"
        f"  caller wrote:  {bridge}\n"
        f"  fence judged:  {bridge.resolve()}\n"
        f"  reader given:  {handed}\n"
        "Two resolutions of one string leave a window, and a symlink swapped inside it "
        "is judged as one path and read as another."
    )
